#!/usr/bin/env python3
"""Phanes lane autodispatcher (runs on the box).

Every TICK seconds: finish runs that ended (push branch + open PR, relabel issue), then give every idle
lane the next open issue labelled `lane-ready` (not `in-progress`), ordered p0 < p1 < p2 < unlabelled,
then by issue number. Optional `backend:pi|agy|codex` label pins an issue to that executor family.
State: ~/phanes-ctl/autodispatch/state.json   Log: ~/phanes-ctl/autodispatch/autodispatch.log + brain/log.md
Pause: the swrm pause store ($OPENSWARM_PAUSE_DIR, else $OPENSWARM_HOME/pauses, written by
`swrm pause <scope>` and by `swrm fleet health --apply`) and the legacy markers
~/phanes-ctl/autodispatch/PAUSE (whole loop), PAUSE-<LANE> (one lane),
PAUSE-HOST-<HOST>, PAUSE-BACKEND-<BACKEND> and PAUSE-MINT (no new issue minted). All are read (Refs #1366).
A hold that a program keeps under a scope of its own (`host:<host>@health`) holds the host as well (Refs #1367).
A legacy marker with an `until=<epoch|ISO>` deadline stops holding after that deadline, while the file stays as evidence (Refs #1592).
"""
import base64, hashlib, json, math, os, re, shlex, stat, subprocess, sys, threading, time, traceback, tempfile, concurrent.futures as cf
import importlib.util
from datetime import datetime, timezone
# Resolve the sibling even when the dispatcher is loaded by path from another directory.
# A deployment that copies only this file must still dispatch: without the
# classifier every card is read in full (Refs #1988).
try:
    _risk_spec = importlib.util.spec_from_file_location("tester_risk", os.path.join(os.path.dirname(__file__), "tester_risk.py"))
    tester_risk = importlib.util.module_from_spec(_risk_spec)
    _risk_spec.loader.exec_module(tester_risk)
except Exception:
    tester_risk = None
HOME = os.path.expanduser("~")
# The state dir holds the snapshot, the heartbeat and the log. It can be moved for a test or a
# second deployment (Refs #1697): the reader (tools/check-lanes-status-stall.py) resolves the
# same variables, so both sides always name the same file.
D = os.path.expanduser(os.environ.get("OPENSWARM_AD_STATE_DIR") or os.environ.get("XSWARM_AD_STATE_DIR")
                        or f"{HOME}/phanes-ctl/autodispatch")
# The snapshot file itself can be named explicitly (the control plane reads the same path).
LANES_STATUS_FILE_ENVS = ("OPENSWARM_LANES_STATUS_FILE", "XSWARM_LANES_STATUS_FILE")
# The knobs that name the refresh cadence of the snapshot publisher: while a slow step of the
# tick runs, the snapshot is republished so `_at` follows the dispatch rhythm (Refs #1697).
STATUS_REFRESH_ENVS = ("AD_STATUS_REFRESH", "AD_STATUS_REFRESH_SECONDS", "AD_SNAPSHOT_REFRESH",
                        "AD_REFRESH_SECONDS", "LANES_STATUS_REFRESH")
try:  # a typo in the configuration must not kill the dispatcher at import time
    STATUS_REFRESH_SECONDS = float(next(os.environ[name] for name in STATUS_REFRESH_ENVS if os.environ.get(name)))
except (StopIteration, TypeError, ValueError):
    STATUS_REFRESH_SECONDS = 30.0
REPO = "surlymochan/phanes"; LOCAL = "/workspace/phanes-repo"; BRAIN_LOG = "/workspace/phanes/brain/log.md"
TICK = int(os.environ.get("AD_TICK", "60")); BUDGET = os.environ.get("AD_BUDGET", "45"); LOW = int(os.environ.get("AD_LOW", "10"))
# The pool is topped up to TARGET when it drops below LOW: refilling to LOW only leaves the
# pool at the threshold, so the lanes starve again before the next refill (Refs #1662).
TARGET = int(os.environ.get("AD_TARGET", "20"))
# A queue read that fails leaves the pool level unknown. The refill then works on the last
# known level plus the cards minted since, and the number of consecutive failures is
# bounded: past the bound the AD waits for an authoritative read instead of minting a new
# batch every tick (Refs #1662).
QUEUE_UNREADABLE_REFILL_MAX = int(os.environ.get("AD_QUEUE_FAIL_REFILL_MAX", "3"))
MINT_EVIDENCE = {}  # what the last refill attempt did: rejected cards, planner gate (Refs #1662)
LAST_MINT_LOG = 0.0  # throttle of the repeated "the planner gate blocked a new card" line
RULES = ("Rules: work only inside this repository worktree (the current directory). Never create, modify, move or delete "
         "files outside it — not the main repo, not $HOME, not /tmp; for scratch files use $TMPDIR (inside the worktree, cleaned "
         "automatically) and leave cleanup to the executor. Do not push, do not open PRs, do not touch credentials, no network "
         "installs. Keep the change small and factual; cite file paths and line numbers you checked. Commit your result with a "
         "clear message referencing the issue (\"Refs #{n}\"). Write in the same language as the issue. "
         "Your commit message must also carry a short body with a `Closes #{n}` line and a `验收：<command>` line, and the "
         "commit subject (the PR title) must contain `#{n}`, so the lane PR passes the pre-submit check (Refs #1704). Include a 验收条目→改动文件→测试 mapping table, one row per acceptance item (Refs #1849).")
IMPL_CARD_TASK_BOOK = ("## Impl card delivery requirements\n"
                       "1. 交付说明必须对每个新增函数列出入口→调用链→数据结构来源：由谁调用、输入来自哪个真实数据结构、每个字段由谁填写。\n"
                       "2. 至少补一个 tick/集成级测试：从真实入口（tick 或等价入口）驱动到可观测结果；只测函数加 mock 不算。\n")
# The brief of a fix round (rubric F1..F6, Refs #1851): a fix round card asks a lane to redo a
# rejected PR, so the task book has to force the six checklist items of the rubric. The `{pr}`
# placeholder is the PR of the round (the `rebase-pr-<N>` branch to continue from); it is replaced
# with the number the card names, or with `<N>` when the card names none.
FIX_ROUND_TASK_BOOK = ("## Fix round checklist (rubric F1-F6)\n"
                       "F1 续改基点：从 `rebase-pr-{pr}` 续改（上一轮 PR 的头），先做祖先校验 "
                       "`git merge-base --is-ancestor <base> rebase-pr-{pr}`；校验不过就停下报告，不要另起分支重写。\n"
                       "F2 先枚举同类点：先把同类问题全部枚举出来（同样的写法、同样的调用点），再逐个修，不要只修眼前这一处。\n"
                       "F3 逐条 BLOCKER 勾选并附证据：上一轮每条 BLOCKER 都要在交付说明里逐条勾选（`- [x]`），"
                       "并附证据（文件:行号 + 测试名或命令输出）。\n"
                       "F4 先写失败测试：先写能复现上一轮问题的失败测试，跑一次确认是红的，再改实现让它转绿。\n"
                       "F5 不删既有集成测试：不得删除或跳过既有的集成测试；要改断言就改断言并写明理由。\n"
                       "F6 复跑 A4/A2：修完复跑 A4/A2 两项验收（同一条命令、同一个环境），把结果贴进交付说明。\n")
# The markers of a fix round card: a fix label (`fix`, `card:fix`, `type:fix`, `review-fix`…) or a
# `## Fix Round` heading, review request or rebase branch in the body, or a fix round title.
# An ordinary work card that merely fixes a bug is not a fix round.
FIX_ROUND_CARD_LABELS = {"fix", "fix-round", "review-fix", "rework",
                         "card:fix", "card:fix-round", "card:review-fix", "card:rework",
                         "type:fix", "type:fix-round", "type:review-fix", "type:rework"}
FIX_ROUND_HEADING_RE = re.compile(r"(?mi)^##[^\S\r\n]*(?:fix[^\S\r\n]*round|review[^\S\r\n]*fix|rework|修复轮|返修轮)[^\S\r\n]*$")
FIX_ROUND_TITLE_RE = re.compile(r"(?i)^(?:fix[ _-]?round|review[ _-]?fix|rework|修复轮|返修轮)(?:\s*[:：]|\s+|$)")
# The PR of the round: the branch the card names, or its `目标 PR` field.
FIX_ROUND_PR_BRANCH_RE = re.compile(r"(?i)rebase-pr-(\d+)")
FIX_ROUND_PR_FIELD_RE = re.compile(r"(?i)(?:目标[^\S\r\n]*PR|PR)[^\S\r\n]*[:：]?[^\S\r\n]*#?[^\S\r\n]*(\d+)")
NON_IMPL_CARD_TYPES = {"upstream", "ops", "follow-up", "planner", "verify-intent"}
PRIO = {"p0": 0, "p1": 1, "p2": 2}
DISPATCH_EXCLUDING_LABELS = {"in-progress", "lane-pr", "lane-failed"}
# One multiplexed connection per host (cuts handshakes ~10x; banner-exchange timeouts came from bursts of parallel ssh).
SSHO = ["-o", "ConnectTimeout=20", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ControlMaster=auto",
        "-o", f"ControlPath={HOME}/.ssh/cm/%r@%h:%p", "-o", "ControlPersist=600"]
# Hosts reached through a jump host need an explicit ProxyCommand (Refs #907). The jump topology is
# NOT hardcoded here: the box ssh config owns it (docs/dual-mode-ssh.md), and a command-line
# `-o ProxyCommand=` overrides the config's `ProxyJump`. The table below is operator configuration,
# read from $AD_JUMP_HOSTS (JSON object) or <state dir>/jump-hosts.json; empty means "no injection".
JUMP_HOSTS_ENV = "AD_JUMP_HOSTS"
JUMP_HOSTS_FILE = "jump-hosts.json"
BACKOFF = {}  # host -> {"n": consecutive failures, "until": epoch}; transient ssh errors never mark a lane or issue failed
HOST_BACKOFF_BASE = 30
HOST_BACKOFF_MAX = 600
HARVEST_FETCH_MAX_ATTEMPTS = 3  # initial fetch plus two bounded retries
HARVEST_FETCH_RETRY_DELAY = 20
# Refs #947: a harvest fetch failure is transient until HARVEST_FETCH_MAX_TRANSIENT_FAILURES consecutive
# failures; up to that threshold it only logs and backs off, past it the issue is labelled lane-failed.
HARVEST_FETCH_MAX_TRANSIENT_FAILURES = int(os.environ.get("AD_HARVEST_MAX_FAILURES", "3"))
HARVEST_FETCH_BACKOFF_BASE = 30  # seconds; doubles per consecutive failure (30s → 60s → 120s …)
HARVEST_FETCH_BACKOFF_CAP = 600
# Transfer/ssh hiccups seen in the field (M4#904): `fetch-pack: unexpected disconnect`, `bad pack header`.
HARVEST_FETCH_TRANSIENT_RE = re.compile(r"Permission denied|Connection closed|broken pipe|timed out|banner|"
                                        r"unexpected disconnect|bad pack header|early eof|index-pack failed|"
                                        r"RPC failed|Connection reset|hung up unexpectedly", re.I)
# Refs #1810: the base commit is read from upstream (ls-remote) and must exist in the target repo
# before the lane is assigned. The target repo can lag behind upstream (its object store misses the
# new base), which surfaced as `fatal: Not a valid object name <base>^{commit}` plus a 30s host
# backoff on every base switch. The validation is NOT relaxed: a base that is still missing after
# the fetch is reported (and backed off) exactly as before.
BASE_BRANCH = "main"
BASE_REPO_URL = f"https://github.com/{REPO}.git"  # the upstream the base commit is read from
DISPATCH_BASE_FETCH_ATTEMPTS = 3  # bounded attempts (check -> fetch -> check) when the base is missing
DISPATCH_BASE_FETCH_DELAY = 5     # seconds between two attempts
BASE_MISSING_RE = re.compile(r"Not a valid object name", re.I)
DISPATCH_FETCH_FAILURE_WINDOW = 3600
DISPATCH_FETCH_TRANSIENT_RE = re.compile(
    r"gnutls_handshake|tls|SSL|Could not resolve host|unable to access|Connection reset|"
    r"Connection timed out|timed out|unexpected disconnect|early eof|remote end hung up", re.I)
DISPATCH_FETCH_FAILURES = {}  # host -> recent [{at, type}]; used by health and blocking diagnostics
STALE_STRIPPED_N = 0  # stale search-index entries removed by this process
STALE_LANE_FAILED_N = 0  # stale hits whose issue still carries both lane-failed and lane-ready (Refs #985)
LABEL_CACHE = {}  # issue number -> (status, payload): authoritative label reads of the current tick
LABEL_READS_N = 0  # authoritative `gh issue view` reads issued during the current tick
READ_ERROR_LOGGED = set()  # issues whose unreadable state was already logged in this tick
# GitHub charges one GraphQL point per `gh issue view`: cap the authoritative reads of one
# tick so a 100-row queue and a 60s tick cannot blow through the hourly quota (Refs #950).
FRESH_READ_BUDGET = int(os.environ.get("AD_FRESH_BUDGET", "60"))
# Verify cards rejected for missing fields, per issue: the field set that was logged. An invalid card
# must not repeat the same log line every tick, but a changed field set is logged again.
VERIFY_REJECT_LOGGED = {}
# --- 车道空闲间隙（Refs #1688）------------------------------------------------------
# A lane that finished a run is idle until it gets its next card.  When the pool is not
# empty and no capacity is in the way, that gap is pure scheduling delay, so the
# dispatcher hands the lane its next card right after the harvest instead of waiting for
# the next tick, and it measures the gap so a long one can be explained.
IDLE_LOG_MIN_S = int(os.environ.get("AD_IDLE_LOG_S", "300"))  # 5 min: the brain-log threshold
HEARTBEAT_GRACE_S = 10  # the heartbeat is written every few seconds: this is its jitter
# The buckets of an idle window and the reason each of them explains.
IDLE_BUCKETS = (("down_s", "dispatcher not running"), ("no_card_s", "no card"),
                ("no_cap_s", "no capacity"), ("paused_s", "lane paused"),
                ("lag_s", "dispatcher not dispatching"))

# --- verify-card queue share (Refs #1794) ------------------------------------------------
# The verification reservation is enabled explicitly for gradual rollout: with the switch off the
# dispatch order is exactly the one from before this policy.
def env_number(name, default, cast=float):
    """Read a numeric environment variable; a malformed value falls back to the default.

    A typo in the configuration must not kill the AD loop at import time."""
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

AD_VERIFY_SHARE_ENABLED = os.environ.get("AD_VERIFY_SHARE_ENABLED", "0").lower() in {"1", "true", "yes"}
VERIFY_SHARE = env_number("AD_VERIFY_SHARE", 0.2)
VERIFY_MIN = env_number("AD_VERIFY_MIN", 1, int)
# The MiniMax burn reading the lane status shows (Refs #1671). A reading older than
# this is not shown: a stopped burn watchdog must not pass the last known burn off
# as the current one.
MM_BURN_MAX_AGE_S = env_number("MM_BURN_MAX_AGE_S", 900, int)
# The burn alert is logged at most every this many seconds (the watchdog runs every tick).
MM_BURN_WARN_SEC = env_number("MM_BURN_WARN_SEC", 600, int)
BURN_WARN_AT = 0.0  # epoch second of the last burn warning

# --- review lane load balancing (Refs #1701) ---------------------------------------------
# The review lanes (CX2/CX4/CX5) take the review cards by their in-flight backlog: the
# lane with the smallest backlog wins, equal backlogs rotate over the review lanes, and a
# lane that already carries `AD_REVIEW_MAX_IN_FLIGHT` reviews is skipped - the review
# waits for a free lane instead of piling up on one lane. The decision (the PR, the
# chosen lane and the backlog of every review lane) goes to the AD log, so the assignment
# can be reviewed afterwards.
REVIEW_MAX_IN_FLIGHT = max(1, env_number("AD_REVIEW_MAX_IN_FLIGHT", 1, int))
# Tie-break rotation for review lanes (#1701 asks only for round-robin among equal backlogs).
# Deliberately process-local: autodispatch is one long-running loop, and the real selection
# input (the in-flight review counts) is re-derived every tick, so a restart merely restarts
# the rotation and can never skew the load balancing or lose a review.
REVIEW_ROTATION = {}  # review lane set -> {"count": n, "last": {lane: n}}: the turns of the rotation

# --- dispatch priority (Refs #1705) -------------------------------------------------------
# Scores deliberately use time in the *dispatchable pool*, not issue creation time.
PRIORITY_WAIT_STEP_S = max(1.0, env_number("AD_PRIORITY_WAIT_STEP_S", 900.0))
PRIORITY_WAIT_WEIGHT = env_number("AD_PRIORITY_WAIT_WEIGHT", 1.0)
PRIORITY_LARGE_BUDGET_MINUTES = env_number("AD_PRIORITY_LARGE_BUDGET_MINUTES", 90.0)
PRIORITY_LARGE_BONUS = env_number("AD_PRIORITY_LARGE_BONUS", 8.0)
PRIORITY_BACKEND_EXACT = env_number("AD_PRIORITY_BACKEND_EXACT", 4.0)
PRIORITY_BACKEND_UNPINNED = env_number("AD_PRIORITY_BACKEND_UNPINNED", 2.0)
PRIORITY_STARVATION_S = max(0.0, env_number("AD_PRIORITY_STARVATION_S", 3600.0))
PRIORITY_STARVATION_BONUS = env_number("AD_PRIORITY_STARVATION_BONUS", 16.0)

def timestamp(value):
    """Return an epoch timestamp for a numeric or ISO-8601 value, else None."""
    if isinstance(value, bool) or value is None: return None
    if isinstance(value, (int, float)): return float(value)
    text = str(value).strip()
    if not text: return None
    for pattern, candidate in (("%Y-%m-%dT%H:%M:%SZ", text), ("%Y-%m-%dT%H:%M:%S", text[:19])):
        try: return time.mktime(time.strptime(candidate, pattern))
        except ValueError: pass
    return None

def budget_minutes(item):
    """Read the dispatch budget populated by queue(), accepting legacy spellings."""
    for key in ("budget_minutes", "budgetMinutes", "budget"):
        try: return float(item.get(key))
        except (TypeError, ValueError): pass
    return 0.0

def pool_wait_seconds(item, now=None):
    """How long the card has actually been dispatchable, never its issue age."""
    now = time.time() if now is None else now
    entered = timestamp(item.get("pool_entered_at"))
    if entered is None: entered = timestamp(item.get("poolEnteredAt"))
    if entered is None: entered = now
    return max(0.0, now - entered)

def priority_score(backend, item, now=None):
    """Return the score and auditable components used by dispatch ordering."""
    waited = pool_wait_seconds(item, now)
    wait_score = math.floor(waited / PRIORITY_WAIT_STEP_S) * PRIORITY_WAIT_WEIGHT
    budget = budget_minutes(item)
    large = budget >= PRIORITY_LARGE_BUDGET_MINUTES
    size_score = PRIORITY_LARGE_BONUS if large else 0.0
    if item.get("backend") == backend: backend_score = PRIORITY_BACKEND_EXACT
    elif item.get("backend") is None: backend_score = PRIORITY_BACKEND_UNPINNED
    else: backend_score = 0.0
    starvation_score = PRIORITY_STARVATION_BONUS if not large and waited >= PRIORITY_STARVATION_S else 0.0
    components = {"wait_s": round(waited, 3), "wait": wait_score, "size": size_score,
                  "backend": backend_score, "starvation": starvation_score,
                  "budget_minutes": budget}
    return sum(components[k] for k in ("wait", "size", "backend", "starvation")), components

def ctl_state_dir():
    directory = os.path.expanduser(os.environ.get("OPENSWARM_CTL_STATE_DIR", f"{HOME}/phanes-ctl/state"))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory

def pause_home(home=None):
    """The home that holds the pause store ($OPENSWARM_HOME by default)."""
    if home: return os.path.expanduser(home)
    return os.path.expanduser(os.environ.get("OPENSWARM_HOME") or os.environ.get("XSWARM_HOME") or f"{HOME}/.claude/swrm")

def pause_store_dir(home=None):
    """The swrm pause store, shared with the fleet health check (Refs #1367)."""
    configured = os.environ.get("OPENSWARM_PAUSE_DIR") or os.environ.get("XSWARM_PAUSE_DIR")
    if home is None and configured: return os.path.expanduser(configured)
    return os.path.join(pause_home(home), "pauses")

def pause_executor_start_lane(lane):
    """Cool down only the failing executor's lane; the card has not had an attempt."""
    now = time.time()
    scope = f"lane:{lane}"
    directory = pause_store_dir()
    os.makedirs(directory, exist_ok=True)
    record = {"scope": scope, "reason": "auto:executor_start_failed", "created_at": now,
              "expires_at": now + 600}
    # Write atomically so dispatch never observes a partially written pause.
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
        temporary = stream.name
        json.dump(record, stream)
    os.replace(temporary, os.path.join(directory, hashlib.sha256(scope.encode()).hexdigest() + ".json"))

def marker_scope(name):
    """The pause scope of a legacy PAUSE marker file name, or None when it is none."""
    if name == "PAUSE": return "all"
    if name == "PAUSE-MINT": return "mint"
    for prefix, kind in (("PAUSE-HOST-", "host"), ("PAUSE-BACKEND-", "backend")):
        if name.startswith(prefix) and len(name) > len(prefix): return f"{kind}:{name[len(prefix):]}"
    if name.startswith("PAUSE-") and len(name) > len("PAUSE-"): return f"lane:{name[len('PAUSE-'):]}"
    return None

# The disk watermark guard of the box control plane (Refs #1666) measures a remote lane host and
# pauses it when the disk is too full. The health sweep can clear the `auto:` host pause, so the
# dispatcher reads the state file of the guard as well: that is an independent gate that keeps the
# lane closed for the whole hysteresis band, even between two guard ticks.
DISK_WATERMARK_STATE_PREFIX = "disk-watermark-"

def disk_watermark_states(now=None):
    """The disk watermark states of the guard: one record per measured lane host."""
    # A blocked host stays blocked until the guard records a measured recovery.
    # A stopped guard and a missing marker are not evidence of free disk space.
    states = {}
    try: names = sorted(os.listdir(D))
    except OSError: return states
    for name in names:
        if not name.startswith(DISK_WATERMARK_STATE_PREFIX) or not name.endswith(".json"): continue
        try:
            with open(os.path.join(D, name)) as stream: record = json.load(stream)
        except (OSError, ValueError): continue
        if not isinstance(record, dict): continue
        host = record.get("host")
        if not isinstance(host, str) or not host.strip(): continue
        updated = record.get("updated_at")
        if isinstance(updated, bool) or not isinstance(updated, (int, float)): continue
        states[host.strip()] = record
    return states

def disk_watermark_blocked(host, states=None):
    """Whether the disk watermark guard blocks the host (the independent disk gate)."""
    if not host: return False
    states = disk_watermark_states() if states is None else states
    record = states.get(host)
    if not record: return False
    return bool(record.get("blocked"))

# The deadline of a legacy pause marker: the health loop writes `auto:cooldown:until=<epoch|ISO>`
# (`tools/check-health-pause.py`), the quota loop `mm_remains:until=<ISO>`
# (`tools/check-health-quota-fresh.py`). The deadline is separated from the rest of the body by a
# colon or a space, so both shapes are read (Refs #1592).
PAUSE_UNTIL_RE = re.compile(r"(?:^|[\s:])until=(\S+)", re.MULTILINE)
# The markers whose expiry was already logged: one log line per marker, not one per tick.
EXPIRED_PAUSE_LOGGED = set()

def pause_until(body):
    """The deadline of a legacy pause marker body (`until=<epoch|ISO>`), in epoch seconds.

    A naive ISO timestamp is UTC, exactly like in `tools/check-health-pause.py`: a deadline
    must not move with the timezone of the machine that reads it. None when the body carries
    no readable deadline: such a marker never expires."""
    match = PAUSE_UNTIL_RE.search(body or "")
    if not match: return None
    value = match.group(1)
    try: return float(value)
    except ValueError: pass
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError: return None
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()

def marker_body(name, directory=None):
    """The body of a legacy pause marker (empty when the file cannot be read)."""
    try:
        with open(os.path.join(D if directory is None else directory, name)) as stream: return stream.read()
    except OSError: return ""

def marker_reason(name, body):
    """The reason of a legacy marker: its name plus the first line of its body.

    The body belongs to the reason: `auto:cooldown:until=...` says more than the file name,
    and the operator finds the cause of the hold in the lane status (Refs #1592)."""
    lines = (body or "").strip().splitlines()
    first = lines[0].strip() if lines else ""
    return f"legacy marker {name}: {first}" if first else f"legacy marker {name}"

def log_expired_pause(name, deadline):
    """Log once that the deadline of a legacy marker passed: the hold ends, the file stays."""
    key = (name, deadline)
    if key in EXPIRED_PAUSE_LOGGED: return
    EXPIRED_PAUSE_LOGGED.add(key)
    try: log(f"pause expired: {name} (until={deadline}) holds no lane any more; the marker stays as evidence", brain=False)
    except Exception: pass

def pause_records(now=None, home=None):
    """The active pause records: the swrm pause store plus the legacy PAUSE markers.

    A store record carries a TTL (`expires_at`); an expired record is no pause. A legacy
    marker carries its own deadline in the body (`until=<epoch|ISO>`); an expired marker is
    no pause either, but the file stays on disk as evidence (the health loop owns the
    removal). The result is sorted by scope."""
    now = time.time() if now is None else now
    records = []
    disk_states = disk_watermark_states(now)
    store = pause_store_dir(home)
    try: names = sorted(os.listdir(store))
    except OSError: names = []
    for name in names:
        if not name.endswith(".json"): continue
        try:
            with open(os.path.join(store, name)) as stream: record = json.load(stream)
        except (OSError, ValueError): continue
        if not isinstance(record, dict): continue
        scope = record.get("scope")
        if not isinstance(scope, str) or not scope.strip(): continue
        expires = record.get("expires_at")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)): expires = None
        elif expires <= now: continue
        records.append({"scope": scope.strip(), "reason": str(record.get("reason") or ""),
                        "created_at": record.get("created_at"), "expires_at": expires, "source": "store"})
    try: names = sorted(os.listdir(D))
    except OSError: names = []
    for name in names:
        # A recovery claim remains a gate until it is inspected and removed. If
        # restoring an independent pause collides with a new marker, keep both.
        if name.startswith(".pause-recovery-"):
            try: claimed = os.listdir(os.path.join(D, name))
            except OSError: claimed = []
            for marker in claimed:
                scope = marker_scope(marker)
                if scope is not None:
                    records.append({"scope": scope, "reason": f"recovery hold: {name}/{marker}",
                                    "created_at": 0.0, "expires_at": None, "source": "marker"})
            continue
        scope = marker_scope(name)
        if scope is None: continue
        body = marker_body(name)
        # Version 2 stopped writing shared host markers. Retain old files
        # untouched, but use its independent state instead of its legacy marker.
        # Other writers do not share a lock with the guard; deleting the legacy
        # file after reading its body could delete a concurrent manual pause.
        if name.startswith("PAUSE-HOST-"):
            host = name[len("PAUSE-HOST-"):]
            version = disk_states.get(host, {}).get("guard", 0)
            if isinstance(version, int) and version >= 2:
                if body.startswith(f"auto:disk-watermark host={host} "): continue
        deadline = pause_until(body)
        if deadline is not None and deadline <= now:
            log_expired_pause(name, deadline)
            continue
        records.append({"scope": scope, "reason": marker_reason(name, body), "created_at": 0.0,
                        "expires_at": deadline, "source": "marker"})
    # Fleet health also writes legacy markers in the pause store.
    if os.path.abspath(store) != os.path.abspath(D):
        try: names = sorted(os.listdir(store))
        except OSError: names = []
        for name in names:
            scope = marker_scope(name)
            if scope is None: continue
            body = marker_body(name, store)
            deadline = pause_until(body)
            if deadline is not None and deadline <= now:
                log_expired_pause(name, deadline)
                continue
            records.append({"scope": scope, "reason": marker_reason(name, body), "created_at": 0.0,
                            "expires_at": deadline, "source": "marker"})
    # The private marker also gates dispatch if writing the state failed.
    try: disk_markers = os.listdir(os.path.join(D, ".disk-watermark"))
    except OSError: disk_markers = []
    for name in disk_markers:
        scope = marker_scope(name)
        if scope and scope.startswith("host:"):
            records.append({"scope": scope, "reason": "disk watermark marker",
                            "created_at": 0.0, "expires_at": None, "source": "marker"})
    # The disk watermark gate (Refs #1666): a blocked lane host stays blocked even when the
    # health sweep removed its pause marker, as long as the guard reports the block.
    for host, record in disk_states.items():
        if not record.get("blocked"): continue
        records.append({"scope": f"host:{host}", "reason": f"disk watermark guard: {record.get('state')}",
                        "created_at": record.get("updated_at"), "expires_at": None,
                        "source": "disk-watermark"})
    return sorted(records, key=lambda row: row["scope"])

def paused_scopes(now=None, records=None):
    """The scopes that are paused right now (the store and the legacy markers)."""
    records = pause_records(now) if records is None else records
    return {row["scope"] for row in records}

def lane_paused(lane, cfg=None, scopes=None):
    """Whether a lane is paused by an all/lane/host/backend scope."""
    scopes = paused_scopes() if scopes is None else scopes
    if "all" in scopes: return True
    if f"lane:{lane}" in scopes: return True
    cfg = cfg or {}
    if cfg.get("host") and holds_target(scopes, "host", cfg["host"]): return True
    if cfg.get("backend") and holds_target(scopes, "backend", cfg["backend"]): return True
    return False

def holds_target(scopes, kind, value):
    """Whether a scope holds the dispatch target `kind:value`.

    A hold that a program keeps under a scope of its own (`host:<host>@health`) holds the
    target as well: the fleet health check never overwrites the pause of an operator, and
    the release of the operator never frees the host (Refs #1367).
    """
    base = f"{kind}:{value}"
    if base in scopes: return True
    return any(str(scope).startswith(base + "@") for scope in scopes)
def pause_priority(row):
    """The order in which the switches are read: the global PAUSE, then the pause store,
    then the remaining legacy markers (Refs #1592)."""
    if row.get("source") == "marker" and row.get("scope") == "all": return 0
    if row.get("source") == "store": return 1
    return 2

def pause_reason(lane, cfg=None, records=None, now=None):
    """Why a lane is paused right now (None when it is not paused).

    The result names the scope that holds the lane, the switch it comes from, the reason of
    the switch and the remaining seconds (None when the switch has no deadline). It is what
    `lanes-status.json` shows, so the operator sees which switch stops the lane and for how
    long (Refs #1592)."""
    now = time.time() if now is None else now
    records = pause_records(now) if records is None else records
    cfg = cfg or {}
    wanted = [f"lane:{lane}"]
    if cfg.get("host"): wanted.append(f"host:{cfg['host']}")
    if cfg.get("backend"): wanted.append(f"backend:{cfg['backend']}")
    for row in sorted(records, key=pause_priority):
        scope = row.get("scope")
        if scope != "all" and scope not in wanted and not (
            cfg.get("host") and holds_target({scope}, "host", cfg["host"]) or
            cfg.get("backend") and holds_target({scope}, "backend", cfg["backend"])): continue
        expires = row.get("expires_at")
        remaining = None if expires is None else max(0, int(round(expires - now)))
        return {"scope": scope, "source": row.get("source"), "reason": row.get("reason"),
                "remaining_s": remaining}
    return None

def pause_reasons(lanes, records=None, now=None):
    """The pause reason of every paused lane (the lane status view of the switches)."""
    records = pause_records(now) if records is None else records
    reasons = {}
    for lane, cfg in lanes.items():
        reason = pause_reason(lane, cfg, records, now)
        if reason: reasons[lane] = reason
    return reasons


def mint_paused(scopes=None):
    """Whether the minting of new issues is paused (the PAUSE-MINT marker / the mint scope)."""
    scopes = paused_scopes() if scopes is None else scopes
    return "mint" in scopes

# --- switch audit (Refs #1827) -------------------------------------------------------------
# The PAUSE*/EXCLUDE* files of the autodispatch directory are the dispatch switches. Nobody
# knows today who wrote them: the file is left behind and the dispatch silently stops. Every
# tick compares the switch set (name, mtime, content) with the previous tick and appends one
# line per change to `switch-audit.jsonl`. The audit only records: it never removes nor
# restores a switch, and it never stops the dispatch loop (Refs #1827).
SWITCH_AUDIT_FILE = "switch-audit.jsonl"
SWITCH_AUDIT_STATE = "switch-audit-state.json"
SWITCH_ALERT_FILE = "health.alert"
SWITCH_PREFIXES = ("PAUSE", "EXCLUDE")
# An authorization entry: an operator order in the brain log, or the `auto:` marker the
# health loop writes into its own switches (docs/ctl-boot.md).
AUTHORIZATION_RE = re.compile(r"用户.*令|user order|auto:")
AUTHORIZATION_TAIL_LINES = 30

def switch_audit_log(message):
    """Write one line to the AD log; the audit never raises (Refs #1827)."""
    try: log(message, brain=False)
    except Exception: pass

def switch_stats(directory=None):
    """The stat result of every switch file of the autodispatch directory: (name -> stat).

    The second value of the result is False when the directory cannot be listed or one of
    the entries cannot be read: an unreadable directory is not an empty switch set, and an
    unreadable entry is not a deleted switch (Refs #1827)."""
    root = D if directory is None else directory
    try: entries = sorted(os.listdir(root))
    except OSError: return {}, False
    found = {}
    for name in entries:
        if not name.startswith(SWITCH_PREFIXES): continue
        path = os.path.join(root, name)
        try: info = os.stat(path)              # the entry must be readable, else the set is unknown
        except FileNotFoundError: continue     # it is really gone: a deletion, not a read error
        except OSError: return {}, False       # it cannot be read: the switch set is unknown
        # the file type comes from the stat above: a second stat (`os.path.isfile`) swallows
        # its own OSError and would drop a living switch from the set, which fakes a
        # deletion and breaks the baseline (Refs #1827)
        if stat.S_ISREG(info.st_mode): found[name] = info
    return found, True

def switch_owner(info):
    """The owner of a switch file: uid, gid and the user name (None when it is unknown)."""
    owner = {"uid": info.st_uid, "gid": info.st_gid, "name": None}
    try:
        import pwd
        owner["name"] = pwd.getpwuid(info.st_uid).pw_name
    except (ImportError, KeyError, OSError):
        pass
    return owner

def switch_summary(data):
    """A one-line summary of a switch body (the first non-empty line, truncated)."""
    text = data.decode("utf-8", "replace").strip()
    if not text: return ""
    line = text.splitlines()[0].strip()
    return line if len(line) <= 120 else line[:120] + "…"

def switch_entry(directory, name, info=None):
    """The snapshot of one switch file: mtime, size, content digest, owner and summary.

    `info` is the stat result of the file (the caller already did it): it is reused here so
    that a switch is stat'ed only once per tick. None means the file cannot be read (a
    permission error, a broken file): the switch is not proven to be gone, so the caller
    must not record a deletion (Refs #1827)."""
    path = os.path.join(directory, name)
    try:
        if info is None: info = os.stat(path)
        with open(path, "rb") as stream: data = stream.read()
    except OSError:
        return None
    return {"name": name, "mtime": info.st_mtime, "size": info.st_size,
            "digest": hashlib.sha256(data).hexdigest(), "owner": switch_owner(info),
            "summary": switch_summary(data)}

def switch_snapshot(directory=None):
    """The snapshot of every switch file of the directory: (file name -> entry, complete).

    `complete` is False when the directory or one of the switches cannot be read: such a
    snapshot is not comparable with the baseline, it would fake deletions and creations
    (Refs #1827)."""
    root = D if directory is None else directory
    stats, listed = switch_stats(root)
    if not listed: return {}, False
    snapshot = {}
    for name, info in stats.items():
        entry = switch_entry(root, name, info)
        if entry is None: return {}, False
        snapshot[name] = entry
    return snapshot, True

def load_switch_audit_state(path):
    """The snapshot of the previous tick (empty when there is none).

    A corrupt state file is reported as corrupt: the caller rebuilds the baseline instead of
    writing a row for every switch (a broken file must not fake a change)."""
    try:
        with open(path, encoding="utf-8") as stream: state = json.load(stream)
    except FileNotFoundError: return {}, False
    except (OSError, ValueError): return {}, True
    switches = state.get("switches") if isinstance(state, dict) else None
    if not isinstance(switches, dict): return {}, True
    return {name: entry for name, entry in switches.items() if isinstance(entry, dict)}, False

def save_switch_audit_state(path, snapshot):
    """Write the snapshot of the switches as the baseline of the next tick."""
    try:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump({"at": time.strftime("%F %T"), "switches": snapshot}, stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    except OSError as e:
        switch_audit_log(f"switch-audit: the baseline is not writable ({e!r})")

def brain_authorization(path=None, lines=AUTHORIZATION_TAIL_LINES):
    """The authorization entry of the last `lines` lines of the brain log (None when there is none).

    The brain log is the only place where an operator writes down why a switch was written;
    only its tail is read, so a long log stays cheap."""
    source = BRAIN_LOG if path is None else path
    try:
        with open(source, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell(); data = b""
            while position > 0 and data.count(b"\n") <= lines:
                step = min(8192, position); position -= step
                stream.seek(position); data = stream.read(step) + data
    except OSError:
        return None
    for line in data.decode("utf-8", "replace").splitlines()[-lines:]:
        if AUTHORIZATION_RE.search(line): return line.strip()[:200]
    return None

def switch_changes(previous, current):
    """The changes between two snapshots: (file name, action, before, after) per change.

    A file is `created` when it was not there, `deleted` when it is gone and `changed` when
    its mtime or its content differ (both are compared: a touched switch is a change too)."""
    changes = []
    for name in sorted(current):
        before = previous.get(name)
        if before is None:
            changes.append((name, "created", None, current[name])); continue
        same_mtime = float(before.get("mtime", -1)) == float(current[name]["mtime"])
        if before.get("digest") != current[name]["digest"] or not same_mtime:
            changes.append((name, "changed", before, current[name]))
    for name in sorted(previous):
        if name not in current: changes.append((name, "deleted", previous[name], None))
    return changes

def switch_audit_rows(changes, authorization, moment=None):
    """Build one audit row per change (the authorization is the brain log entry, if any).

    A switch whose own body starts with `auto:` is attributed to the health loop, so it is
    not marked `unattributed` even when the brain log carries no order."""
    stamp = time.strftime("%F %T") if moment is None else moment
    rows = []
    for name, action, before, after in changes:
        source = after if after is not None else before
        body = (source or {}).get("summary", "")
        owner = (source or {}).get("owner") or {"uid": None, "gid": None, "name": None}
        evidence = authorization
        if evidence is None and "auto:" in body: evidence = "auto:"
        rows.append({"at": stamp, "file": name, "action": action,
                     "before": (before or {}).get("summary", ""), "after": (after or {}).get("summary", ""),
                     "owner": owner, "mtime": (source or {}).get("mtime"),
                     "unattributed": evidence is None, "authorization": evidence or ""})
    return rows

def write_switch_alert(root, row):
    """Write the alert of an unattributed switch change into `health.alert`."""
    owner = row.get("owner") or {}
    line = (f"{row['at']} level=hard source=switch-audit file={row['file']} action={row['action']} "
            f"owner={owner.get('name') or owner.get('uid')} uid={owner.get('uid')} mtime={row.get('mtime')} "
            f"unattributed: the switch changed without an authorization entry")
    with open(os.path.join(root, SWITCH_ALERT_FILE), "a", encoding="utf-8") as stream:
        stream.write(line + "\n")

def audit_switches(directory=None, brain_log=None):
    """Audit the switch files of one tick and append the changes to `switch-audit.jsonl`.

    Returns the audit rows of this tick (empty when nothing changed). Every failure is
    swallowed: a broken audit file or an unreadable switch directory never stops the
    dispatch loop (Refs #1827)."""
    root = D if directory is None else directory
    try:
        return _audit_switches(root, brain_log)
    except Exception as e:
        switch_audit_log(f"switch-audit failed: {e!r}")
        return []

def _audit_switches(root, brain_log):
    state_path = os.path.join(root, SWITCH_AUDIT_STATE)
    previous, corrupt = load_switch_audit_state(state_path)
    current, complete = switch_snapshot(root)
    if not complete:
        # the switch set is not fully readable: no change is proven, so nothing is compared
        # and the baseline is kept as it is. A read error must not fake a deletion, and the
        # next readable tick still compares against the last known good baseline, so the
        # changes of the unreadable window are audited once, with the right before/after.
        switch_audit_log("switch-audit: the switch set is not fully readable, this tick is skipped")
        return []
    if corrupt:
        # the baseline is broken: rebuild it, but do not fake a change for every switch
        switch_audit_log("switch-audit: the baseline is unreadable, it is rebuilt without rows")
        save_switch_audit_state(state_path, current)
        return []
    rows = switch_audit_rows(switch_changes(previous, current), brain_authorization(brain_log))
    if rows:
        try:
            with open(os.path.join(root, SWITCH_AUDIT_FILE), "a", encoding="utf-8") as stream:
                for row in rows: stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as e:
            switch_audit_log(f"switch-audit: the audit file is not writable ({e!r})")
        for row in rows:
            switch_audit_log(f"switch-audit: {row['file']} {row['action']}"
                             + (" (unattributed)" if row["unattributed"] else ""))
            if row["unattributed"]:
                try: write_switch_alert(root, row)
                except OSError as e: switch_audit_log(f"switch-audit: the alert is not writable ({e!r})")
    save_switch_audit_state(state_path, current)
    return rows

def worker_state_path():
    return os.path.join(ctl_state_dir(), "autodispatch.pid")

def process_start_time(pid):
    """Process start time in epoch seconds: /proc on Linux, `ps -o lstart=` elsewhere (macOS has no /proc)."""
    try:
        with open(f"/proc/{pid}/stat") as stream:
            stat = stream.read()
        fields = stat[stat.rfind(")") + 2:].split()
        with open("/proc/stat") as stream:
            boot_time = float(stream.read().split("btime ", 1)[1].split()[0])
        return boot_time + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except FileNotFoundError:
        import subprocess, time
        try:
            out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                                 env={**os.environ, "LC_ALL": "C"}, timeout=10).stdout.strip()
        except subprocess.SubprocessError as error:
            raise OSError(str(error)) from error
        if not out:
            raise ProcessLookupError(pid)
        return time.mktime(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))

def worker_alive(pid, started_at):
    try:
        if isinstance(pid, bool) or not re.fullmatch(r"[0-9]+", str(pid)):
            return False
        pid = int(pid)
        if pid <= 1:
            return False
        os.kill(pid, 0)
        process_started_at = process_start_time(pid)
        age = float(started_at) - process_started_at
        return 0 <= age <= 5
    except PermissionError:
        return True
    except (ProcessLookupError, ValueError, OSError, IndexError, TypeError):
        return False
    return True

def start_autodispatch(command=None):
    """Start autodispatch once, removing a stale pid left by a reboot."""
    state_path = worker_state_path()
    try:
        with open(state_path) as stream:
            state = json.load(stream)
        if worker_alive(state["pid"], state["started_at"]):
            return state
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        os.unlink(state_path)
    except FileNotFoundError:
        pass
    command = command or os.environ.get("OPENSWARM_AUTODISPATCH_COMMAND", f"{HOME}/phanes-ctl/bin/lane-autodispatch")
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    if not argv:
        raise ValueError("worker command must not be empty")
    process = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    state = {"pid": process.pid, "started_at": time.time()}
    temporary = state_path + ".tmp"
    with open(temporary, "w") as stream:
        json.dump(state, stream)
        stream.write("\n")
    os.replace(temporary, state_path)
    return state

def host_ok(h): return time.time() >= BACKOFF.get(h, {}).get("until", 0)
def host_fail(h, why):
    b = BACKOFF.setdefault(h, {"n": 0, "until": 0}); b["n"] += 1
    wait = min(HOST_BACKOFF_MAX, HOST_BACKOFF_BASE * 2 ** (b["n"] - 1)); b["until"] = time.time() + wait
    log(f"{h} ssh transient error #{b['n']} ({why[-80:]}); backing off {wait}s", brain=(b["n"] == 4))
def host_good(h):
    if BACKOFF.pop(h, None): log(f"{h} ssh recovered", brain=False)

# --- 主机回归检测（Refs #1673）------------------------------------------------------
# The box loses its connection from time to time.  While a host is unreachable the
# control plane holds it with a `PAUSE-HOST-<host>` file, and the runs that were in
# flight are lost with the outage.  When the host comes back, both are re-checked
# automatically.  Two rules hold everywhere below:
#
#   * only a pause whose cause is the unreachability of the host is released.  The
#     host being reachable again does not prove that another cause (the hard disk
#     limit, the memory limit, a machine cooldown, an operator hold) is gone;
#   * a run is only re-queued when the runner of that run is *positively* dead.  An
#     unknown liveness keeps the lane and its card: the runner may still be running
#     and a second dispatch would run the same work twice.
HOST_UNREACHABLE_MARKER = "auto: host_unreachable"
# The closed list of the reasons that unambiguously mean "the host was unreachable".
# The reason is compared as a whole phrase: a body that merely contains "ssh" or
# "streak" ("auto: api failure streak 3") is an unknown cause and is kept.
HOST_UNREACHABLE_REASONS = ("host_unreachable", "ssh_unreachable", "unreachable",
                             "host unreachable", "ssh unreachable", "ssh down",
                             "ssh failed", "ssh streak", "ssh failure streak")
# The run states that prove that the runner of a run is not running any more.
TERMINAL_RUN_STATES = ("finished", "failed", "unroutable", "budget_rejected",
                       "dry-run", "cancelled", "stopped")

def host_pause_path(host):
    """The path of the automatic pause file of a host."""
    return os.path.join(D, f"PAUSE-HOST-{host}")

def lane_pause_path(lane):
    """The path of the automatic pause file of a lane."""
    return os.path.join(D, f"PAUSE-{lane}")

def read_pause(path):
    """The body of a pause file ("" when it does not exist or cannot be read)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream: return stream.read()
    except OSError: return ""

def pause_body_reason(body):
    """The reason of an `auto:` pause body (None for a manual hold)."""
    text = (body or "").strip()
    if not text.startswith("auto:"): return None
    return text[len("auto:"):].strip()

def normalized_pause_reason(body):
    """The reason of an automatic pause, normalized for the comparison.

    The counters are dropped ("ssh streak 3" becomes "ssh streak"), the case and the
    whitespace do not matter."""
    reason = pause_body_reason(body)
    if reason is None: return None
    return re.sub(r"\s+", " ", re.sub(r"\s+\d+\b", "", reason)).strip().lower()

def pause_cause_is_unreachable(body):
    """Whether an automatic pause was caused by the unreachability of the host.

    Only a body that names the unreachability proves it: the marker this dispatcher
    writes itself, or one of the explicit reasons of the closed list.  Every other
    cause (the disk limit, the memory limit, a machine cooldown, a quota) and every
    unknown reason is kept: a reachable host does not prove that cause is gone
    (Refs #1673)."""
    reason = normalized_pause_reason(body)
    if reason is None: return False
    return reason.startswith("host_unreachable") or reason in HOST_UNREACHABLE_REASONS

def lanes_of_host(lanes, host):
    """The lanes that run on a host."""
    return [lane for lane, cfg in lanes.items() if (cfg or {}).get("host") == host]

def host_pause_paths(host, lane_names=()):
    """The pause files of a host: its own pause and the pauses of its lanes."""
    return [host_pause_path(host), *(lane_pause_path(lane) for lane in lane_names)]

def write_unreachable_pause(host, since):
    """Hold the host with the marker of its unreachability (Refs #1673).

    An existing pause is never overwritten: it carries another cause (the disk
    limit, a manual hold) that this dispatcher does not own."""
    path = host_pause_path(host)
    try:
        with open(path, "x", encoding="utf-8") as stream:
            stream.write(f"{HOST_UNREACHABLE_MARKER} host={host} since={int(since)}\n")
    except FileExistsError: return False
    except OSError as e:
        log(f"{host}: the automatic pause of the outage was not written ({e!r})", brain=False)
        return False
    return True

def release_unreachable_pauses(host, paths, explain=True):
    """Release the automatic pauses of a host whose cause is its unreachability.

    Atomically move the candidate to a private claim before checking its cause.
    Only the claimed path is ever deleted; a health writer can create a new pause
    at the original path without recovery unlinking it. Claims remain pause gates
    until removed, including after an error or a dispatcher restart (Refs #1673)."""
    released = []
    for path in paths:
        body = read_pause(path)
        if not body: continue
        name = os.path.basename(path)
        if not pause_cause_is_unreachable(body):
            if explain:
                log(f"{name}: kept after the recovery of {host} (the cause is not the unreachability: "
                    f"{normalized_pause_reason(body) or 'manual hold'})", brain=False)
            continue
        claim_dir = None
        claimed = None
        try:
            claim_dir = tempfile.mkdtemp(prefix=".pause-recovery-", dir=os.path.dirname(path))
            claimed = os.path.join(claim_dir, name)
            os.rename(path, claimed)
            # The original path may have changed after the first read. Check the
            # file we actually claimed, never unlink the shared pathname.
            if not pause_cause_is_unreachable(read_pause(claimed)):
                try: os.link(claimed, path)
                except FileExistsError:
                    # Preserve the independent cause as an additional pause gate;
                    # replacing the new marker would lose that writer's cause.
                    log(f"{name}: independent recovery hold retained at {claimed}", brain=False)
                    continue
                os.unlink(claimed)
                continue
            os.unlink(claimed)
        except FileNotFoundError: continue
        except OSError as e:
            log(f"{name}: the automatic pause of the outage was not released ({e!r})", brain=False)
            continue
        finally:
            if claim_dir:
                try: os.rmdir(claim_dir)
                except OSError: pass  # A retained claim must stay visible to pause_records().
        if os.path.lexists(path): continue
        released.append(name)
        log(f"{name}: released after the recovery of {host} (the pause was caused by its unreachability)")
    return released

def host_records(state):
    """The reachability records of the hosts (the host -> record table of the state)."""
    records = state.get("_hosts")
    if not isinstance(records, dict): records = {}
    state["_hosts"] = records
    return records

def note_host_unreachable(state, host):
    """Record that the host is unreachable and hold it with the own marker.

    The record lives in the state of the dispatcher, so the recovery of the host is
    detected even when the outage spans a restart of the dispatcher."""
    records = host_records(state)
    record = records.get(host)
    if not isinstance(record, dict): record = {}
    if not record.get("unreachable_since"):
        record["unreachable_since"] = time.time()
        write_unreachable_pause(host, record["unreachable_since"])
        log(f"{host} is unreachable: it is held until it comes back", brain=False)
    record["last_error_at"] = time.time()
    records[host] = record

def host_came_back(state, host):
    """Whether the host just came back (the recovery edge); the edge is consumed.

    The edge is the transition from unreachable to reachable.  It is reported once
    per outage: the re-check of the pauses and the verification of the runs in
    flight happen on that edge, and the verification itself is retried in the
    following ticks until it succeeds."""
    records = host_records(state)
    record = records.get(host)
    if not isinstance(record, dict): record = {}
    if record.get("unreachable_since"):
        record["unreachable_since"] = None
        record["recovered_at"] = time.time()
        records[host] = record
        return True
    # The dispatcher may have been restarted during the outage: its own pause marker
    # still holds the host, and the host is reachable again.
    if read_pause(host_pause_path(host)).startswith(HOST_UNREACHABLE_MARKER):
        record["unreachable_since"] = None
        record["recovered_at"] = time.time()
        records[host] = record
        return True
    return False

def recovery_runs(state):
    """The runs whose liveness still has to be verified (the lane -> run table).

    The table is bounded like the other bookkeeping of the state: a lane that
    disappears from the configuration cannot keep its entry forever."""
    pending = state.get("_recovery")
    if not isinstance(pending, dict): pending = {}
    while len(pending) > 200:
        pending.pop(next(iter(pending)), None)
    state["_recovery"] = pending
    return pending

def mark_recovery_runs(state, host, lanes):
    """Mark the runs of a host that came back for the liveness verification.

    A host that was unreachable may have lost the runs that were in flight: the run
    directory of the host is gone, or its runner died with the outage.  The runs are
    marked here and verified in the ticks that follow: the verification needs a
    positive proof that the runner is dead, so it can take more than one tick
    (Refs #1673)."""
    pending = recovery_runs(state)
    for lane in lanes_of_host(lanes, host):
        st = state.get(lane)
        if not st: continue
        record = pending.get(lane)
        if isinstance(record, dict) and record.get("run") == st.get("run"): continue
        pending[lane] = {"host": host, "run": st.get("run"), "issue": st.get("issue"),
                         "since": time.time()}
        log(f"{lane}: the run {st.get('run')} of {host} is verified (a live runner continues the run, "
            f"a dead runner re-queues the card)", brain=False)

def runner_liveness(status):
    """The liveness of the runner of a run: "alive", "dead" or "unknown".

    "unknown" is the answer when the host cannot tell: the run has no status file,
    or the file names no runner.  A run whose runner may still be alive is never
    re-queued: its card would be dispatched a second time (Refs #1673)."""
    if status.get("runner_alive") is True: return "alive"
    if status.get("runner_alive") is False: return "dead"
    if str(status.get("run_state") or "").strip().lower() in TERMINAL_RUN_STATES: return "dead"
    return "unknown"

def now(): return time.strftime("%H:%M")

# --- lane PR 提交前校验（Refs #1704）---------------------------------------------
# lane PR 缺 Closes #N 时合并后 issue 不会自动关闭，缺验收段时审查方无法核验；两者都能在
# 提交前发现。校验对象是真正要提交的标题与正文：缺项逐条返回给 runner，严格模式下不建 PR。
PR_VALIDATION_ENV = "AD_PR_VALIDATION"        # strict（默认，缺项即拦截）| warn（只记录不拦截）
PR_VALIDATION_STRICT = "strict"
PR_VALIDATION_WARN = "warn"
PR_VALIDATION_CLASS = "pr_validation"         # 校验失败：可快速修复类，不计入 lane-failed
PR_VALIDATION_FIX_CLASS = "quick_fix"
CLOSING_RE = re.compile(r"\b(?:Closes|Fixes|Resolves)\s+#([1-9]\d*)\b", re.I)
ACCEPTANCE_RE = re.compile(r"(?im)^[ \t]*(?:验收|Verify|Acceptance)[ \t]*[:：][ \t]*(\S.*)$")

def validate_lane_pr(issue, title, body):
    """Return the required lane PR fields that are missing (empty list = submittable).

    Refs #1704: the body must close the task issue, must carry an acceptance section a
    reviewer can verify, and the title must name the issue."""
    missing = []
    if not any(int(number) == int(issue) for number in CLOSING_RE.findall(body or "")):
        missing.append(f"PR 正文缺少 Closes/Fixes/Resolves #{issue}")
    if not ACCEPTANCE_RE.search(body or ""):
        missing.append("PR 正文缺少验收段或 Verify 段")
    if not re.search(rf"(?<!\d)#{int(issue)}(?!\d)", title or ""):
        missing.append(f"PR 标题缺少 issue 号 #{issue}")
    return missing

def pr_validation_mode():
    """Return the PR validation mode: `strict` (default) blocks a PR with missing fields."""
    mode = (os.environ.get(PR_VALIDATION_ENV) or PR_VALIDATION_STRICT).strip().lower()
    return mode if mode in (PR_VALIDATION_STRICT, PR_VALIDATION_WARN) else PR_VALIDATION_STRICT

def closing_reference(issue, text):
    """Return the closing reference the lane itself provided for `issue` ("" when it did not).

    Only a closing keyword (`Closes`/`Fixes`/`Resolves`) that names the task issue counts.
    The title is not searched: the title has to carry the issue number, but a number in the
    title is not a closing instruction and the AD must not invent one for the lane
    (Refs #1704)."""
    for match in CLOSING_RE.finditer(text or ""):
        if int(match.group(1)) == int(issue): return f"Closes #{issue}"
    return ""

def acceptance_evidence(verify_rc, report, summary, tests):
    """Return the acceptance evidence of a lane run ("" when the run has none).

    Only real evidence counts: the host verify rc, the lane's own acceptance statement, or
    the test command the lane reported. A missing verify rc is not evidence, and a missing
    test result stays empty instead of turning into the string `None`."""
    if verify_rc is not None: return f"host verify rc `{verify_rc}`"
    for text in (report or "", summary or ""):
        found = ACCEPTANCE_RE.search(text)
        if found: return found.group(1).strip()
    reported = (tests or "").strip()
    if reported and reported.lower() not in ("none", "null"): return f"lane reported tests: `{reported[:200]}`"
    return ""

def review_evidence_errors(evidence):
    """Validate the versioned raw package, never deriving facts from lane opinions."""
    if not isinstance(evidence, dict): return ["review evidence package missing or not an object"]
    if type(evidence.get("version")) is not int or evidence["version"] != 1:
        return ["unsupported review evidence version"]
    errors = []
    for section, fields in (("ci", ("conclusion", "workflow", "url")), ("tests", ("summary",))):
        value = evidence.get(section)
        if not isinstance(value, dict):
            errors.append(f"review evidence missing {section}")
            continue
        for field in fields:
            if not isinstance(value.get(field), str) or not value[field].strip():
                errors.append(f"review evidence missing {section}.{field}")
    diff = evidence.get("diff")
    if not isinstance(diff, dict): errors.append("review evidence missing diff")
    else:
        for field in ("files", "insertions", "deletions"):
            if type(diff.get(field)) is not int or diff[field] < 0:
                errors.append(f"review evidence invalid diff.{field}")
    acceptance = evidence.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        errors.append("review evidence missing acceptance checklist")
    else:
        for item in acceptance:
            if (not isinstance(item, dict) or not isinstance(item.get("text"), str)
                    or not item["text"].strip() or type(item.get("ok")) is not bool):
                errors.append("review evidence invalid acceptance item")
    return errors


def review_evidence_body(evidence, summary):
    """Keep raw fields in fixed sections and the opinion in a separate quoted block.

    JSON escaping preserves multiline raw values without letting them create new
    Markdown headings. Words in logs and check results remain raw evidence; only
    the executor summary belongs to the lane opinion block.
    """
    def raw(value):
        return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace("`", "\\u0060")

    parts = ["## CI\n" + raw(evidence["ci"]),
             "## 测试日志摘要\n" + raw(evidence["tests"]),
             "## diff 统计\n" + raw(evidence["diff"]),
             "验收：原始核查清单\n" + "\n".join(
                 f"- [{'x' if item['ok'] else ' '}] {raw(item['text'])}"
                 for item in evidence["acceptance"])]
    if summary:
        parts.append("## Lane 意见（参考、不算判定）\n" + "\n".join(
            "> " + line for line in str(summary).splitlines()))
    return parts


def lane_pr_content(issue, lane, cfg, st, info, subject, message, summary, tests, verify_rc):
    """Build the lane PR title and body from the lane's own artifacts.

    Nothing is fabricated here: the title is the lane commit subject, and the closing
    reference and the acceptance section come from the lane's own report (commit message /
    executor summary / tests) or from the host verify run. What the lane did not provide
    stays missing, so the submit path can block the PR (Refs #1704)."""
    title = (subject or "").strip()[:200]
    report = (message or "").strip()
    parts = []
    # the closing reference comes from the lane's own text only: the title carries the issue
    # number by rule, and a number in the title is not a closing instruction (Refs #1704)
    closing = closing_reference(issue, report if st.get("class") == "verify" else f"{report}\n{summary or ''}")
    if closing: parts.append(closing)
    if st.get("class") == "verify":
        package = info.get("evidence")
        if not review_evidence_errors(package):
            parts.extend(review_evidence_body(package, summary))
        return title, "\n\n".join(parts)
    evidence = acceptance_evidence(verify_rc, report, summary, tests)
    if evidence: parts.append(f"验收：{evidence}")
    verify_note = f", host verify rc `{verify_rc}`" if verify_rc is not None else ""
    parts.append(f"Autodispatched to lane **{lane}** on **{cfg.get('host')}** ({info.get('backend')}/{info.get('model')}), "
                 f"run `{st.get('run')}`, base `{str(st.get('base'))[:8]}`, lane status `{info.get('status')}`{verify_note}.")
    if summary: parts.append(f"Executor summary: {summary}")
    return title, "\n\n".join(parts)

# --- 交付说明映射表预检（Refs #1849，rubric A7/F3）--------------------------------
# 首轮与 fix 轮的交付说明须含「验收条目→改动文件→测试」映射表：缺行时评审必报流程性
# BLOCKER，白烧一轮。AD 在推送前检查：缺表或缺行就把卡退回作者（不建 PR，不进评审，不计评审轮次）。
DELIVERY_MAP_CLASS = "delivery_map"        # 预检失败：退回作者，不计入 lane-failed
DELIVERY_MAP_MISSING = "交付说明缺「验收条目→改动文件→测试」映射表（每行写验收条目、改动文件和测试）"
ACCEPT_SECTION_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*)?(?:验收|Verify|verify|Acceptance)[^:\n：]{0,24}[:：]?[ \t]*$")
ACCEPT_ITEM_RE = re.compile(r"^[ \t]*(?:[-*+][ \t]*)?(?:验收|Verify|verify|Acceptance)[ \t]*[:：][ \t]*(.*)$")
BULLET_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(\S.*)$")
MAP_HEADER_ITEM = ("验收", "acceptance", "条目", "item")
MAP_HEADER_TEST = ("测试", "test", "验证", "verify")
SEPARATOR_CELL_RE = re.compile(r"^:?-{2,}:?$")
PUNCTUATION_RE = re.compile(r"[\s`*_~()\[\]{}<>（）【】、,，.。:：;；!！?？/\\|+\-—–>#]+")

def acceptance_items(body):
    """The acceptance sentences of a card: the `验收：`/`Verify:` lines and the bullets of an acceptance section.

    Each of them needs a row of the mapping table (Refs #1849)."""
    items, section = [], False
    for line in str(body or "").splitlines():
        if ACCEPT_SECTION_RE.match(line): section = True; continue
        if line.lstrip().startswith("#"): section = False; continue
        found = ACCEPT_ITEM_RE.match(line)
        if found and found.group(1).strip(): items.append(found.group(1).strip()); continue
        bullet = BULLET_RE.match(line) if section else None
        if bullet: items.append(bullet.group(1).strip())
        elif section and line.strip(): section = False
    return items

def table_cells(line):
    """The cells of a markdown table row ([] when the line is not a table row)."""
    text = str(line or "").strip()
    return [cell.strip() for cell in text.strip("|").split("|")] if "|" in text else []

def mapping_rows(note):
    """The rows of the 「验收条目→改动文件→测试」tables of a delivery note.

    A table counts when its header names the acceptance column and the test column, the
    next line is the separator and a data row follows; a row is (acceptance, changed
    file, test) and only a row with all three cells counts."""
    rows, lines = [], str(note or "").splitlines()
    for index, line in enumerate(lines):
        header = table_cells(line)
        separator = table_cells(lines[index + 1]) if index + 1 < len(lines) else []
        if len(header) < 3 or not separator or not all(SEPARATOR_CELL_RE.match(c) for c in separator): continue
        def column(keywords):
            return next((i for i, cell in enumerate(header) if any(w in cell.lower() for w in keywords)), None)
        item_col, test_col = column(MAP_HEADER_ITEM), column(MAP_HEADER_TEST)
        if item_col is None or test_col is None or item_col == test_col: continue
        file_col = next((c for c in range(len(header)) if c not in (item_col, test_col)), None)
        if file_col is None: continue
        for row_line in lines[index + 2:]:
            cells = table_cells(row_line)
            if not cells or len(cells) <= max(item_col, file_col, test_col): break
            item, files, test = cells[item_col], cells[file_col], cells[test_col]
            if item and files and test: rows.append((item, files, test))
    return rows

def normalize_text(text):
    """The comparable form of a text: no markdown, no punctuation, no spaces, lowercase."""
    return PUNCTUATION_RE.sub("", str(text or "")).lower()

def row_covers(row, item):
    """Whether a mapping row covers an acceptance sentence: the acceptance column repeats
    it in full; unrelated columns cannot supply coverage."""
    target = normalize_text(item)
    return bool(target) and target == normalize_text(row[0])

def validate_delivery_map(card, note):
    """The missing items of the delivery note's mapping table ([] when the delivery is complete).

    Every acceptance sentence of the card needs a row of the 「验收条目→改动文件→测试」table."""
    rows = mapping_rows(note)
    if not rows: return [DELIVERY_MAP_MISSING]
    return [f"验收条目缺映射行：{item}" for item in acceptance_items(card)
            if not any(row_covers(row, item) for row in rows)]

def delivery_map_missing(lane, n, message):
    """The missing items of the delivery note's mapping table ([] when the delivery is complete).

    The card body is read from GitHub; a failed read is not a delivery defect."""
    rc, out, err = gh(["issue", "view", str(n), "--json", "body"])
    if rc: raise RuntimeError("issue body read failed")
    try: payload = json.loads(out)
    except ValueError: raise RuntimeError("issue body JSON invalid")
    if not isinstance(payload, dict) or not isinstance(payload.get("body"), str):
        raise RuntimeError("issue body missing or invalid")
    return validate_delivery_map(payload["body"], message)

def log(msg, brain=True):
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    with open(f"{D}/autodispatch.log", "a") as f: f.write(line + "\n")
    if brain:
        with open(BRAIN_LOG, "a") as f: f.write(f"- {now()} UTC+8 autodispatch: {msg}\n")

def safe_log(msg, brain=True):
    """log() that never raises: a broken log file must not kill the dispatcher."""
    try:
        log(msg, brain=brain)
    except Exception:
        print(f"{time.strftime('%F %T')} {msg}", flush=True)

def lanes_status_path():
    """The snapshot file the dispatcher publishes and the control plane reads (Refs #1697).

    ``OPENSWARM_LANES_STATUS_FILE`` names the file itself, ``OPENSWARM_AD_STATE_DIR`` the
    directory that holds it.  The reader (``tools/check-lanes-status-stall.py``) resolves the
    very same variables, so a configured deployment reads the file that is actually written."""
    for name in LANES_STATUS_FILE_ENVS:
        explicit = os.environ.get(name)
        if explicit: return os.path.abspath(os.path.expanduser(explicit))
    for name in ("OPENSWARM_AD_STATE_DIR", "XSWARM_AD_STATE_DIR"):
        directory = os.environ.get(name)
        if directory: return os.path.join(os.path.expanduser(directory), "lanes-status.json")
    return os.path.join(D, "lanes-status.json")

def lanes_status_heartbeat_path():
    """The heartbeat of the dispatcher loop: it lives in the state dir, with the other runtime files."""
    return os.path.join(D, "heartbeat")

def write_heartbeat(path=None):
    """Refresh the heartbeat file: the health check reads it to tell a live writer from a stalled one."""
    target = path or lanes_status_heartbeat_path()
    try:
        with open(target, "w") as stream:
            stream.write(time.strftime("%F %T") + "\n")
        return True
    except OSError as error:
        safe_log(f"ERROR heartbeat write failed ({target}): {error!r}", brain=False)
        return False

def write_status_snapshot(path, status, logger=print):
    """Atomically publish the complete snapshot; report failures to the dispatcher log.

    The snapshot is written to a temporary file in the same directory and renamed into place,
    so a reader never sees a half-written file (Refs #1697).  A failed write is logged, never
    swallowed, and the previous snapshot is kept.
    """
    directory = os.path.dirname(path) or "."
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".lanes-status-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(status, stream, ensure_ascii=False, indent=1)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        return True
    except Exception as error:
        message = f"ERROR lanes-status write failed ({path}): {error!r}"
        try:
            logger(message)
        except Exception:
            print(message, flush=True)
        return False
    finally:
        if temporary and os.path.exists(temporary):
            try: os.unlink(temporary)
            except OSError: pass

def sh(cmd, timeout=120, inp=None):
    p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout, input=inp)
    return p.returncode, p.stdout, p.stderr

def load_jump_hosts():
    """Return the host -> ProxyCommand table for jump hosts (Refs #907).

    Sources, in order: $AD_JUMP_HOSTS (JSON object), then <state dir>/jump-hosts.json.
    No jump host is hardcoded in code: the box ssh config owns the routing topology, and an
    injected `-o ProxyCommand=` would override the config's `ProxyJump` for that host.
    """
    raw = os.environ.get(JUMP_HOSTS_ENV) or ""
    if not raw:
        try: raw = open(f"{D}/{JUMP_HOSTS_FILE}").read()
        except OSError: return {}
    try: table = json.loads(raw)
    except ValueError: return {}
    if not isinstance(table, dict): return {}
    return {h: v for h, v in table.items() if isinstance(h, str) and isinstance(v, str) and v.strip()}

JUMP_HOSTS = load_jump_hosts()

def ssh_opts(host=None, jump_hosts=None):
    """Return the SSH option vector for a host, injecting ProxyCommand for configured jump hosts.

    `jump_hosts` defaults to the table loaded from the AD runtime configuration (JUMP_HOSTS).
    """
    opts = list(SSHO)
    table = JUMP_HOSTS if jump_hosts is None else jump_hosts
    proxy = table.get(host) if host else None
    if proxy:
        # A bare jump host becomes `ssh -W %h:%p <host>`; the inner ssh must never prompt, so it
        # inherits BatchMode/ConnectTimeout. A full command string is used verbatim.
        cmd = proxy if (" " in proxy or "%h" in proxy) else f"ssh -o BatchMode=yes -o ConnectTimeout=20 -W %h:%p {proxy}"
        opts.extend(["-o", f"ProxyCommand={cmd}"])
    return opts

def git_ssh_command(ssh_options):
    """Quote the complete SSH option vector for Git's core.sshCommand value."""
    return shlex.join(["ssh", *ssh_options])

def ssh_argv(ssh_options, *args):
    """Recover the same safely quoted SSH command as argv for direct probes."""
    return shlex.split(git_ssh_command(ssh_options)) + list(args)

def load(p, d):
    try: return json.load(open(p))
    except Exception: return d
def save_state(s):
    tmp = f"{D}/state.json.tmp"
    with open(tmp, "w") as stream: json.dump(s, stream, indent=1, ensure_ascii=False)
    os.replace(tmp, f"{D}/state.json")
def gh(args, timeout=60):
    return sh(["gh"] + args + ([] if args[0] == "api" else ["-R", REPO]), timeout=timeout)

# Refs #1772: port the old paginated lookup, without its stale map cache.
def open_pr_for_issue(n, runner=None, repo=None):
    """Return an OPEN PR number, or None; every failed/incomplete read raises."""
    runner = runner or gh; repo = repo or REPO
    owner, name = repo.split("/")
    cursor, seen = "", set()
    while True:
        query = ('query{repository(owner:%s,name:%s){pullRequests(states:OPEN,first:100%s){'
                 'nodes{number state body headRefName closingIssuesReferences(first:100){nodes{number}}}'
                 'pageInfo{hasNextPage endCursor}}}}' %
                 (json.dumps(owner), json.dumps(name), ',after:' + json.dumps(cursor) if cursor else ''))
        # api does not accept -R; the injected runner follows the normal gh contract.
        rc, out, err = runner(["api", "graphql", "-f", "query=" + query])
        if rc: raise RuntimeError("open PR lookup failed")
        data = json.loads(out)
        if data.get("errors"): raise RuntimeError("open PR GraphQL errors")
        connection = data["data"]["repository"]["pullRequests"]
        for pr in connection["nodes"]:
            if pr.get("state") != "OPEN": continue
            linked = {row["number"] for row in pr["closingIssuesReferences"]["nodes"]}
            linked.update(int(m.group(1)) for m in CLOSING_RE.finditer(pr.get("body") or ""))
            linked.update(int(x) for x in re.findall(r"(?:^|[/\-])issue([1-9]\d*)(?=[-/_]|$)", pr.get("headRefName") or ""))
            if int(n) in linked: return pr["number"]
        page = connection["pageInfo"]
        if not page["hasNextPage"]: return None
        cursor = page["endCursor"]
        if not cursor or cursor in seen: raise RuntimeError("open PR pagination did not advance")
        seen.add(cursor)

def pr_allows_requeue(n):
    pr = open_pr_for_issue(n)
    if pr is None: return True
    log(f"skip #{n}: open PR #{pr} already delivers it; not dispatched/requeued", brain=False)
    if not ensure_delivery_state(n): raise RuntimeError("lane-pr repair unverified")
    return False

def clear_workflow_label(n, label):
    rc, _, _ = gh(["issue", "edit", str(n), "--remove-label", label])
    status, detail = read_issue(n)
    if rc or status == "error" or label in label_names(detail):
        raise RuntimeError(f"{label} removal unverified")

def guarded_requeue(n):
    """Fail closed before writes; undo and verify uncertain writes afterwards."""
    try:
        if not pr_allows_requeue(n): return True
        rc, _, _ = gh(["issue", "edit", str(n), "--remove-label", "in-progress",
                       "--remove-label", "lane-failed", "--add-label", "lane-ready"])
        if rc: return False
        try: pr_allows_requeue(n)
        except Exception:
            clear_workflow_label(n, "lane-ready")
            raise
        return True
    except Exception as exc:
        log(f"WARNING #{n} requeue unverified ({exc}); retry next tick", brain=True)
        return False

def cancel_guarded_dispatch(lane, cfg, st):
    """Keep the lane claimed until abort and fresh remote state confirm it stopped."""
    try:
        probe = remote(lane, cfg, st["run"])
        if probe.get("runner_alive") is False and probe.get("lane") == "idle":
            if not pr_allows_requeue(st["issue"]): return True
            clear_workflow_label(st["issue"], "in-progress")
            return True
        rc, out, _ = sh(ssh_argv(ssh_opts(cfg["host"]), cfg["host"],
                              f"{cfg.get('fleet', '~/.local/bin/swrm-fleet')} stop {shlex.quote(st['run'])}"))
        if rc or not json.loads(out).get("signalled"): raise RuntimeError("abort not acknowledged")
        probe = remote(lane, cfg, st["run"])
        if probe.get("runner_alive") is not False or probe.get("lane") != "idle":
            raise RuntimeError("run termination unverified")
        if not pr_allows_requeue(st["issue"]): return True
        clear_workflow_label(st["issue"], "in-progress")
        return True
    except Exception as exc:
        log(f"WARNING #{st['issue']} dispatch cancellation unverified ({exc}); retry next tick", brain=True)
        try:
            clear_workflow_label(st["issue"], "in-progress")
            clear_workflow_label(st["issue"], "lane-ready")
        except Exception as error: log(f"WARNING #{st['issue']} cleanup failed ({error})", brain=True)
        return False

# Ported from refs/old/1774; only workflow labels participate in delivery.
DELIVERY_LABELS = {"lane-ready", "in-progress", "lane-pr"}
DELIVERY_ATTEMPTS = 3

def ensure_delivery_state(n):
    """Verify delivery using fresh gh reads; never replace unrelated labels."""
    def read():
        rc, out, err = gh(["issue", "view", str(n), "--json", "labels"])
        if rc: raise RuntimeError(f"label read rc={rc}")
        payload = json.loads(out)
        if not isinstance(payload.get("labels"), list):
            raise ValueError("missing label list")
        return label_names(payload)

    for attempt in range(1, DELIVERY_ATTEMPTS + 1):
        try:
            labels = read()  # A failed pre-read must not authorize a write.
            if labels & DELIVERY_LABELS == {"lane-pr"}: return True
            rc, _, _ = gh(["issue", "edit", str(n), "--remove-label", "lane-ready",
                           "--remove-label", "in-progress", "--add-label", "lane-pr"])
            if rc: raise RuntimeError(f"label write rc={rc}")
            if read() & DELIVERY_LABELS == {"lane-pr"}: return True
            error = "workflow labels not terminal"
        except Exception as exc:
            error = type(exc).__name__
        log(f"#{n} delivery label attempt {attempt}/{DELIVERY_ATTEMPTS} failed: {error}", brain=False)
    log(f"WARNING #{n} delivery labels unverified after {DELIVERY_ATTEMPTS} attempts; "
        "lane-ready must be cleared; manual repair required", brain=True)
    return False

def label_names(issue):
    return {
        label if isinstance(label, str) else label["name"]
        for label in issue.get("labels", [])
        if isinstance(label, str) or isinstance(label, dict) and "name" in label
    }

def dispatchable_issue(issue):
    labels = label_names(issue)
    return (issue.get("state", "OPEN") == "OPEN" and "lane-ready" in labels
            and not labels & DISPATCH_EXCLUDING_LABELS)

# Shared by the remote reader and writer: resolve on the executor, not on the AD host.
# Legacy state homes remain readable without moving or merging control-plane data.
REMOTE_STATE_HOME = r'''
import os
O = os.environ.get("OPENSWARM_HOME") or os.environ.get("XSWARM_HOME")
if O:
    O = os.path.expanduser(O)
else:
    homes = [os.path.expanduser("~/.claude/" + name) for name in
             ("swrm-executor-candidate", "openswarm-executor-candidate", "swrm", "openswarm")]
    O = next((home for home in homes if os.path.isdir(home)), homes[0])
'''

REMOTE_STATUS = REMOTE_STATE_HOME + r'''
import json,sys
L=sys.argv[1]; R=sys.argv[2] if len(sys.argv)>2 else ""
out={"lane":None,"run":None}
try:
  ls=json.load(open(f"{O}/lanes/{L}/state.json")); out["lane"]=ls.get("state"); out["current_run"]=ls.get("current_run")
  if R and ls.get("current_run")==R: out["last_activity"]=ls.get("last_activity")
except Exception as e: out["lane"]="unknown"
if R:
  p=f"{O}/runs/{R}/result.json"
  if os.path.exists(p):
    with open(p) as stream: r=json.load(stream)
    res=r.get("result") or {}
    # the real end time of the run (Refs #1688): the idle window starts there, not at the
    # harvest, so a run that ended while the dispatcher was down is not counted as fresh
    try:
      with open(f"{O}/runs/{R}/status.json") as stream: rs=json.load(stream)
    except Exception: rs={}
    def stamp(*values):
      # the first value that is a time (a number or a date string): a flag such as
      # `finished: true` is not a time and must not shadow the real stamp
      for value in values:
        if value is None or isinstance(value,bool): continue
        if isinstance(value,(int,float)): return value
        text=str(value).strip()
        if text and text not in ("True","False"): return text
      return None
    ended=(stamp(r.get("finished_at"), r.get("ended_at"), r.get("finished"), r.get("updated_at"),
                rs.get("finished"), rs.get("ended_at"), rs.get("updated_at"))
           or os.path.getmtime(p))
    out["run"]={"status":r.get("status"),"branch":r.get("branch"),"commit":r.get("commit"),"repo":(r.get("workspace") or {}).get("repo"),
      "backend":r.get("backend"),"model":r.get("model"),"error_class":r.get("error_class"),"summary":(str(res.get("summary") or "") if isinstance(res,dict) else str(res or ""))[:1200],
      "tests":(str(res.get("tests") or "") if isinstance(res,dict) else "")[:400],
      "verify_rc":(r.get("verify") or {}).get("rc") if isinstance(r.get("verify"),dict) else None,
      "evidence":r.get("evidence"),
      "ended_at":ended}
  else:
    out["run"]="running"
    try:
      s=json.load(open(f"{O}/runs/{R}/status.json")); pid=s.get("runner_pid"); out["run_state"]=s.get("state")
      if pid:
        try: os.kill(int(pid),0); out["runner_alive"]=True
        except ProcessLookupError: out["runner_alive"]=False
        except PermissionError: out["runner_alive"]=True
    except Exception: out["run_state"]="missing"
print(json.dumps(out))
'''
def remote(lane, cfg, run=""):
    try: rc, out, err = sh(ssh_argv(ssh_opts(cfg["host"]), cfg["host"], "python3", "-", lane, run), timeout=60, inp=REMOTE_STATUS)
    except subprocess.TimeoutExpired: return {"error": "ssh status timeout"}
    if rc: return {"error": (err or out).strip()[-200:]}
    try: return json.loads(out.strip().splitlines()[-1])
    except Exception: return {"error": out[-200:]}

def queue(state=None):
    """Track admission clocks in tick's persisted state, keyed by JSON issue numbers.

    Only a successful, complete pool read replaces the clocks. Missing/invalid cards
    lose their clock so a later admission cannot inherit an earlier pool visit.
    """
    limit = 100
    while True:
        rc, out, err = gh(["issue", "list", "--state", "open", "--label", "lane-ready", "--limit", str(limit), "--json", "number,title,labels,body,createdAt"])
        if rc: raise RuntimeError(err)
        issues = json.loads(out)
        if len(issues) < limit: break
        # gh paginates up to --limit. A full result may omit an older waiting
        # card, so expand until exhaustion before replacing any admission clocks.
        limit *= 2
    state = {} if state is None else state
    entered_at = state.get("_pool_entered_at", {})
    items = []
    for i in issues:
        labels = label_names(i)
        if not dispatchable_issue(i): continue
        pr = min([PRIO[l] for l in labels if l in PRIO] or [3])
        be = next((l.split(":", 1)[1] for l in labels if l.startswith("backend:")), None)
        body = i["body"] or ""
        verify = is_verify_issue(labels, body)
        # a `verify-intent` label without the `## Verify Intent` heading is an invalid card: it stays
        # undispatched. A heading without the label is an ordinary work card (only the label asks for
        # the verify contract, so a normal card that happens to use the heading is not dropped).
        if "verify-intent" in labels and not verify:
            log_verify_rejected(i["number"], ["正文 Verify Intent 段"]); continue
        if verify:
            valid, missing = verify_requirements(body)
            if not valid:
                log_verify_rejected(i["number"], missing); continue
        match = re.search(r"(?im)(?:budget(?:[_ -]minutes)?|预算)\s*[:：]\s*(\d+(?:\.\d+)?)", body)
        budget = float(match.group(1)) if match else float(BUDGET)
        item = {"n": i["number"], "title": i["title"], "body": body, "labels": sorted(labels), "prio": pr, "backend": be, "strict": "strict-backend" in labels, "verify": verify,
                "queued": to_epoch(i.get("createdAt")), "created_at": i.get("createdAt"), "budget_minutes": budget}
        item["deps_ok"] = deps_met(item)
        if item["deps_ok"]:
            entered = timestamp(entered_at.get(str(i["number"])))
            item["pool_entered_at"] = time.time() if entered is None else entered
        items.append(item)
    state["_pool_entered_at"] = {str(item["n"]): item["pool_entered_at"] for item in items if item["deps_ok"]}
    return sorted(items, key=lambda x: (x["prio"], x["n"]))

VERIFY_HEADING_RE = re.compile(r"(?mi)^##[^\S\r\n]*Verify[^\S\r\n]*Intent[^\S\r\n]*$")
def is_verify_issue(labels, body):
    """A verification card carries both markers: the `verify-intent` label and the `## Verify Intent` heading."""
    return "verify-intent" in labels and bool(VERIFY_HEADING_RE.search(body))
def verify_requirements(body):
    """Return (valid, missing) for the required verify-card body fields (Refs #1811).

    `目标 PR` must carry a non-empty value on its own line (a missing value never borrows the value of
    the next line) and `证据要求` must be followed by at least one checkbox item."""
    target = re.search(r"(?mi)^[ \t]*目标 PR[^\S\r\n]*[:：][^\S\r\n]*(?=\S)[^\r\n]*$", body)
    evidence = re.search(r"(?mi)^[ \t]*证据要求[^\S\r\n]*[:：][^\S\r\n]*$(?:\r?\n(?:[ \t]*\r?\n)*[ \t]*[-*][ \t]*\[[ xX]\][^\S\r\n]*\S[^\r\n]*)+", body)
    missing = []
    if not target: missing.append("目标 PR")
    if not evidence: missing.append("证据要求清单")
    return not missing, missing
def log_verify_rejected(n, missing):
    """Log a rejected verify card once per issue and field set (not once per tick)."""
    fields = ", ".join(missing)
    if VERIFY_REJECT_LOGGED.get(n) == fields: return
    VERIFY_REJECT_LOGGED[n] = fields
    log(f"verify #{n} rejected: missing required fields: {fields}", brain=False)

SOCKET_RE = re.compile(r"unix\s*socket|AF_UNIX|socketpair|needs-socket|Unix 套接字|套接字", re.I)
def can_take(backend, item, lane=None, verify_gate=True):
    """Exact pin or unpinned always; cross-pin allowed unless `strict-backend`, except codex never takes Unix-socket work
    (codex sandbox blocks AF_UNIX bind) — so idle agy/codex lanes drain the pi-heavy queue instead of sitting idle.
    A verify card is only eligible for the review lanes CX2/CX4/CX5 (Refs #1811); `verify_gate=False` skips
    that lane rule (the backend-pool starvation count treats a verify card as available work)."""
    if verify_gate and item.get("verify") and not is_review_lane(lane): return False
    if item["backend"] in (None, backend): return True
    if item.get("strict"): return False
    if backend == "codex" and SOCKET_RE.search(item["title"] + "\n" + item["body"]): return False
    return True

DEFAULT_CODEX_MIN_MEMORY_MB = 400

def available_memory_mb(host):
    """Read available memory on a Linux or macOS executor through the normal SSH shim."""
    # vm_stat reports pages, with the host's page size in its header (4K or 16K).
    # Count free/reclaimable pages without adding purgeable pages a second time.
    # Both platform branches emit KiB; a missing/incomplete probe stays unavailable.
    command = """case "$(uname -s)" in
Linux)
    awk '/^MemAvailable:/ {print $2; found=1} END {if (!found) exit 1}' /proc/meminfo ;;
Darwin)
    vm_stat | awk '
        /^Mach Virtual Memory Statistics:/ {
            for (i=1; i<NF; i++) if ($i == "of") pagesize=$(i+1)
        }
        /^(Pages free|Pages inactive|Pages speculative):/ {
            sub(/\\.$/, "", $NF)
            if ($NF !~ /^[0-9]+$/) exit 1
            pages+=$NF; found++
        }
        END {if (pagesize > 0 && found == 3) printf "%.0f\\n", pages*pagesize/1024; else exit 1}' ;;
*) exit 1 ;;
esac"""
    # SSH joins remote arguments with spaces; keep the entire script as one -c argument.
    try: rc, out, _ = sh(ssh_argv(ssh_opts(host), host, "sh", "-lc", shlex.quote(command)), timeout=30)
    except (OSError, subprocess.TimeoutExpired): return None
    if rc: return None
    try: return int(out.strip().splitlines()[-1]) // 1024
    except (ValueError, IndexError): return None

def memory_preflight(lane, cfg, item, cache):
    """Return the codex host's memory rejection detail, or None when it can run."""
    if cfg.get("backend") != "codex": return None
    threshold = cfg.get("min_available_memory_mb", DEFAULT_CODEX_MIN_MEMORY_MB)
    try: threshold = max(0, int(threshold))
    except (TypeError, ValueError): threshold = DEFAULT_CODEX_MIN_MEMORY_MB
    host = cfg["host"]
    if host not in cache: cache[host] = available_memory_mb(host)
    available = cache[host]
    if available is not None and available >= threshold: return None
    return {"host": host, "available_mb": available, "threshold_mb": threshold, "reason": "blocked-by-host-memory"}

def is_review_lane(lane):
    """The review lanes a verify card may be dispatched to (CX2/CX4/CX5)."""
    return bool(re.fullmatch(r"CX(?:2|4|5)(?:[-_].*)?", str(lane), re.I))

# --- pi fix rounds run at the thinking level of their round (Refs #1847) -------------------
# The thinking level a pi fix round runs at, by fix round: the first fix round runs at "high", a
# later round only has to apply the reviewer's notes, so the level steps down; the last entry
# covers every further round. A review card keeps the lane's own level.
FIX_THINKING_LEVELS = ("high", "medium", "low")
# The marker the review flow writes into the issue body when a review asks for changes
# ("Review round N (...) on PR #x head ... returned CHANGES"). It names the fix round a card
# belongs to, so the dispatcher can pick the thinking level of that round (Refs #1847).
FIX_ROUND_RE = re.compile(
    r"^Review round ([1-9]\d*) \([^\r\n]+\) on PR #\d+ head `[^`\r\n]+` "
    r"returned (CHANGES|APPROVED)\.", re.M)

def fix_round_of(body):
    """Use the latest review result; only a CHANGES result requests a fix round."""
    # Only a formal result counts: examples inside fenced code blocks (an unclosed fence runs to the
    # end) and quoted lines are not results of the review. Fences follow CommonMark: the closing
    # fence uses the opening character, is at least as long, and carries only whitespace after it.
    kept, fence = [], None
    for line in (body or "").splitlines():
        if fence is None:
            opening = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if opening and not (opening[1][0] == "`" and "`" in line[opening.end():]):
                fence = opening[1]
                continue
            if not re.match(r"^ {0,3}>", line): kept.append(line)
        elif re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$", line):
            fence = None
    text = "\n".join(kept)
    rounds = FIX_ROUND_RE.findall(text)
    if not rounds or rounds[-1][1] != "CHANGES": return None
    return int(rounds[-1][0])

def fix_thinking(cfg, item):
    """The thinking level a pi fix round runs at, or None (Refs #1847).

    The Nth fix round takes the Nth level of FIX_THINKING_LEVELS (the first fix round runs at
    "high"). Only pi lanes get a level: the other executors have no such flag, and a review card
    keeps the lane's own level, so the review rules stay untouched.
    """
    if (cfg or {}).get("backend") != "pi": return None
    if item.get("verify"): return None
    round_no = fix_round_of(item.get("body") or "")
    if not round_no: return None
    return FIX_THINKING_LEVELS[min(round_no, len(FIX_THINKING_LEVELS)) - 1]


def review_lanes(lanes):
    """The review lanes of a lane set, in the caller's order (CX2/CX4/CX5)."""
    return [lane for lane in lanes if is_review_lane(lane)]

def review_running(lanes, state):
    """The reviews every review lane runs right now (the in-flight part of its load).

    A lane runs one card at a time, so a lane runs at most one review (Refs #1701)."""
    state = state or {}
    return {lane: 1 if (state.get(lane) or {}).get("class") == "verify"
            and not (state.get(lane) or {}).get("review_queued") else 0 for lane in lanes}

def review_in_flight(lanes, state):
    """Real admitted reviews: queued reservations plus reviews that have started."""
    running = review_running(lanes, state)
    queued = state.get("_review_queued") or {}
    return {lane: running[lane] + int(lane in queued) + int(bool((state.get(lane) or {}).get("review_queued"))) for lane in lanes}

def reconcile_review_queue(state, res):
    """Release reservations on start, terminal result, or an idle interrupted dispatch."""
    queued = state.get("_review_queued") or {}
    assigned = {lane: card for lane, card in state.items()
                if isinstance(card, dict) and card.get("review_queued")}
    for lane, card in list(dict(assigned, **queued).items()):
        remote = res.get(lane) or {}
        if "error" in remote: continue
        run = card.get("run")
        if not run and lane in queued and remote.get("lane") == "idle" and not state.get(lane):
            queued.pop(lane, None)
            continue
        if run and (isinstance(remote.get("run"), dict)
                    or (remote.get("run") == "running"
                        and remote.get("current_run") == run)):
            queued.pop(lane, None)
            if lane in assigned: assigned[lane].pop("review_queued", None)


def review_backlog(lanes, state):
    """Running reviews plus the complete waiting queue from the latest tick."""
    state = state or {}
    running = review_in_flight(lanes, state)
    waiting = state.get("_review_waiting") or {}
    return {lane: running[lane] + int(waiting.get(lane, 0) or 0) for lane in lanes}

def pick_reviewer(lanes, loads, max_in_flight=None, log=None, pr=None, eligible=None, rotation=None,
                  in_flight=None):
    """Pick the review lane with the smallest review load (Refs #1701).

    `lanes` is the ordered list of the review lanes (CX2/CX4/CX5); the order is also the
    rotation order of the equal backlogs. `loads` maps a lane to the reviews it carries:
    the reviews it runs plus the reviews already queued for it. The lane with the smallest
    load wins; equal loads rotate over the review lanes. `in_flight` counts reviews
    actually admitted to each lane for the cap check; it defaults to `loads` for plans
    whose queues are already assigned. A forecast of the shared waiting queue affects
    ranking and logging, but must not consume admission capacity. A lane that already
    carries `max_in_flight` admitted reviews is skipped. `eligible` restricts the lanes
    that may take the card (a lane that is busy or whose backend cannot take it).

    The rotation remembers the turn of every review lane (the turn is taken by the lane
    that had it the longest ago), so a lane that is busy for a moment does not break the
    rotation: the reviews keep rotating over the review lanes instead of stacking on one
    lane. The decision is logged with the backlog snapshot (the PR, the chosen lane and
    the load of every review lane), so it can be reviewed afterwards."""
    order = [str(lane) for lane in lanes]
    snapshot = {lane: int((loads or {}).get(lane, 0) or 0) for lane in order}
    admitted = snapshot if in_flight is None else in_flight
    backlog = ", ".join(f"{lane}={snapshot[lane]}" for lane in order)
    allowed = set(order) if eligible is None else {str(lane) for lane in eligible}
    free = [lane for lane in order if lane in allowed
            and (max_in_flight is None or int(admitted.get(lane, 0) or 0) < max_in_flight)]
    if not free:
        if log:
            log(f"review plan: PR #{pr} → lane none (backlog: {backlog}); every free review lane is at "
                f"its cap ({max_in_flight}), the review waits for a free lane")
        return None, snapshot
    smallest = min(snapshot[lane] for lane in free)
    tied = [lane for lane in free if snapshot[lane] == smallest]
    key = tuple(order)
    cursor = REVIEW_ROTATION if rotation is None else rotation
    turns = cursor.setdefault(key, {"count": 0, "last": {}})
    # the tied lane that had its turn the longest ago (a lane that never had one comes
    # first): that is the rotation over the review lanes
    chosen = min(tied, key=lambda lane: (turns["last"].get(lane, -1), order.index(lane)))
    # Every selection consumes a turn, including a unique minimum. Otherwise that
    # lane can immediately win the next tie after finishing the current round.
    turns["count"] += 1
    turns["last"][chosen] = turns["count"]
    if log:
        log(f"review plan: PR #{pr} → lane {chosen} (backlog: {backlog})")
    return chosen, snapshot

def plan_reviews(idle_review_ids, pending, running, max_in_flight=None, lane_cfgs=None, log=None,
                 review_ids=None):
    """Plan the review cards over the review lanes by load (Refs #1701).

    `running` maps a review lane to the reviews it runs right now; the load of a lane is
    that number plus the cards the plan already gave it (its review queue). The cards
    are taken by the lane with the smallest load, equal loads rotate. The rotation walks
    `review_ids` (all the review lanes, the ones that are busy included), so the reviews
    keep rotating over the review lanes.

    Returns (plan, snapshots): the plan maps a review lane to the cards it takes (the
    first card is dispatched first), the snapshots hold the load at the moment of the
    decision (for the log). A lane at its cap is skipped: the review waits for a free
    lane."""
    order = [str(lane) for lane in (review_ids or idle_review_ids)]
    plan, snapshots, counts = {}, {}, dict(running)
    for card in pending:
        eligible = [lane for lane in idle_review_ids
                    if lane_cfgs is None or can_take(lane_cfgs[lane].get("backend"), card, lane)]
        if not eligible: continue
        lane, snapshot = pick_reviewer(order, counts, max_in_flight=max_in_flight,
                                      eligible=eligible, log=log, pr=review_target_pr(card))
        if lane is None:
            # every free review lane is at its cap: the review waits for a free lane
            # (pick_reviewer logged the decision with the backlog snapshot)
            continue
        plan.setdefault(lane, []).append(card)
        snapshots[card["n"]] = snapshot
        counts[lane] = counts.get(lane, 0) + 1
    return plan, snapshots

def review_waiting_counts(review_ids, pending, running, max_in_flight=None, head_eligible=None):
    """Spread the entire waiting queue for backlog accounting, without admitting it.

    The in-flight cap limits admission, not visibility: even when every lane is at
    capacity, every waiting card must appear in the backlog. This forecast uses a
    private copy of the current cursor so counting cannot consume an actual
    assignment's turn. During admission, reserve the queue head's prospective turn
    before forecasting the other waiting cards; the head itself is not counted in
    the returned queue. This keeps a fresh forecast from biasing every dispatch
    against the first lane in the rotation.
    """
    counts, current = {}, dict(running)
    rotation = {key: {"count": turns["count"], "last": dict(turns["last"])}
                for key, turns in REVIEW_ROTATION.items()}
    if head_eligible is not None:
        head, _ = pick_reviewer(review_ids, current, eligible=head_eligible, rotation=rotation)
        if head is not None: current[head] = current.get(head, 0) + 1
    for card in pending:
        lane, _ = pick_reviewer(review_ids, current, rotation=rotation)
        if lane is None: break
        counts[lane] = counts.get(lane, 0) + 1
        current[lane] = current.get(lane, 0) + 1
    return counts

def review_target_pr(card):
    """Read the target PR, never substitute the verification issue number."""
    target = re.search(r"(?mi)^[ \t]*目标 PR[^\S\r\n]*[:：][ \t]*([^\r\n]+)", card.get("body", ""))
    if not target: return "unknown"
    number = re.search(r"(?:#|/pull/)([0-9]+)\b", target[1])
    if not number: number = re.fullmatch(r"[ \t]*([0-9]+)[ \t]*", target[1])
    return number[1] if number else "unknown"

def idle_lane_names(lanes, state, res, scopes, freed=None):
    """The lanes that can take a card right now (healthy, not paused and idle).

    A lane that was freed by a finished run in this tick is available even if the remote
    snapshot still calls it busy (Refs #1688)."""
    freed = freed or ()
    return [name for name, c in lanes.items() if not state.get(name) and "error" not in res[name]
            and (name in freed or res[name].get("lane") == "idle")
            and host_ok(c["host"]) and not lane_paused(name, c, scopes)]
def rank(backend, item):
    """Highest score first; stable legacy fields break equal-score ties."""
    score, _ = priority_score(backend, item)
    return (-score, item["prio"], bool(item.get("verify")), item["n"])

def queue_class_limits(lanes, state, idle):
    """Reserve review-capable capacity using available lanes and active runs only."""
    active = [L for L in lanes if L in idle or state.get(L)]
    total = len(active)
    review = sum(is_review_lane(L) for L in active)
    reserve = min(review, max(0, total - 1), max(0, VERIFY_MIN, round(total * max(0.0, VERIFY_SHARE))))
    verify_busy = sum(1 for L in active if (state.get(L) or {}).get("class") == "verify")
    work_busy = sum(1 for L in active if state.get(L) and (state[L].get("class") != "verify"))
    return max(0, reserve - verify_busy), max(0, total - reserve - work_busy)

VERIFY_METRICS_KEY = "_verify_metrics"
VERIFY_HISTOGRAM_BUCKETS = (60, 300, 900, 1800, 3600)

def verify_metrics(state):
    """Persistent measurements for verify cards and their dispatch-share window."""
    metrics = state.setdefault(VERIFY_METRICS_KEY, {"cards": {}, "dispatches": {"verify": 0, "all": 0},
                                                     "work_cap_hits": 0})
    metrics.setdefault("cards", {})
    metrics.setdefault("dispatches", {}).setdefault("verify", 0)
    metrics.setdefault("dispatches", {}).setdefault("all", 0)
    metrics.setdefault("work_cap_hits", 0)
    return metrics

def verify_record(state, issue):
    return verify_metrics(state)["cards"].get(str(issue))

def record_verify_time(state, issue, point, at=None):
    record = verify_record(state, issue)
    if record is not None:
        record.setdefault(point, time.time() if at is None else at)

def verify_histogram(state, point):
    """Histogram durations from queued to dispatched or verdict."""
    counts = {}
    edges = (0,) + VERIFY_HISTOGRAM_BUCKETS
    for low, high in zip(edges, edges[1:]): counts[f"{low}-{high}"] = 0
    counts[f"{VERIFY_HISTOGRAM_BUCKETS[-1]}+"] = 0
    for record in verify_metrics(state)["cards"].values():
        end = record.get(point)
        start = record.get("queued")
        if end is None or start is None: continue
        duration = max(0.0, float(end) - float(start))
        bucket = next((f"{low}-{high}" for low, high in zip(edges, edges[1:])
                       if low <= duration < high), f"{VERIFY_HISTOGRAM_BUCKETS[-1]}+")
        counts[bucket] += 1
    return {key: value for key, value in counts.items() if value}

def observe_verify_verdicts(state):
    """Poll tracked cards, including harvested cards no longer represented by a lane."""
    metrics = verify_metrics(state)
    for issue, record in metrics["cards"].items():
        if record.get("verdict") is not None: continue
        try:
            rc, output, _ = gh(["issue", "view", str(issue), "--json", "state,labels"])
            if rc: continue
            card = json.loads(output)
            labels = label_names(card)
            if card.get("state") == "CLOSED" or any(label == "verdict" or label.startswith("verdict:") for label in labels):
                record["verdict"] = time.time()
        except (OSError, ValueError, TypeError):
            continue

def age_s(st):
    try: return time.time() - time.mktime(time.strptime(st["at"], "%Y-%m-%d %H:%M:%S"))
    except Exception: return 1e9

def requeue_lost_run(L, st, r, state, why="orphaned"):
    """Re-queue the card of a run that was lost (the safe orphan path).

    The labels of the issue and the lane state are updated together: when the label
    update fails, the lane state is kept, so the next tick retries instead of losing
    the card (Refs #1673)."""
    n = st["issue"]
    rc, out, _ = gh(["issue", "view", str(n), "--json", "state,labels"])
    if rc: return False
    try: j = json.loads(out); labels = {l["name"] for l in j["labels"]}
    except Exception: return False
    if j["state"] == "OPEN" and "lane-pr" not in labels:
        if not guarded_requeue(n): return False
        what = "requeued (lane-ready)"
    else: what = f"already {j['state'].lower()}/{'lane-pr' if 'lane-pr' in labels else '-'}; dropped"
    log(f"{L} #{n} run {st['run']} {why} (remote lane {r.get('lane')}, current_run={r.get('current_run')}, run_state={r.get('run_state')}, runner dead) → {what}")
    return True

def reconcile(L, cfg, st, r, state):
    """Local state says busy but the remote lane is idle on another/no run, the run has no result.json and its runner is dead:
    the run was orphaned (lane restart / reassignment). Drop it; requeue the issue unless it already got a PR or closed."""
    if r.get("run") != "running" or r.get("lane") != "idle" or r.get("current_run") == st["run"]: return False
    # Missing status is not evidence that the runner exited. Keep the lane claimed
    # until the remote probe positively reports a dead runner (Refs #1673).
    if r.get("runner_alive") is not False or age_s(st) < 180: return False
    return requeue_lost_run(L, st, r, state, "orphaned")

def verify_recovered_run(L, cfg, st, r, state):
    """Verify the run of a lane whose host came back; clean it up when it is lost.

    The runner of the run decides: a live runner means the run continues (the
    ordinary harvest picks it up when it ends), a dead runner means the run was lost
    with the outage and its card is re-queued through the orphan path.  An unknown
    liveness keeps the lane and its card: the runner may still be running, and a
    second dispatch would run the same work twice (Refs #1673).

    Returns True when the run was cleaned up (the lane slot is free again)."""
    pending = recovery_runs(state)
    if L not in pending: return False
    liveness = runner_liveness(r)
    if liveness == "alive":
        pending.pop(L, None)
        log(f"{L} run {st['run']} survived the outage of {cfg['host']}: the runner is alive, the run continues", brain=False)
        return False
    if liveness == "unknown":
        log(f"{L} run {st['run']}: the liveness of the runner is unknown, the lane keeps its card until the host can tell", brain=False)
        return False
    if age_s(st) < 180: return False  # a young run: the host may still be starting it
    if not requeue_lost_run(L, st, r, state, "lost with the outage of the host"): return False
    pending.pop(L, None)
    return True

def verify_of(body):
    for line in reversed(body.splitlines()):
        if re.match(r"\s*(验收|Verify|verify|Acceptance)\s*[:：]", line):
            cmds = re.findall(r"`([^`]+)`", line)
            if cmds: return " && ".join(cmds) if len(cmds) > 1 and all(not c.startswith("cd ") for c in cmds[1:]) else cmds[0]
    return None

def read_issue(n):
    """One authoritative read of an issue; returns (status, payload).

    `status` is "fresh" (labels read, still dispatchable), "stale" (labels read, no longer
    dispatchable) or "error" (the read failed). A failed read is NOT stale: the caller must
    keep the row, otherwise a transient `gh` error shrinks `_ready` to a false 0."""
    try:
        rc, out, err = gh(["issue", "view", str(n), "--json", "state,labels"])
    except Exception as e:
        return "error", f"gh raised {e!r}"
    if rc: return "error", f"gh rc={rc}: {(err or out).strip()[-100:]}"
    try:
        issue = json.loads(out)
        fresh = dispatchable_issue(issue)
    except Exception: return "error", "unparsable `gh issue view` output"
    # labels from `gh issue view` are authoritative; `_done` alone no longer blocks (a re-labelled lane-ready issue is retried)
    return ("fresh" if fresh else "stale"), issue

def label_cache_reset():
    """Forget the authoritative reads of the previous tick (one read per issue per tick)."""
    global LABEL_READS_N
    LABEL_CACHE.clear(); LABEL_READS_N = 0; READ_ERROR_LOGGED.clear()

def label_state(n, cache=None):
    """Authoritative readiness of one issue, cached per tick by issue number.

    Returns (status, payload): "fresh" (dispatchable), "stale" (claimed/done), "error" (the
    authoritative read failed — the issue is NOT stale) or "budget" (the per-tick read budget
    was exhausted, so the issue was not read at all)."""
    cache = LABEL_CACHE if cache is None else cache
    if n in cache: return cache[n]
    global LABEL_READS_N
    if LABEL_READS_N >= FRESH_READ_BUDGET:
        res = ("budget", f"per-tick read budget ({FRESH_READ_BUDGET}) exhausted")
    else:
        LABEL_READS_N += 1
        res = read_issue(n)
    cache[n] = res
    return res

def prefetch_labels(numbers, cache=None):
    """Read the authoritative labels of `numbers` in parallel and cache them for this tick.

    The budget is reserved before the parallel reads start, so the reads of one tick can
    never exceed it, and the same issue is never read twice in a tick."""
    cache = LABEL_CACHE if cache is None else cache
    todo = []
    for n in numbers:
        if n in cache or n in todo: continue
        todo.append(n)
    if not todo: return
    global LABEL_READS_N
    room = max(0, FRESH_READ_BUDGET - LABEL_READS_N)
    LABEL_READS_N += min(room, len(todo))
    todo = todo[:room]
    if not todo: return
    with cf.ThreadPoolExecutor(4) as ex:
        for n, res in zip(todo, ex.map(read_issue, todo)): cache[n] = res

def fresh_ready(n, state=None):
    """`gh issue list --label` goes through the search index, which lags label edits; re-read the issue directly.

    True only when the authoritative labels say dispatchable; a stale or unreadable issue is False."""
    return label_state(n)[0] == "fresh"

def unverified_reason(status, detail):
    """Log wording for a row that could not be verified authoritatively (never "stale search index")."""
    if status == "error": return f"label read failed ({detail})"
    if status == "budget": return f"label read skipped ({detail})"
    return f"label read problem ({status}: {detail})"

def log_unverified(n, status, detail, suffix):
    """Log an unverifiable row once per issue and tick (several lanes may retry the same issue)."""
    if n in READ_ERROR_LOGGED: return
    READ_ERROR_LOGGED.add(n)
    log(f"skip #{n}: {unverified_reason(status, detail)}; {suffix}", brain=False)

def issue_labels(n):
    """Authoritative labels of an issue as re-read by `gh issue view` (empty set when unreadable)."""
    rc, out, _ = gh(["issue", "view", str(n), "--json", "state,labels"])
    if rc: return set()
    try: return label_names(json.loads(out))
    except (TypeError, ValueError, KeyError): return set()

def note_stale_skip(lane_failed_residual=False):
    """Count a stale search hit; a `lane-failed` + `lane-ready` residual is counted separately (Refs #985)."""
    global STALE_STRIPPED_N, STALE_LANE_FAILED_N
    STALE_STRIPPED_N += 1
    if lane_failed_residual: STALE_LANE_FAILED_N += 1

def skip_stale(n, payload):
    """Count and log a stale search row; `payload` is the authoritative issue that proved it stale."""
    labels = label_names(payload) if isinstance(payload, dict) else set()
    residual = "lane-failed" in labels and "lane-ready" in labels
    note_stale_skip(residual)
    if residual:
        log(f"skip #{n}: stale search hit lane-failed + lane-ready; remove lane-ready label to clear the residual", brain=False)
    else:
        log(f"skip #{n}: stale search index (already claimed/done)", brain=False)

def freshen_queue(q, state, cache=None):
    """Drop search-index rows that are no longer dispatchable before counting them.

    `gh issue list --label lane-ready` answers from the search index, so a row can still
    look `lane-ready` while the issue itself carries `lane-failed`/`lane-pr`/`in-progress`.
    Every row is re-read authoritatively (once per tick, shared with the dispatch
    candidates); stale rows are skipped (and logged) so that the `_ready` count in
    lanes-status.json matches the really dispatchable issues. A row whose read failed is
    KEPT and logged as a read error: it may still be dispatchable, and a transient `gh`
    error must never shrink `_ready` to a false 0 (nor trigger a needless refill).

    `state` is accepted for call-site compatibility; the authoritative labels decide."""
    cache = LABEL_CACHE if cache is None else cache
    prefetch_labels([item["n"] for item in q], cache)
    fresh = []
    for item in q:
        status, detail = label_state(item["n"], cache)
        if status == "fresh": fresh.append(item)
        elif status == "stale":
            skip_stale(item["n"], detail)
        else:
            log_unverified(item["n"], status, detail, "kept in queue (not counted as stale)")
            fresh.append(item)
    return fresh

def update_busy_stale(lanes, state, res):
    """Retain review markers independently of run tracking, including offline hosts.

    `updated` and dispatch age are deliberately not activity. Legacy runs get one
    persisted observation window when no trustworthy activity has been seen yet.
    """
    now = time.time()
    threshold = env_number("AD_BUSY_STALE_MINUTES", 30) * 60
    if not math.isfinite(threshold) or threshold < 0: threshold = 30 * 60
    for lane in lanes:
        st = state.get(lane)
        if not st: continue
        st.setdefault("activity_observed_since", now)
        r = res.get(lane, {})
        activity = r.get("last_activity") if r.get("current_run") == st["run"] else None
        if (isinstance(activity, (int, float)) and not isinstance(activity, bool)
                and math.isfinite(activity) and activity > 0):
            # Remember the raw host reading: an unchanged future clock reading
            # must not become fresh activity every time it is polled.
            source = st.get("activity_source", 0)
            if activity != source and (activity > source or st.get("last_activity", 0) < activity <= now):
                st["activity_source"] = activity
                st["last_activity"] = max(st.get("last_activity", 0), min(activity, now))
        last = st.get("last_activity", st["activity_observed_since"])
        if now - last > threshold and "stale_since" not in st:
            st["stale_since"] = now
            log(f"{lane} #{st['issue']} run {st['run']} stale: no trusted activity; review required", brain=False)
    save_state(state)

def minimax_burn_path():
    """The path of the MiniMax burn snapshot the status surface shows (Refs #1671)."""
    return os.environ.get("MM_BURN_STATUS") or os.path.join(D, "minimax-burn.json")

def minimax_burn_tool():
    """The burn watchdog of the repository: ``tools/mm-burn-rate.py``.

    The dispatcher lives in ``tools/lane-autodispatch/`` and the watchdog one
    directory above it (``tools/``), so the watchdog is found without any
    configuration (Refs #1671).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (os.path.join(os.path.dirname(here), "mm-burn-rate.py"),
                  os.path.join(here, "mm-burn-rate.py"))
    for candidate in candidates:
        if os.path.exists(candidate): return candidate
    return candidates[0]

def minimax_burn_health():
    """The health snapshot of the box, when it carries the MiniMax quota object.

    The quota history of the burn watchdog is fed from the health snapshot. A
    snapshot without a quota object is not fed at all: the box health loop
    probes the hosts, and a missing quota is not an error of every tick.
    """
    path = os.path.join(D, "health.json")
    try:
        with open(path, encoding="utf-8") as stream:
            health = json.load(stream)
    except (OSError, ValueError, TypeError):
        return None
    return path if isinstance(health, dict) and "minimax" in health else None

def minimax_burn_command():
    """The command that refreshes the MiniMax burn snapshot (Refs #1671).

    ``MM_BURN_CMD`` overrides it. The default runs the burn watchdog of the
    repository: it reads the MiniMax provider request log (the log of the
    running broker, found without configuration) and writes the snapshot the
    lane status shows. Without the watchdog on the box there is no reading and
    the status stays as it was.
    """
    override = os.environ.get("MM_BURN_CMD")
    if override: return shlex.split(override)
    tool = os.environ.get("MM_BURN_TOOL") or minimax_burn_tool()
    if not os.path.exists(tool): return []
    cmd = [sys.executable, tool, "--history", os.path.join(D, "minimax-burn-history.jsonl"),
           "--status-file", minimax_burn_path()]
    broker = os.environ.get("MM_BROKER_LOG") or os.environ.get("PI_BROKER_LOG")
    if broker: cmd += ["--broker-log", broker]
    health = minimax_burn_health()
    if health: cmd += ["--append", health]
    return cmd

def refresh_minimax_burn():
    """Refresh the MiniMax burn snapshot the lane status shows (Refs #1671).

    The watchdog exits 1 while it alerts (a collapsed token burn) and 2 when
    the quota collection is broken; both are logged, so the dispatcher log
    carries the burn alert too. A failing watchdog never stops the dispatcher.
    """
    global BURN_WARN_AT
    cmd = minimax_burn_command()
    if not cmd: return
    try: rc, out, err = sh(cmd, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"minimax burn watchdog failed: {e!r}", brain=False); return
    if not rc: return
    if time.time() - BURN_WARN_AT < MM_BURN_WARN_SEC: return
    BURN_WARN_AT = time.time()
    log(f"minimax burn watchdog rc={rc}: {(err or out).strip()[-160:]}", brain=False)

def minimax_burn_status(now=None, max_age_s=MM_BURN_MAX_AGE_S):
    """The MiniMax burn reading of the watchdog snapshot (Refs #1671).

    The snapshot is only shown while the reading is fresh: the watchdog writes
    ``token_observed_at`` (the moment the reading was taken). A snapshot whose
    reading is older than the freshness window is dropped, so a stopped
    watchdog cannot pass the last known burn off as the current one.
    ``token_sample_at`` (the newest MiniMax provider request the reading is
    based on) is reported with it; a genuine zero burn has no request at all
    and is still a reading of this hour.
    A snapshot without a reading is a collection failure (the collector is
    gone, the log lost rows): the state is shown, but no token count -- a
    broken collection is never read as a fleet that burned nothing.
    """
    try:
        with open(minimax_burn_path(), encoding="utf-8") as stream:
            burn = json.load(stream)
    except (OSError, ValueError, TypeError):
        return {}  # a missing or malformed snapshot must not break the dispatch
    if not isinstance(burn, dict): return {}
    stamp = burn.get("token_observed_at")
    if not isinstance(stamp, str): return {}
    try: observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError: return {}
    if observed.tzinfo is None: observed = observed.replace(tzinfo=timezone.utc)
    age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
    if age < -60 or age > max_age_s: return {}
    fields = {}
    tokens = burn.get("hourly_tokens")
    if burn.get("token_state") in ("ok", "idle") and not isinstance(tokens, bool) and isinstance(tokens, (int, float)):
        fields["_minimax_hourly_tokens"] = int(tokens)
        fields["_minimax_token_alert"] = bool(burn.get("token_alert"))
        fields["_minimax_usage_at"] = burn.get("token_sample_at") or stamp
        fields["_minimax_token_state"] = str(burn.get("token_state") or "unknown")
    elif burn.get("token_state"):
        # A collection failure is no reading, but it is not a zero burn either:
        # the state says which one it is.
        fields["_minimax_token_state"] = str(burn.get("token_state"))
    collector = burn.get("token_collector")
    if isinstance(collector, str):
        # "alive" is a verified reading, the other states are collection failures.
        fields["_minimax_collector_state"] = collector
    interval = burn.get("interval_tokens")
    if not isinstance(interval, bool) and isinstance(interval, (int, float)):
        # The total of the current Token Plan window: the figure the MiniMax
        # console shows for the current plan (Refs #1671).
        fields["_minimax_interval_tokens"] = int(interval)
        requests = burn.get("interval_requests")
        if not isinstance(requests, bool) and isinstance(requests, (int, float)):
            fields["_minimax_interval_requests"] = int(requests)
        reset = burn.get("interval_reset_at")
        if isinstance(reset, str):
            fields["_minimax_interval_reset_at"] = reset
    buckets = burn.get("hourly_buckets")
    if isinstance(buckets, list):
        # The clock hours of the MiniMax console: an operator samples the console
        # and compares one hour with it (Refs #1671).
        rows = []
        for bucket in buckets:
            if not isinstance(bucket, dict): continue
            hour = bucket.get("hour")
            total = bucket.get("tokens")
            if not isinstance(hour, str): continue
            if isinstance(total, bool) or not isinstance(total, (int, float)): continue
            rows.append({"hour": hour, "tokens": int(total)})
        if rows: fields["_minimax_hourly_buckets"] = rows
    return fields

# Shared by the backlog mint and dispatch decisions (Refs #1721).
_HOT_DIR = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("ad_hotfiles", os.path.join(_HOT_DIR, "hotfiles.py"))
HOTFILES = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(HOTFILES)
with open(os.path.join(_HOT_DIR, "hot_policy.json"), encoding="utf-8") as _stream:
    _hot_policy = json.load(_stream)
HOT_LOCK = HOTFILES.HotLock(
    dict(_hot_policy["hot_shared"], **{f: _hot_policy["hot_default"] for f in _hot_policy["hot_bare"]}),
    _hot_policy["hot_default"])
HOT_UNION = set(HOTFILES.parse_hot_files("热文件：" + " ".join(_hot_policy["hot_union"])))


def hot_files(body):
    return [f for f in HOTFILES.parse_hot_files(body) if f not in HOT_UNION and not HOTFILES.is_doc_file(f)]


def hot_inflight(state):
    counts = {}
    for lane, entry in (state or {}).items():
        if lane.startswith("_") or not isinstance(entry, dict) or not entry.get("issue"): continue
        # Runs dispatched before this feature have no declared files in state.
        if not isinstance(entry.get("hot"), list) or not all(isinstance(f, str) for f in entry["hot"]):
            counts["__unknown__"] = 1
            continue
        for path in set(hot_files("热文件：" + " ".join(entry["hot"]))):
            counts[path] = counts.get(path, 0) + 1
    return counts


def hot_verdict(body, counts):
    files = sorted(hot_files(body))
    verdict = HOT_LOCK.preflight(files, counts)
    if files and counts.get("__unknown__"):
        verdict["blocked"] = True
        verdict["unknown_inflight"] = True
        verdict["advice"] = "在途热文件未知，等待或核实后重试"
    return verdict


def blocked_details(q, hot_blocked, memory_blocks, state):
    now = time.time()
    since = state.setdefault("_blocked_since", {})
    if not isinstance(since, dict): since = {}; state["_blocked_since"] = since
    details = []
    for item in q:
        n = item["n"]
        if not item.get("deps_ok", True): reason = "deps"; files = []
        elif n in hot_blocked:
            reason = "hot_file"; files = [c["file"] for c in hot_blocked[n]["conflicts"]]
            if hot_blocked[n].get("unknown_inflight"): files = hot_blocked[n]["files"]
        elif n in memory_blocks: reason = "memory"; files = []
        else: continue
        key = str(n)
        try: first = float(since[key])
        except (KeyError, TypeError, ValueError): first = now; since[key] = first
        details.append({"issue": n, "reason": reason, "files": files,
                        "unknown_inflight": bool(hot_blocked.get(n, {}).get("unknown_inflight")),
                        "wait_seconds": max(0, int(now - first))})
    active = {str(d["issue"]) for d in details}
    for key in list(since):
        if key not in active: since.pop(key, None)
    return details


def status_snapshot(lanes, state, res, ready, blocked, records=None):
    """Build the observable lane status row, including this process's stale skips."""
    lanes = lanes or {}
    status = {L: (("stale #%s (review required)" if "stale_since" in state[L] else "busy #%s") % state[L]["issue"]) if state.get(L) else (res[L].get("error") or res[L].get("lane") or "?") for L in lanes}
    status["_busy"] = sum(1 for L in lanes if state.get(L) and "stale_since" not in state[L])
    status["_stale"] = sum(1 for L in lanes if state.get(L) and "stale_since" in state[L])
    status["_ready"] = ready
    status["_blocked"] = blocked
    status["_review_backlog"] = review_backlog(review_lanes(lanes), state)
    status["_queue_failures"] = int(state.get("_queue_failures", 0) or 0)
    status["_minted"] = int(state.get("_minted", 0) or 0)
    status["_pauses"] = pause_reasons(lanes, records)  # which switch holds every paused lane (Refs #1592)
    status["_stale_stripped_n"] = STALE_STRIPPED_N
    status["_stale_search_skips"] = STALE_STRIPPED_N
    status["_stale_lane_failed_skips"] = STALE_LANE_FAILED_N
    status["_fetch_failures"] = dispatch_fetch_health(lanes)
    idle_s, idle_reason, idle_open_s = idle_metrics(state)
    status["_idle_s"] = idle_s              # the last idle gap of every lane (run end -> dispatch)
    status["_idle_reason"] = idle_reason    # why that gap was long
    status["_idle_open_s"] = idle_open_s    # the gap the idle lanes are in right now
    status["_idle_worst_s"] = max(idle_open_s.values(), default=0.0)
    status["_at"] = time.strftime("%F %T")
    # The phase and the tick start tell the reader how far the current tick got (Refs #1697):
    # a snapshot whose phase does not change while `_at` advances means a slow step is running.
    # The publisher names the phase of the running tick; outside a tick the snapshot is idle.
    status["_phase"] = "idle"
    status["_tick_at"] = status["_at"]
    # The burn watchdog measures the real MiniMax token burn of the hour; the
    # lane status shows its reading, so an operator compares it with the
    # MiniMax console instead of watching the dispatch count (Refs #1671).
    status.update(minimax_burn_status())
    metrics = verify_metrics(state)
    status["_verify_wait_histogram"] = verify_histogram(state, "dispatched")
    status["_verify_cycle_histogram"] = verify_histogram(state, "verdict")
    status["_verify_deficit"] = metrics.get("deficit", 0)
    dispatches = metrics["dispatches"]
    status["_verify_share"] = (dispatches["verify"] / dispatches["all"] if dispatches["all"] else 0.0)
    status["_work_cap_hits"] = metrics["work_cap_hits"]
    return status

class StatusPublisher:
    """Publishes the lanes-status snapshot and refreshes `_at` on every publish.

    The snapshot used to be written once, at the very end of the tick, so a slow harvest, a
    hung `gh` call or a long refill froze `_at` while the dispatcher kept dispatching
    (Refs #1697).  The publisher is called at every step of the tick and a refresher thread
    republishes it while a slow step runs, so `_at` follows the dispatch rhythm; the queue
    counters keep their last known value until the current tick measured them again,
    then every successful assignment updates the busy and ready counts together.
    """

    def __init__(self, lanes, state, path=None, logger=None, heartbeat=None, refresh=None):
        self.lanes = lanes
        self.state = state
        self.path = path or lanes_status_path()
        self.heartbeat = heartbeat or lanes_status_heartbeat_path()
        self.logger = logger or safe_log
        self.res = {}
        self.records = None
        self.ready = self.previous("_ready")
        self.blocked = self.previous("_blocked")
        self.memory_blocks = {}
        self.blocked_details = None
        self.rows = self.previous_rows()
        self.refresh = STATUS_REFRESH_SECONDS if refresh is None else float(refresh)
        self.phase = "start"
        self.tick_at = None  # the stamp of the first publish: the start of the running tick
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    def previous(self, key):
        """The counter of the last published snapshot (an early publish must not report a false 0)."""
        try:
            with open(self.path, encoding="utf-8") as stream:
                published = json.load(stream)
        except (OSError, ValueError):
            return None
        value = published.get(key) if isinstance(published, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def previous_rows(self):
        """The lane rows of the last published snapshot (a publish before the probe keeps them)."""
        try:
            with open(self.path, encoding="utf-8") as stream:
                published = json.load(stream)
        except (OSError, ValueError):
            return {}
        if not isinstance(published, dict): return {}
        return {lane: row for lane, row in published.items()
                if not lane.startswith("_") and isinstance(row, str)}

    def snapshot(self, res=None, ready=None, blocked=None):
        """The snapshot body with the latest known values."""
        if res is not None: self.res = res
        if ready is not None: self.ready = ready
        if blocked is not None: self.blocked = blocked
        rows = {L: self.res.get(L) or {"lane": self.rows.get(L, "?")} for L in self.lanes}
        # status_snapshot() keeps the argument shape it always had: a caller that replaces it
        # (the tick tests do) must keep working. The phase of the tick is added below.
        snapshot = status_snapshot(self.lanes, self.state, rows, self.ready or 0, self.blocked or 0,
                                   self.records)
        if isinstance(snapshot, dict):
            snapshot["_memory_blocks"] = self.memory_blocks
            if self.blocked_details is not None:
                snapshot["_blocked_details"] = self.blocked_details
                snapshot["_blocked_reasons"] = {reason: sum(d["reason"] == reason for d in self.blocked_details)
                                                for reason in ("deps", "hot_file", "memory")}
            # The phase and the tick start tell the reader how far the current tick got
            # (Refs #1697): a snapshot whose phase does not change while `_at` advances
            # means a slow step is running.
            if self.tick_at is None: self.tick_at = snapshot.get("_at")
            snapshot["_phase"] = self.phase
            snapshot["_tick_at"] = self.tick_at
        return snapshot

    def publish(self, res=None, ready=None, blocked=None, phase=None, heartbeat=True):
        """Write the snapshot now; a failed write is logged, never swallowed.

        The publish is serialized: the refresher thread and the tick itself must never
        overwrite a newer snapshot with an older one (Refs #1697)."""
        with self._lock:
            if phase is not None: self.phase = phase
            snapshot = self.snapshot(res=res, ready=ready, blocked=blocked)
            write_status_snapshot(self.path, snapshot, self.logger)
            if heartbeat: write_heartbeat(self.heartbeat)
            return snapshot

    def start(self):
        """Republish the snapshot while the tick works: a slow step must not freeze `_at`.

        The refresher does not touch the heartbeat: the heartbeat is the signal that the
        dispatcher loop itself is alive, so a tick stuck in one operation is still reported
        as a stalled writer by the health check (Refs #1697)."""
        if self.refresh <= 0 or self._thread is not None: return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._refresh, args=(self.refresh,),
                                        name="lanes-status-refresh", daemon=True)
        self._thread.start()
        return self

    def record_dispatch(self, lane, record):
        """Publish an assignment and its queue decrement before any slow save or logging.

        The refresher shares this lock, so it cannot publish a new busy lane with the
        ready count from before the assignment.
        """
        with self._lock:
            self.state[lane] = record
            return self.publish(ready=max(0, (self.ready or 0) - 1), phase="dispatch")

    def _refresh(self, every):
        while not self._stop.wait(every):
            try:
                self.publish(heartbeat=False)
            except Exception as error:
                safe_log(f"ERROR lanes-status refresh failed: {error!r}", brain=False)

    def stop(self):
        """Stop the refresher thread."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None: thread.join(timeout=5)

def classify_dispatch_fetch_error(error):
    """Return a useful network failure category for dispatch fetch diagnostics."""
    if DISPATCH_FETCH_TRANSIENT_RE.search(error or ""): return "network_transient"
    if re.search(r"Authentication failed|Permission denied \(publickey\)|403|repository not found", error or "", re.I):
        return "authentication_or_access"
    return "git_fetch_error"

def record_dispatch_fetch_failure(host, error, at=None):
    """Keep a bounded one-hour failure history for the target host."""
    at = time.time() if at is None else at
    records = DISPATCH_FETCH_FAILURES.setdefault(host, [])
    records.append({"at": at, "type": classify_dispatch_fetch_error(error)})
    cutoff = at - DISPATCH_FETCH_FAILURE_WINDOW
    DISPATCH_FETCH_FAILURES[host] = [record for record in records if record["at"] >= cutoff]

def dispatch_fetch_health(lanes, now=None):
    """Health view of recent target-host fetch failures (one-hour rolling window)."""
    now = time.time() if now is None else now
    hosts = {cfg.get("host") for cfg in lanes.values() if cfg.get("host")}
    hosts.update(cfg.get("controller_host", "controller") for cfg in lanes.values())
    result = {}
    for host in sorted(hosts):
        records = [r for r in DISPATCH_FETCH_FAILURES.get(host, [])
                   if r["at"] >= now - DISPATCH_FETCH_FAILURE_WINDOW]
        if records:
            counts = {}
            for record in records: counts[record["type"]] = counts.get(record["type"], 0) + 1
            result[host] = {"recent_failures": len(records),
                            "primary_error": max(counts, key=counts.get),
                            "window_seconds": DISPATCH_FETCH_FAILURE_WINDOW}
    return result

def git_network_policy(cfg, harvest=False):
    """Per-host lane config: fetch_policy={attempts, delay, fallback_after}.

    Main fetch, controller fetch, mirror fetch and seed push use the same bounds.
    fallback_after caps direct HTTPS attempts independently of the retry budget.
    """
    configured = cfg.get("fetch_policy", {})
    attempts = max(1, int(configured.get("attempts", HARVEST_FETCH_MAX_ATTEMPTS if harvest
                                         else DISPATCH_BASE_FETCH_ATTEMPTS)))
    return {"attempts": attempts,
            "delay": max(0, float(configured.get("delay", HARVEST_FETCH_RETRY_DELAY if harvest
                                                  else DISPATCH_BASE_FETCH_DELAY))),
            "fallback_after": max(1, min(attempts, int(configured.get("fallback_after", attempts))))}

def git_network_operation(command, host, operation, policy, direct=False, transient_only=False,
                          track_fetch=True):
    """Retry a Git transfer; seed pushes opt out of fetch failure health counts."""
    attempts = policy["fallback_after"] if direct else policy["attempts"]
    for attempt in range(1, attempts + 1):
        try:
            rc, out, err = sh(command, timeout=180)
        except subprocess.TimeoutExpired as error:
            # subprocess can return partial bytes even with text=True. Keep the
            # timeout in the normal failure path for retries, fallback and health.
            out, err = error.stdout or "", error.stderr or ""
            if isinstance(out, bytes): out = out.decode(errors="replace")
            if isinstance(err, bytes): err = err.decode(errors="replace")
            rc, err = 124, f"git transfer timed out after {error.timeout}s: {err}"
        if not rc: return rc, out, err
        if track_fetch: record_dispatch_fetch_failure(host, err)
        retry = attempt < attempts and (not transient_only or classify_harvest_fetch_error(err) == "transient")
        log(f"{host} {operation} failed {attempt}/{attempts} "
            f"({classify_dispatch_fetch_error(err)}); " + ("retrying" if retry else "exhausted"), brain=False)
        if not retry: break
        time.sleep(policy["delay"] * attempt)
    return rc, out, err

def deps_met(item):
    """Body line `依赖：#12 #34` (or `Depends on: #12`) gates dispatch until those issues are CLOSED."""
    m = re.search(r"^\s*(?:依赖|Depends on)\s*[:：](.*)$", item["body"], re.M)
    for d in re.findall(r"#(\d+)", m.group(1)) if m else []:
        rc, out, _ = gh(["issue", "view", d, "--json", "state"])
        if rc or json.loads(out)["state"] != "CLOSED": return False
    return True

def base_missing(error):
    """True when the failure says the base commit is not in the target repo yet (fetch lag)."""
    return bool(BASE_MISSING_RE.search(error or ""))

def base_check_command(repo_path, base):
    """Remote snippet: verify that the base commit exists in the target repo."""
    return f"git -C {repo_path} cat-file -e {base}^{{commit}}"

def base_fetch_command(repo_path, url, branch):
    """Remote snippet: fetch the base branch into the target repo."""
    return f"git -C {repo_path} fetch -q {url} {branch}"

def base_seed_push_command(local_repo, host, repo_path, base):
    """Push the already-fetched base object from the controller into a lane repo over SSH."""
    return ["git", "-c", f"core.sshCommand={git_ssh_command(ssh_opts(host))}",
            "-C", local_repo, "push", "-q", f"{host}:{repo_path}",
            f"{base}:refs/fleet/seed/{base}"]

def dispatch_failed(lane, cfg, item, error, busy=False):
    """Drop the claim; capacity denials wait quietly, host failures back off.

    A lane that the host still calls busy is not a host failure: the host is reachable
    and healthy, the lane simply has not turned idle yet.  Backing the host off for it
    would starve every other lane of that host, so the re-dispatch after a run ends
    waits for the next tick instead. Exhausted fleet capacity also leaves the host
    healthy and is accounted in the lane's idle window (Refs #1688)."""
    gh(["issue", "edit", str(item["n"]), "--remove-label", "in-progress"])
    if CAPACITY_EXHAUSTED_RE.search(str(error)): return
    if busy:
        log(f"{lane} #{item['n']}: the host still calls the lane busy; no card assigned, the host is not backed off", brain=False)
        return
    if str(error).startswith("dispatch fetch failed:"):
        health = dispatch_fetch_health({lane: cfg}).get(cfg["host"], {})
        category = health.get("primary_error", "git_fetch_error")
        b = BACKOFF.setdefault(cfg["host"], {"n": 0, "until": 0}); b["n"] += 1
        wait = min(HOST_BACKOFF_MAX, HOST_BACKOFF_BASE * 2 ** (b["n"] - 1)); b["until"] = time.time() + wait
        log(f"{cfg['host']} dispatch fetch failed ({category}; recent={health.get('recent_failures', 1)}; "
            f"#{item['n']}: {str(error)[-100:]}); backing off {wait}s", brain=False)
        return
    if str(error).startswith("controller source fetch failed:"):
        source = cfg.get("controller_host", "controller")
        health = dispatch_fetch_health({"source": {"host": source}}).get(source, {})
        log(f"dispatch #{item['n']} controller source fetch failed; {cfg['host']} remains healthy "
            f"(source={source}; recent={health.get('recent_failures', 0)}; "
            f"primary_error={health.get('primary_error', 'git_fetch_error')}; {str(error)[-120:]})", brain=False)
        return
    host_fail(cfg["host"], f"dispatch #{item['n']}: {error}")

def is_fix_round_card(labels, title, body):
    """True when the card asks a lane to redo a rejected PR (rubric F1..F6, Refs #1851).

    The marker is explicit: a fix label (`fix`, `card:fix`, `type:fix`, `review-fix`…), a
    `## Fix Round` heading, review request or rebase branch in the body, or a fix round title prefix.
    An ordinary work card that merely fixes a bug stays an ordinary work card."""
    if set(labels) & FIX_ROUND_CARD_LABELS: return True
    if FIX_ROUND_HEADING_RE.search(body or ""): return True
    if "审查要求修改" in (body or "") or FIX_ROUND_PR_BRANCH_RE.search(body or ""): return True
    return bool(FIX_ROUND_TITLE_RE.match(title or ""))

def fix_round_pr(body, title=""):
    """The PR number of the round: the `rebase-pr-<N>` branch or the `目标 PR` field of the card."""
    for text in (body or "", title or ""):
        match = FIX_ROUND_PR_BRANCH_RE.search(text) or FIX_ROUND_PR_FIELD_RE.search(text)
        if match: return match.group(1)
    return None

def fix_round_brief(body, title=""):
    """The fix round brief, with the PR of the round when the card names one."""
    return FIX_ROUND_TASK_BOOK.replace("{pr}", fix_round_pr(body, title) or "<N>")

LANE_BUSY_RE = re.compile(r"lane \S+ is busy", re.I)  # the host still runs the finished run
CAPACITY_EXHAUSTED_RE = re.compile(r"\b(?:capacity exhausted|no (?:available )?capacity)\b", re.I)

TESTER_RISK_CARD_LIMIT = 200

def tester_risk_decision(body):
    """Classify how deep the evidence review of a verify card must go (Refs #1988).

    A failure never buys a cheaper review: the card is read in full.
    """
    if tester_risk is None:
        return {"decision": "full_read", "reason": "risk classifier unavailable"}
    try:
        return tester_risk.classify_card(
            body, repo=REPO, read=sh,
            # default 1 = every card is read in full: sampling is opt-in and never lowers the default gate
            sample_rate=os.environ.get("AD_TESTER_RISK_SAMPLE_RATE", "1"),
            seed=os.environ.get("AD_TESTER_RISK_SEED", "risk-v1"))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return {"decision": "full_read", "reason": "risk classification unavailable"}

def record_tester_risk(state, issue, decision):
    """Keep the review depth with the card, not only with the lane slot.

    The lane entry is dropped when the run ends, but the card keeps its decision so
    the tester still reads how deep the evidence review must go.
    """
    cards = state.setdefault("_tester_risk_cards", {})
    cards[str(issue)] = decision
    if len(cards) > TESTER_RISK_CARD_LIMIT:
        for key in list(cards)[:len(cards) - TESTER_RISK_CARD_LIMIT]: cards.pop(key)

def dispatch(lane, cfg, item, base):
    n = item["n"]
    body = item["body"]
    if item.get("verify") and item.get("tester_risk"):
        risk = item["tester_risk"]
        body = f"{body.rstrip()}\n\nTester evidence review: {risk['decision']}\nReason: {risk['reason']}\n"
    labels = set(item.get("labels", ()))
    # PLANNER's ordinary work cards have Chinese titles and only priority/backend
    # labels, so impl is the default. Explicit non-impl markers take precedence,
    # including when an old impl label or title prefix remains on the card.
    excluded_labels = {prefix + kind for kind in NON_IMPL_CARD_TYPES for prefix in ("", "card:", "type:")}
    excluded_labels.add("upstream(swrm)")
    excluded_title = re.match(r"(?i)^(?:upstream|ops|follow-up|planner|verify-intent)(?:\s*[:：]|\s+|$)", item["title"])
    is_impl = not (labels & excluded_labels or excluded_title)
    # A fix round redoes a rejected PR: its task book forces the six checklist items of the
    # rubric (F1..F6). Only an implementation card gets them: a planner/follow-up/verify card
    # asks for no code, so the checklist would be noise there.
    if is_impl and is_fix_round_card(labels, item["title"], body):
        brief = fix_round_brief(body, item["title"])
        if brief.rstrip() not in body and FIX_ROUND_TASK_BOOK.rstrip() not in body:
            body = f"{body.rstrip()}\n\n{brief}"
    # A heading mention or a partial block must not suppress either requirement.
    if is_impl and IMPL_CARD_TASK_BOOK.rstrip() not in body:
        body = f"{body.rstrip()}\n\n{IMPL_CARD_TASK_BOOK}"
    task = f"# #{n} {item['title']}\n\n{body}\n\n{RULES.format(n=n)}\n"
    path = f"{D}/tasks/{lane}-{n}.md"; os.makedirs(f"{D}/tasks", exist_ok=True)
    with open(path, "w") as stream: stream.write(task)
    h = cfg["host"]; F = cfg.get("fleet", "~/.local/bin/swrm-fleet"); R = cfg.get("repo", "~/work/phanes-fleet")
    with open(path) as task_file:
        rc, _, err = sh(ssh_argv(ssh_opts(h), h, f"cat > /tmp/task-{lane}.md"), timeout=60, inp=task_file.read())
    if rc: raise RuntimeError(f"task upload: {err.strip()[-200:]}")
    ver = verify_of(item["body"]); title = f"#{n} {item['title']}"[:120]
    assign = (f"{F} lane assign {lane} --task-file /tmp/task-{lane}.md --base {base} --budget-minutes {BUDGET} --title {shlex.quote(title)}"
              + (f" --verify {shlex.quote(ver)}" if ver else ""))
    # A pi fix round must carry its level even when the host runs an older swrm. A
    # rejected flag fails dispatch; retrying without it would silently change the level.
    thinking = fix_thinking(cfg, item)
    if thinking: assign += f" --thinking {shlex.quote(thinking)}"
    # Refs #1810: the base commit must exist in the target repo before the lane is assigned. The
    # target repo lags behind upstream, so a missing base is fetched and the check is repeated; the
    # check itself is not relaxed (a base that stays missing is still an error).
    cmd = f"{base_check_command(R, base)} && {assign}"
    for attempt in range(1, DISPATCH_BASE_FETCH_ATTEMPTS + 1):
        rc, out, err = sh(ssh_argv(ssh_opts(h), h, cmd), timeout=180)
        m = re.search(r'"id":\s*"([^"]+)"', out)
        if not rc and m: return m.group(1), ver
        if not base_missing(err) or attempt == DISPATCH_BASE_FETCH_ATTEMPTS: break
        # fetch-on-miss: pull the base branch into the target repo, then check again. No AD-side
        # mutex guards the fetch, so concurrent ticks never block each other (git serializes its
        # own short-lived ref locks).
        policy = git_network_policy(cfg)
        frc, _, ferr = git_network_operation(
            ssh_argv(ssh_opts(h), h, base_fetch_command(R, BASE_REPO_URL, BASE_BRANCH)),
            h, f"dispatch fetch #{n}", policy, direct=True)
        if frc:
            log(f"{h} dispatch fetch #{n}: direct fetch threshold reached; falling back to controller seed push", brain=False)
            controller_repo = cfg.get("controller_repo", LOCAL)
            controller_host = cfg.get("controller_host", "controller")
            src_rc, _, src_err = git_network_operation(
                ["git", "-C", controller_repo, "fetch", "-q", BASE_REPO_URL, base],
                controller_host, f"controller source fetch #{n}", policy)
            if src_rc:
                # The controller's GitHub route must never back off the target host.
                frc, ferr = src_rc, f"controller source fetch failed: {src_err.strip()[-120:]}"
            else:
                frc, _, ferr = git_network_operation(
                    base_seed_push_command(controller_repo, h, R, base), h, f"seed push #{n}", policy,
                    track_fetch=False)
            if frc and cfg.get("seed_url"):
                frc, _, ferr = git_network_operation(
                    ssh_argv(ssh_opts(h), h, base_fetch_command(R, cfg["seed_url"], BASE_BRANCH)),
                    h, f"seed mirror fetch #{n}", policy)
            if frc:
                if src_rc and not cfg.get("seed_url"):
                    raise RuntimeError(ferr)
                health = dispatch_fetch_health({lane: cfg}).get(h, {})
                raise RuntimeError(f"dispatch fetch failed: base check: {(err or out).strip()[-100:]} | "
                                   f"rc={frc}: {ferr.strip()[-120:]}; recent fetch failures="
                                   f"{health.get('recent_failures', 0)}; primary error="
                                   f"{health.get('primary_error', 'git_fetch_error')}")
            log(f"{h} dispatch fetch #{n}: seed fallback succeeded; verifying base commit", brain=False)
        time.sleep(DISPATCH_BASE_FETCH_DELAY)
    raise RuntimeError(f"assign rc={rc}: {(err or out).strip()[-300:]}")

def git_fetch_command(ssh_options, local_repo, remote, branch, lane):
    """Build the harvest fetch argv without splitting SSH option values twice."""
    ssh_command = git_ssh_command(ssh_options)
    return ["git", "-c", f"core.sshCommand={ssh_command}", "-C", local_repo,
            "fetch", "-q", remote, f"+{branch}:refs/fleet/{lane}"]

def classify_harvest_fetch_error(error):
    """Classify a harvest fetch failure: `transient` (retry after a backoff) or `fatal` (a retry cannot help)."""
    return "transient" if (HARVEST_FETCH_TRANSIENT_RE.search(error or "") or
                           DISPATCH_FETCH_TRANSIENT_RE.search(error or "")) else "fatal"

def should_retry_harvest_fetch(error, attempt, max_attempts=HARVEST_FETCH_MAX_ATTEMPTS):
    """Return whether a failed harvest fetch is transient and still within its retry bound."""
    return classify_harvest_fetch_error(error) == "transient" and attempt < max_attempts

def harvest_fetch_backoff_seconds(failures, base=HARVEST_FETCH_BACKOFF_BASE, cap=HARVEST_FETCH_BACKOFF_CAP):
    """Exponential backoff (seconds) before the next harvest attempt after `failures` consecutive failures."""
    if failures <= 0: return 0
    return min(cap, base * 2 ** (failures - 1))

def harvest_fetch_outcome(failures, classification, threshold=HARVEST_FETCH_MAX_TRANSIENT_FAILURES):
    """Outcome of a failed harvest fetch: `transient` (log + backoff) or `lane-failed` (give up on the issue)."""
    if classification == "fatal": return "lane-failed"
    return "lane-failed" if failures > threshold else "transient"

def lane_in_fetch_backoff(st, now=None):
    """True while a lane's harvest fetch is backing off after a transient failure."""
    until = float((st or {}).get("fetch_backoff_until", 0) or 0)
    return until > (time.time() if now is None else now)

def mark_done(state, n):
    state.setdefault("_done", []); state["_done"] = (state["_done"] + [n])[-200:]

def label_lane_failed(n, body):
    """Label the issue lane-failed and explain why in a comment."""
    gh(["issue", "edit", str(n), "--remove-label", "in-progress", "--remove-label", "lane-ready", "--add-label", "lane-failed"])
    gh(["issue", "comment", str(n), "--body", body])

def budget_window_open(lane, cfg, info):
    """Confirm recovery from a scoped probe, or the health-loop quota snapshot.

    The default source is health.json in the AD state directory. Its MiniMax
    remains observation applies to pi (the provider's consumer in swrm/policy.json).
    Snapshots must be newer than the hold; old snapshots and unknown windows keep
    the hold. Elapsed time alone never establishes recovery. An operator probe
    overrides the snapshot.
    """
    command = os.environ.get("AD_BUDGET_PROBE_CMD", "").strip()
    try:
        if command:
            proc = subprocess.run(shlex.split(command), shell=False, capture_output=True, text=True, timeout=20)
            if proc.returncode: return False
            payload = json.loads(proc.stdout)
        else:
            path = os.path.join(D, "health.json")
            held_at = info.get("at")
            if (not isinstance(held_at, (int, float)) or isinstance(held_at, bool)
                    or not math.isfinite(held_at)): return False
            with open(path) as stream:
                payload = json.load(stream)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    backend = cfg.get("backend")
    if not backend or not isinstance(payload, dict): return False
    # Select the target before inspecting any windows. Never search other lanes
    # or backends for an available window when the target has unknown quota.
    if "lanes" in payload:
        rows = payload["lanes"]
        payload = rows.get(lane) if isinstance(rows, dict) else None
    elif "backends" in payload:
        rows = payload["backends"]
        payload = rows.get(backend) if isinstance(rows, dict) else None
    elif backend in payload:
        payload = payload[backend]
    elif not command:
        provider = cfg.get("provider")
        if backend != "pi" or provider not in (None, "minimax", "minimax-cn"): return False
        payload = payload.get("minimax")
        if isinstance(payload, dict) and any(key in payload for key in ("interval_pct", "weekly_pct")):
            # health-loop's coding_plan/remains percentages are quota remaining,
            # not usage. Both the short and weekly window must have headroom.
            payload = dict(payload, five_hour={"remaining_pct": payload.get("interval_pct")},
                           seven_day={"remaining_pct": payload.get("weekly_pct")})
    elif any(key in payload for key in ("pi", "codex", "claude", "agy", "ocgo", "deepseek")):
        return False

    if not command:
        observed_at = payload.get("observed_at") if isinstance(payload, dict) else None
        if (not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool)
                or not math.isfinite(observed_at) or observed_at <= held_at
                or not 0 <= time.time() - observed_at <= 900): return False

    def available(row):
        if not isinstance(row, dict): return False
        if row.get("lane", lane) != lane or row.get("backend", backend) != backend: return False
        if row.get("stale") is True: return False
        states = []
        exhausted = row.get("exhausted")
        if isinstance(exhausted, bool): states.append(not exhausted)
        for key, limit, remaining in (("used_percentage", 100, False), ("used_percent", 100, False),
                                      ("remaining_pct", 100, True), ("used", 1, False)):
            value = row.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                states.append(0 < value <= limit if remaining else 0 <= value < limit)
        # Only defined quota containers are inspected; metadata and sibling
        # backends cannot establish availability. A null advertised window is unknown.
        for key in ("five_hour", "seven_day", "window", "windows", "quota", "quotas", "rate_limits"):
            if key in row:
                child = row[key]
                if key == "windows" and isinstance(child, dict):
                    states.append(bool(child) and all(available(value) for value in child.values()))
                else:
                    states.append(available(child))
        return bool(states) and all(states)

    return available(payload)

def budget_holds(state):
    holds = state.get("_budget_requeue")
    if not isinstance(holds, dict): holds = {}; state["_budget_requeue"] = holds
    counts = state.get("_budget_failures")
    if not isinstance(counts, dict): counts = {}; state["_budget_failures"] = counts
    return holds, counts

def clear_budget_history(state, issue):
    """Forget the consecutive budget failures when a card reaches another terminal flow."""
    holds, counts = budget_holds(state)
    holds.pop(str(issue), None); counts.pop(str(issue), None)

def release_recovered_budget_holds(state, lanes):
    """Return held cards to lane-ready only after the window query confirms recovery."""
    holds, _ = budget_holds(state)
    if not holds: return  # nothing held: no state write before the queue is measured
    for issue, hold in list(holds.items()):
        if not isinstance(hold, dict): continue
        lane = hold.get("lane")
        cfg = lanes.get(lane, {})
        if not hold.get("terminal_pending"):
            recovered = budget_window_open(lane, cfg, hold)
            held_at = hold.get("at")
            if (not isinstance(held_at, (int, float)) or isinstance(held_at, bool)
                    or not math.isfinite(held_at) or held_at > time.time()):
                held_at = hold["at"] = time.time()
            try:
                max_hours = max(1, int(os.environ.get("AD_BUDGET_HOLD_MAX_HOURS", "6")))
            except ValueError:
                max_hours = 6
            elapsed = time.time() - held_at
            if not recovered and elapsed > max_hours * 3600:
                hold["terminal_pending"] = True
                hold["terminal_reason"] = "hold_expired"
                hold["hold_hours"] = elapsed / 3600
                save_state(state)
            elif not recovered:
                continue
        if hold.get("terminal_pending"):
            lane = hold.get("lane", "lane")
            host = (lanes.get(lane) or {}).get("host", "unknown")
            body = (f"Lane {lane} ({host}, run `{hold.get('run')}`) ended `{hold.get('status')}` without a commit "
                    f"after {hold.get('tries')} budget-window failures (error_class `{hold.get('error_class')}`).\n\n"
                    f"Executor summary: {hold.get('summary', '')}\n\nLabel `lane-failed`; re-add `lane-ready` to retry.")
            rc, _, err = gh(["issue", "edit", str(issue), "--remove-label", "in-progress",
                             "--remove-label", "lane-ready", "--add-label", "lane-failed"])
            if rc:
                log(f"budget failure #{issue}: terminal label write failed ({err.strip()[-120:]}); retaining hold", brain=False)
                continue
            if hold.get("terminal_reason") == "hold_expired":
                body = (f"Budget hold expired after {hold['hold_hours']:.2f} hours without confirmed recovery "
                        f"(hold_expired). Label `lane-failed`; re-add `lane-ready` to retry.")
                log(f"budget hold #{issue}: {hold['hold_hours']:.2f} hours reason=hold_expired", brain=False)
            gh(["issue", "comment", str(issue), "--body", body])
            _, counts = budget_holds(state)
            holds.pop(issue, None); counts.pop(str(issue), None)
            mark_done(state, int(issue))
            continue
        rc = not guarded_requeue(int(issue))
        err = "PR guard unverified"
        if rc:
            log(f"budget hold #{issue}: window recovered but labels could not be updated ({err.strip()[-120:]})", brain=False)
            continue
        holds.pop(issue, None)
        log(f"budget hold #{issue}: backend window recovered; returned to lane-ready", brain=False)
    save_state(state)

def budget_failure(info):
    """Recognize an executor budget death without classifying quota or real failures.

    An explicit `error_class` is authoritative: a run the executor classified as a
    real failure (or as a quota problem) keeps the ordinary failure flow, whatever
    its status or its summary text says.  Only a run without an error class is
    judged by the status and the summary.
    """
    error_class = str(info.get("error_class") or "").strip().lower()
    if error_class: return error_class == "budget_exceeded"
    if str(info.get("status") or "").strip().lower() == "budget_exceeded": return True
    return bool(re.search(r"\b(?:budget_exceeded|token plan.{0,40}(?:limit|quota)|budget window exhausted)\b",
                          str(info.get("summary") or ""), re.I))

def terminal_budget_failure(n, lane, cfg, st, info, state, tries):
    """Schedule the terminal label on the next tick; retain the durable count meanwhile."""
    holds, _ = budget_holds(state)
    holds[str(n)] = {"issue": n, "lane": lane, "run": st["run"], "tries": tries,
                     "terminal_pending": True, "status": info.get("status"),
                     "error_class": info.get("error_class"), "summary": info.get("summary", ""),
                     "at": time.time()}
    save_state(state)
    return {"done": True, "status": "budget-terminal-pending", "error": None, "missing": []}

def hold_budget_failure(lane, cfg, st, info, state):
    """Free the lane while keeping the issue claimed until its backend window recovers."""
    n = st["issue"]
    holds, counts = budget_holds(state)
    key = str(n)
    tries = int(counts.get(key, 0) or 0) + 1
    counts[key] = tries
    maximum = int(os.environ.get("AD_BUDGET_REQUEUE_MAX", "2"))
    if tries >= max(1, maximum):
        return terminal_budget_failure(n, lane, cfg, st, info, state, tries)
    holds[key] = {"issue": n, "lane": lane, "run": st["run"], "tries": tries,
                  "at": time.time(), "summary": info.get("summary", "")}
    save_state(state)
    log(f"{lane} #{n} run {st['run']} budget-exceeded ({tries}/{maximum}); held pending window recovery", brain=False)
    return {"done": True, "status": "budget-held", "error": None, "missing": []}

def harvest_fetch(lane, n, fetch, st, cfg=None):
    """Fetch a finished lane branch with bounded attempts.

    Returns (ok, error, failures); `failures` counts the consecutive failed harvest
    cycles of this lane and is reset by the first successful fetch.
    """
    cfg = cfg or {}
    rc, _, err = git_network_operation(fetch, cfg.get("host", lane), f"PR head fetch {lane} #{n}",
                                        git_network_policy(cfg, harvest=True), transient_only=True)
    if not rc:
        st.pop("fetch_failures", None); st.pop("fetch_backoff_until", None)
        return True, "", 0
    failures = int(st.get("fetch_failures", 0) or 0) + 1
    st["fetch_failures"] = failures
    return False, err, failures

def handle_harvest_fetch_failure(lane, cfg, st, info, state, err, failures):
    """Transient harvest fetch failures only log and back off; past the threshold the issue is lane-failed."""
    n, run = st["issue"], st["run"]
    classification = classify_harvest_fetch_error(err)
    if harvest_fetch_outcome(failures, classification) == "transient":
        wait = harvest_fetch_backoff_seconds(failures)
        st["fetch_backoff_until"] = time.time() + wait
        save_state(state)
        log(f"{lane} #{n} harvest fetch failed (transient) {failures}/{HARVEST_FETCH_MAX_TRANSIENT_FAILURES}; backing off {wait}s before the next attempt", brain=False)
        return {"done": False, "status": "fetch-backoff", "error": None, "missing": []}
    st.pop("fetch_failures", None); st.pop("fetch_backoff_until", None)
    label_lane_failed(n, f"Lane {lane} ({cfg['host']}, run `{run}`) produced commit `{info.get('commit')}` but the harvest fetch failed "
                       f"({classification}, {failures} consecutive failure(s)): {err.strip()[-200:]}\n\nLabel `lane-failed`; re-add `lane-ready` to retry.")
    mark_done(state, n)
    log(f"{lane} #{n} harvest fetch failed ({classification}) {failures} consecutive failure(s) → labelled lane-failed")
    return {"done": True, "status": "lane-failed", "error": None, "missing": []}

RUN_RECORD_SNIPPET = REMOTE_STATE_HOME + r'''
import base64,json,os,sys
run, payload = sys.argv[1], sys.argv[2]
d = os.path.join(O, "runs", run)
os.makedirs(d, exist_ok=True)
rec = json.loads(base64.b64decode(payload).decode("utf-8"))
with open(d + "/pr-validation.json", "w") as f: json.dump(rec, f, ensure_ascii=False, indent=1)
with open(d + "/pr-validation.log", "a") as f:
    f.write(rec["at"] + " " + rec["error_class"] + " (" + rec["class"] + "): " + "; ".join(rec["missing"]) + "\n")
print("ok")
'''

def record_pr_validation(cfg, run, record):
    """Write the PR validation result into the run record and the run log (Refs #1704).

    The AD keeps a per-run log under its state dir; the lane host gets the same record
    next to its run directory, so a blocking diagnosis can quote the missing items. Both
    writes are best effort: a failing host must not stop the dispatch loop."""
    path = f"{D}/runs/{run}.log"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(f"{record['at']} {record['error_class']} ({record['class']}): {'; '.join(record['missing'])}\n")
    except OSError as e:
        log(f"run {run}: PR validation run log not written ({e!r})", brain=False)
    try:
        payload = base64.b64encode(json.dumps(record, ensure_ascii=False).encode("utf-8")).decode("ascii")
        rc, _, err = sh(ssh_argv(ssh_opts(cfg.get("host")), cfg.get("host"), "python3", "-", run, payload),
                        timeout=60, inp=RUN_RECORD_SNIPPET)
        if rc: log(f"run {run}: PR validation run record not written to the host ({err.strip()[-120:]})", brain=False)
    except Exception as e:
        log(f"run {run}: PR validation run record not written to the host ({e!r})", brain=False)

def block_pr_validation(lane, cfg, st, info, state, title, body, missing, error_class=PR_VALIDATION_CLASS):
    """Block a lane PR whose required fields are missing and hand the issue back for a quick fix.

    The missing items are returned to the runner as an explicit error and listed one by
    one on the issue; the run is recorded as a quick-fix class, so the lane-failed
    statistics stay clean (Refs #1704)."""
    n, run = st["issue"], st["run"]
    record = {"run": run, "issue": n, "lane": lane, "class": PR_VALIDATION_FIX_CLASS,
              "error_class": error_class, "missing": list(missing), "title": title,
              "at": time.strftime("%F %T")}
    record_pr_validation(cfg, run, record)
    state["_pr_validation"] = (state.get("_pr_validation", []) + [record])[-200:]
    requeued = guarded_requeue(n)
    gh(["issue", "comment", str(n), "--body", f"Lane {lane} (run `{run}`) 的 PR 未通过提交前校验，未创建 PR：\n\n"
        + "".join(f"- {item}\n" for item in missing)
        + f"\n按可快速修复类（`{error_class}`）处理，不计评审轮次，不计入 lane-failed 统计；重排结果：{'已核验' if requeued else '待下周期重试'}。"])
    if not requeued: return {"done": False, "status": "requeue-pending", "error": "PR guard unverified", "missing": list(missing)}
    mark_done(state, n)
    return {"done": True, "status": "delivery-map-failed" if error_class == DELIVERY_MAP_CLASS else "pr-validation-failed",
            "error": f"PR 提交前校验未通过（{PR_VALIDATION_STRICT}）：{'；'.join(missing)}",
            "missing": list(missing)}

# The artifacts that never enter a harvested branch (Refs #1835, #1863): shared
# object files and the review products
# (.lane-review/PR-N.md).  The rules are a list constant: one more artifact kind is
# one more pattern. This legacy AD owns its filtering rules.
HARVEST_EXCLUDE_PATTERNS = [re.compile(r"(^|/)[^/]*\.so$"), re.compile(r"(^|/)\.lane-review/")]
# The collapse guard of the fix/rebase harvest (Refs #1833).  A fix/rebase round that
# does not continue on the head the PR had, but only produces an incremental patch,
# replaces the whole PR when the AD pushes the harvest back (2026-11-03: #1807 8 files/
# 1780 lines -> 1/11, #1804 6/1401 -> 3/39, #1715 8/868 -> 2/23, #1755 8/746 -> 1/19,
# #1653 6/200 -> 2/19, #1824 9/835 -> 1/17).  Before such a push the AD checks:
#   1. the new head contains the head the PR had before the round
#      (`git merge-base --is-ancestor`), otherwise the harvest replaced the PR;
#   2. the changed files and the changed lines relative to the base do not collapse:
#      one of the two dimensions dropping by more than the threshold (50% by default)
#      refuses the push (the dimensions are independent: "or", not "and");
#   3. a failing git command refuses the push as well (fail closed);
#   4. a card whose body cannot be read or parsed is not an empty card: without it the
#      target PR of the round is unknown, so the harvest is refused.
# A card that declares an overall rewrite is exempt.
HARVEST_COLLAPSE_THRESHOLD = float(os.environ.get("AD_HARVEST_COLLAPSE_THRESHOLD", "0.5"))
# The card of a fix/rebase round names the PR it works on ("重基PR：#1845 fleet/...").
# Only such a card is compared with a PR head: an ordinary card that mentions a PR
# must not be harvested against the head of a foreign PR.
HARVEST_TARGET_PR = re.compile(
    r"(?:重基\s*PR|(?:fix\s*/\s*rebase|rebase|fix)\s*(?:PR|pull\s+request))\s*[：:# ]+\s*#?(\d+)\b", re.I)
HARVEST_REWRITE_MARKER = re.compile(r"整体重写\s*[:：]?\s*(?:是|true|yes|允许|豁免)", re.I)

def harvest_card_body(issue):
    """Read the card of an issue; returns (body, reason).

    A reason means the card could not be read or parsed: that is not an empty card,
    the caller must not treat it as one (Refs #1833)."""
    try:
        rc, out, err = gh(["issue", "view", str(issue), "--json", "body"])
    except (OSError, subprocess.SubprocessError) as error:
        return "", f"the card of #{issue} cannot be read: {error!r}"
    if rc:
        return "", f"the card of #{issue} cannot be read (gh rc={rc}): {err.strip()[-160:]}"
    try:
        payload = json.loads(out)
    except ValueError as error:
        return "", f"the card of #{issue} cannot be parsed: {error}"
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, str):
        return "", f"the card of #{issue} carries no body"
    return body, ""

def harvest_collapse_guard(issue, ref):
    """Return (allowed, detail) for the harvest of a fix/rebase round (Refs #1833).

    `ref` is the head that would actually be pushed after artifact filtering.
    Rebuilt commits must also contain the old PR head. Every doubt refuses the push."""
    body, reason = harvest_card_body(issue)
    if reason: return False, reason
    if HARVEST_REWRITE_MARKER.search(body):
        return True, "the card declares an overall rewrite: the guard is waived"
    match = HARVEST_TARGET_PR.search(body)
    if not match:
        return True, "the card names no target PR: there is nothing to compare with"
    pr = match.group(1)
    try:
        # pr view exposes no baseRefOid. REST provides both SHAs in one response;
        # call sh directly because gh api has no -R option (the gh helper adds it).
        rc, out, err = sh(["gh", "api", f"repos/{REPO}/pulls/{pr}"], timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"PR #{pr} cannot be read: {error!r}"
    if rc:
        return False, f"PR #{pr} cannot be read (gh rc={rc}): {err.strip()[-160:]}"
    try:
        pr_data = json.loads(out)
        old, base = pr_data["head"]["sha"], pr_data["base"]["sha"]
        if not isinstance(old, str) or not isinstance(base, str) or not old or not base:
            raise ValueError("missing PR head/base SHA")
    except (ValueError, KeyError, TypeError) as error:
        return False, f"PR #{pr} metadata cannot be parsed: {error}"

    def git(*args):
        code, stdout, stderr = sh(["git", "-C", LOCAL, *args])
        if code: raise RuntimeError(stderr.strip()[-160:] or f"git {args[0]} failed")
        return stdout.strip()
    try:
        new_sha = git("rev-parse", "--verify", f"{ref}^{{commit}}")
        before_size = diff_size(base, old)
        after_size = diff_size(base, ref)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        return False, f"PR #{pr} cannot be checked: {error}"
    ancestry_failure = (f"PR #{pr}: the ancestry check failed, refusing the push "
                        f"({old[:12]}->{new_sha[:12]}, sizes {before_size}->{after_size})")
    try:
        rc, _, err = sh(["git", "-C", LOCAL, "merge-base", "--is-ancestor", old, new_sha])
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"{ancestry_failure}: {error}"
    if rc == 1:
        return False, (f"PR #{pr}: the new head {new_sha[:12]} does not contain "
                       f"the head the PR had {old[:12]} (sizes {before_size}->{after_size})")
    if rc:
        return False, f"{ancestry_failure}: {err.strip()[-160:]}"
    reasons = []
    for name, index in (("files", 0), ("lines", 1)):
        before, after = before_size[index], after_size[index]
        if before and after < before * (1 - HARVEST_COLLAPSE_THRESHOLD):
            reasons.append(f"{name} collapsed {before}->{after}")
    if reasons:
        return False, (f"PR #{pr}: the change collapsed ({'; '.join(reasons)}), sizes "
                       f"{before_size}->{after_size} ({old[:12]}->{new_sha[:12]})")
    return True, (f"PR #{pr}: the new head {new_sha[:12]} contains the head the PR had {old[:12]} "
                  f"and the size holds ({before_size}->{after_size})")

def harvest_guard_record(run, detail):
    """Append the verdict of the guard to the run log: the failure trail of the AD.

    The brain log carries the verdict for the operator, the run log keeps it next to
    the run the lane produced (Refs #1833)."""
    try:
        os.makedirs(f"{D}/runs", exist_ok=True)
        with open(f"{D}/runs/{run}.log", "a") as f:
            f.write(f"{time.strftime('%F %T')} harvest-collapse-guard: {detail}\n")
    except OSError as e:
        log(f"run {run}: harvest guard record not written ({e!r})", brain=False)

def diff_size(base, head):
    """Count changed paths and inserted+deleted lines relative to base."""
    rc, paths, err = sh(["git", "-C", LOCAL, "diff", "--no-renames", "--name-only", base, head])
    if rc: raise RuntimeError(err.strip()[-160:] or "git diff name-only failed")
    rc, stats, err = sh(["git", "-C", LOCAL, "diff", "--no-renames", "--numstat", base, head])
    if rc: raise RuntimeError(err.strip()[-160:] or "git diff numstat failed")
    lines = 0
    for row in stats.splitlines():
        added, deleted, *_ = row.split("\t")
        if added.isdigit() and deleted.isdigit(): lines += int(added) + int(deleted)
    return len(paths.splitlines()) if paths else 0, lines

def filter_harvest(ref, base):
    """Rebuild the harvested branch without the artifacts that never enter a PR.

    Returns (head, removed, empty): the head of the rebuilt branch (the input ref
    when nothing was excluded), the excluded paths and whether nothing effective is
    left.  The emptiness compares the trees, not the heads: commits that cancel each
    other out leave the tree of the base, whatever the head is.  A failing git
    command raises: the caller then pushes nothing (fail closed)."""
    def git(*args, env=None, inp=None):
        p = subprocess.run(["git", "-C", LOCAL, *args], capture_output=True, env=env, input=inp)
        if p.returncode:
            raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
        return p.stdout

    if not base:
        raise RuntimeError("missing dispatch base")
    git("merge-base", "--is-ancestor", base, ref)
    commits = git("rev-list", "--reverse", "--topo-order", "--parents", f"{base}..{ref}").splitlines()
    if any(len(line.split()) != 2 for line in commits):
        raise RuntimeError("cannot filter merge commits")
    paths = git("diff", "--no-renames", "--name-only", "-z", base, ref).split(b"\0")
    # Inspect every commit as well, including artifacts subsequently deleted.
    for line in commits:
        commit = line.split()[0].decode()
        paths += git("diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-r", "-z", commit).split(b"\0")
    removed = sorted({os.fsdecode(p) for p in paths if p and
                      any(rule.search(os.fsdecode(p)) for rule in HARVEST_EXCLUDE_PATTERNS)})
    tree_of = lambda rev: git("rev-parse", f"{rev}^{{tree}}").strip().decode()
    if not removed:
        return ref, [], tree_of(ref) == tree_of(base)
    parent = base
    with tempfile.TemporaryDirectory(prefix=".harvest-", dir=LOCAL) as directory:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(directory, "index"), "GIT_LITERAL_PATHSPECS": "1"}
        for line in commits:
            commit = line.split()[0].decode()
            git("read-tree", commit, env=env)
            git("update-index", "--force-remove", "--", *removed, env=env)
            present = [path for path in removed if git("ls-tree", "-z", base, "--", path)]
            if present:
                git("restore", "--source", base, "--staged", "--", *present, env=env)
            tree = git("write-tree", env=env).strip().decode()
            if tree == tree_of(f"{parent}^{{tree}}"):
                continue  # nothing is left of this commit
            author = git("show", "-s", "--format=%an%x00%ae%x00%aI", commit).rstrip(b"\n").split(b"\0")
            commit_env = {**env, "GIT_AUTHOR_NAME": os.fsdecode(author[0]),
                          "GIT_AUTHOR_EMAIL": os.fsdecode(author[1]), "GIT_AUTHOR_DATE": os.fsdecode(author[2]),
                          "GIT_COMMITTER_NAME": os.fsdecode(author[0]),
                          "GIT_COMMITTER_EMAIL": os.fsdecode(author[1]),
                          "GIT_COMMITTER_DATE": os.fsdecode(author[2])}
            message = git("show", "-s", "--format=%B", commit)
            parent = git("commit-tree", tree, "-p", parent, env=commit_env, inp=message).strip().decode()
    return parent, removed, tree_of(parent) == tree_of(base)

def requeue_executor_start_failure(lane, n, run):
    """Requeue an executor startup failure without counting a task attempt."""
    if not guarded_requeue(n):
        return {"done": False, "status": "requeue-failed", "error": "PR guard unverified", "missing": []}
    log(f"{lane} executor failed to start #{n} (run {run}) → lane-ready, lane paused 600s")
    return {"done": True, "status": "lane-ready", "error": None, "missing": []}

def origin_branch_head(branch):
    """Return the head commit of a branch on origin ("" when the branch is not there).

    The harvest push and this read use the same remote, so the answer describes the
    branch the PR would be opened from."""
    rc, out, _ = sh(["git", "-C", LOCAL, "ls-remote", "origin", f"refs/heads/{branch}"], timeout=60)
    if rc: return ""
    parts = out.split()
    return parts[0] if parts else ""

def local_commit(rev):
    """Return the commit sha of a local revision ("" when it cannot be resolved)."""
    rc, out, _ = sh(["git", "-C", LOCAL, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], timeout=60)
    return out.strip() if not rc else ""

def harvest_push(lane, n, branch, head):
    """Push the harvested branch to origin and tell whether the harvest may continue.

    Returns (ok, error).  A push that failed without any error output (empty stderr)
    but whose branch is already on origin at the pushed head means the update reached
    origin before the failure was reported: the harvest goes on to the PR instead of
    pushing again on every tick (Refs #1640; the dispatch engine reads the same
    evidence).  A push that said why it failed, a branch that is not on origin and a
    branch that carries another head or an unresolved local head all stay push failures."""
    rc, _, err = sh(["git", "-C", LOCAL, "push", "-q", "origin", f"{head}:refs/heads/{branch}"], timeout=180)
    if not rc: return True, None
    reason = err.strip()[-200:]
    if not reason:
        remote, pushed = origin_branch_head(branch), local_commit(head)
        if remote and pushed and remote == pushed:
            log(f"{lane} #{n} push rc={rc} without error output; branch {branch} is on origin "
                f"at {remote[:8]}; continuing to PR", brain=False)
            return True, None
        reason = ("no error output and the branch is not on origin" if not remote else
                  f"no error output; cannot resolve the pushed head {head}" if not pushed else
                  f"no error output; branch {branch} is on origin at {remote[:8]}, "
                  f"not at the pushed head {pushed[:8]}")
    return False, f"push failed: {reason}"

def finish(lane, cfg, st, info, state):
    """Harvest a finished lane run: push the branch, open the PR, relabel the issue.

    Returns a result for the runner: {"done": bool, "status": str, "error": str|None,
    "missing": [...]}. `done` means the lane slot is free again; `error` carries the
    explicit reason back to the caller (Refs #1704)."""
    n, run = st["issue"], st["run"]
    if info.get("error_class") == "executor_start_failed":
        pause_executor_start_lane(lane)
        # Even a commit made during the startup grace period is not a completed
        # task. Leave the run's work intact and return the card for another attempt.
        return requeue_executor_start_failure(lane, n, run)
    if st.get("class") == "verify":
        missing = review_evidence_errors(info.get("evidence"))
        if missing:
            return block_pr_validation(lane, cfg, st, info, state, "", "", missing)
    if info.get("branch") and info["branch"] != "None" and info.get("commit"):
        b = f"fleet/{lane}-issue{n}-{run}"
        fetch = git_fetch_command(ssh_opts(cfg["host"]), LOCAL, f"{cfg['host']}:{info['repo']}", info["branch"], lane)
        ok, err, failures = harvest_fetch(lane, n, fetch, st, cfg)
        if not ok: return handle_harvest_fetch_failure(lane, cfg, st, info, state, err, failures)
        try:
            head, removed, empty = filter_harvest(f"refs/fleet/{lane}", st.get("base"))
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            # Fail closed before the collapse guard as well: record the refusal and
            # release the lane instead of retrying an uncheckable harvest forever.
            detail = f"harvest filter failed: {error}"
            harvest_guard_record(run, detail)
            log(f"{lane} #{n} run {run} {detail}")
            label_lane_failed(n, f"Lane {lane} run `{run}` harvest refused: {detail}\n\n"
                               "Label `lane-failed`; re-add `lane-ready` to retry.")
            mark_done(state, n)
            return {"done": True, "status": "filter-failed", "error": str(error), "missing": []}
        if removed:
            log(f"{lane} #{n} run {run} harvest excluded: {json.dumps(removed, ensure_ascii=True)}")
        # An empty diff can be a fix that undoes the entire PR. Check it before
        # the empty-harvest exit so the refusal records the PR, heads and sizes.
        allowed, guard_detail = harvest_collapse_guard(n, head)
        if not allowed:
            log(f"{lane} #{n} run {run} harvest collapse guard rejected: {guard_detail}")
            harvest_guard_record(run, guard_detail)
            label_lane_failed(n, f"Lane {lane} run `{run}` harvest refused by #1833 collapse guard: {guard_detail}\n\nLabel `lane-failed`; re-add `lane-ready` to retry.")
            mark_done(state, n)
            return {"done": True, "status": "harvest-collapse-rejected", "error": guard_detail, "missing": []}
        if empty:
            detail = (f"no effective change after the artifact filter ({len(removed)} excluded path(s)); "
                      f"{guard_detail}")
            log(f"{lane} #{n} run {run} harvest refused: {detail}")
            harvest_guard_record(run, detail)
            label_lane_failed(n, f"Lane {lane} run `{run}` has {detail}.\n\n"
                               "Label `lane-failed`; re-add `lane-ready` to retry.")
            clear_budget_history(state, n)
            mark_done(state, n)
            return {"done": True, "status": "lane-failed", "error": "empty after artifact filtering", "missing": []}
        log(f"{lane} #{n} run {run} harvest collapse guard: {guard_detail}", brain=False)
        _, subj, _ = sh(["git", "-C", LOCAL, "log", "-1", "--format=%s", head])
        _, msg, _ = sh(["git", "-C", LOCAL, "log", "-1", "--format=%B", head])
        map_missing = []
        if st.get("class") != "verify":  # a verify card delivers evidence, not a change with acceptance rows
            try: map_missing = delivery_map_missing(lane, n, msg)
            except Exception as error:
                return {"done": False, "status": "delivery-map-read-failed", "error": str(error), "missing": []}
        if map_missing and pr_validation_mode() == PR_VALIDATION_STRICT:
            return block_pr_validation(lane, cfg, st, info, state, subj.strip(), "", map_missing,
                                       error_class=DELIVERY_MAP_CLASS)
        if map_missing: log(f"{lane} #{n} delivery map: {'; '.join(map_missing)}", brain=False)
        pushed, push_error = harvest_push(lane, n, b, head)
        if not pushed: return {"done": False, "status": "push-failed", "error": push_error, "missing": []}
        _, lj, _ = gh(["issue", "view", str(n), "--json", "labels"])
        if "lane-pr" in lj:
            record_verify_time(state, n, "evidence_ready")
            gh(["issue", "comment", str(n), "--body", f"Lane {lane} also finished this issue (run `{run}`, duplicate dispatch); branch `{b}` pushed for comparison, no second PR opened."])
            log(f"{lane} finished #{n} (run {run}) duplicate → branch {b}, no PR"); mark_done(state, n)
            return {"done": True, "status": "duplicate", "error": None, "missing": []}
        title, body = lane_pr_content(n, lane, cfg, st, info, subj, msg, info.get("summary"), info.get("tests"), info.get("verify_rc"))
        missing = validate_lane_pr(n, title, body)
        # A quoted opinion (or a raw log) may contain closing keywords, but it
        # cannot supply the commit's missing issue linkage.
        if st.get("class") == "verify" and not closing_reference(n, msg):
            problem = f"PR 正文缺少 Closes/Fixes/Resolves #{n}"
            if problem not in missing: missing.append(problem)
        mode = pr_validation_mode()
        if missing and mode == PR_VALIDATION_STRICT:
            return block_pr_validation(lane, cfg, st, info, state, title, body, missing)
        if missing:
            log(f"{lane} #{n} run {run} PR validation ({mode}): 缺 {'；'.join(missing)}", brain=False)
        rc, out, err = gh(["pr", "create", "--base", "main", "--head", b, "--title", title, "--body", body])
        if rc:
            # No PR exists: the issue is not delivered, so nothing is relabelled, recorded or marked done.
            log(f"{lane} #{n} run {run} gh pr create failed: {err.strip()[-160:]}")
            return {"done": False, "status": "pr-create-failed", "error": f"gh pr create failed: {err.strip()[-160:]}", "missing": []}
        url = (out.strip().splitlines() or [""])[-1]
        delivered = ensure_delivery_state(n)
        clear_budget_history(state, n)
        record_verify_time(state, n, "evidence_ready")
        log(f"{lane} finished #{n} (run {run}, {info.get('status')}) → {url}"); mark_done(state, n)
        return {"done": True, "status": "pr-created",
                "error": None if delivered else "PR delivery labels unverified", "missing": []}
    else:
        if budget_failure(info):
            return hold_budget_failure(lane, cfg, st, info, state)
        label_lane_failed(n, f"Lane {lane} ({cfg['host']}, run `{run}`) ended `{info.get('status')}` without a commit "
            f"(error_class `{info.get('error_class')}`).\n\nExecutor summary: {info.get('summary')}\n\nLabel `lane-failed`; re-add `lane-ready` to retry.")
        clear_budget_history(state, n)
        mark_done(state, n); log(f"{lane} ended #{n} without a commit (run {run}, {info.get('status')}/{info.get('error_class')}) → labelled lane-failed")
        return {"done": True, "status": "lane-failed", "error": None, "missing": []}

# --- 空闲窗口（Refs #1688）--------------------------------------------------------
def to_epoch(value, fmt=None):
    """Epoch seconds of a number, an epoch string or a timestamp string (None when unknown)."""
    if value is None or isinstance(value, bool): return None
    if isinstance(value, (int, float)): return float(value)
    text = str(value).strip()
    if not text: return None
    try: return float(text)
    except ValueError: pass
    for pattern in ([fmt] if fmt else ["%Y-%m-%d %H:%M:%S"]):
        try: return time.mktime(time.strptime(text, pattern))
        except ValueError: continue
    try:
        import datetime
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

def heartbeat_path(): return f"{D}/heartbeat"

def dispatcher_last_alive():
    """When the dispatcher was last alive (epoch seconds), or None when unknown.

    The heartbeat is rewritten every few seconds while the dispatcher sleeps between the
    ticks, so its stamp is the last moment the dispatcher was alive: the gap to now is
    the downtime of the dispatcher itself (Refs #1688)."""
    try:
        with open(heartbeat_path()) as stream:
            stamp = to_epoch(stream.read().strip(), "%Y-%m-%d %H:%M:%S")
        if stamp is not None: return stamp
    except OSError: pass
    try: return os.path.getmtime(heartbeat_path())
    except OSError: return None

def dispatcher_downtime(last_alive, since, now):
    """The seconds the dispatcher was down between `since` and `now` (0 when it was up).

    The heartbeat grace absorbs the jitter of the heartbeat writes, so a dispatcher that
    merely slept between the ticks is never counted as a downtime."""
    if last_alive is None: return 0.0
    start = max(float(since), float(last_alive))
    if start >= now: return 0.0
    return max(0.0, now - start - HEARTBEAT_GRACE_S)

def idle_books(state):
    """The idle bookkeeping of the state: the open windows and the last closed window."""
    windows = state.get("_idle")
    if not isinstance(windows, dict): windows = {}
    state["_idle"] = windows
    last = state.get("_idle_last")
    if not isinstance(last, dict): last = {}
    state["_idle_last"] = last
    return windows, last

def idle_open(state, lane, ended_at, now, last_alive=None):
    """Open the idle window of a lane at the real end time of its run (Refs #1688).

    The window starts at the run's own end time, not at the harvest: a run that ended
    while the dispatcher was down, or a harvest that took a while, must not be counted
    as a short idle.  A window that is already open keeps its start."""
    windows, _ = idle_books(state)
    window = windows.get(lane)
    if isinstance(window, dict) and window.get("since") is not None:
        return window
    since = to_epoch(ended_at)
    if since is None or since > now: since = now
    window = {"since": since, "last_seen": now, "down_s": 0.0, "no_card_s": 0.0,
              "no_cap_s": 0.0, "paused_s": 0.0, "lag_s": 0.0}
    windows[lane] = window
    # the gap between the real end and the observation: the dispatcher was down part of it
    gap = max(0.0, now - since)
    down = min(dispatcher_downtime(last_alive, since, now), gap)
    window["down_s"] = down
    window["lag_s"] = gap - down
    return window

def idle_bucket(outcome):
    """The bucket of an outcome name (what kept the lane idle)."""
    return {"no-card": "no_card_s", "no-capacity": "no_cap_s", "paused": "paused_s"}.get(outcome, "no_cap_s")

def idle_account(state, lane, window, now, outcome, last_alive=None):
    """Account the time since the last observation of an idle window.

    Every second lands in exactly one bucket: the dispatcher was down (`down_s`), it was
    up but the lane was paused (`paused_s`), there was no card for the lane (`no_card_s`),
    there was a card but no capacity (`no_cap_s`), or the dispatcher was busy harvesting
    (`lag_s`).  The buckets belong to one window: a new window starts empty, so the
    downtime of one window never explains the next one (Refs #1688).
    The previous observation explains the elapsed interval; the current outcome is
    saved for the next interval, never applied retroactively to the wait before it."""
    last_seen = float(window.get("last_seen") or now)
    gap = max(0.0, now - last_seen)
    down = min(dispatcher_downtime(last_alive, last_seen, now), gap)
    up = max(0.0, gap - down)
    window["down_s"] = float(window.get("down_s") or 0.0) + down
    bucket = idle_bucket(window.get("outcome", outcome))
    window[bucket] = float(window.get(bucket) or 0.0) + up
    window["last_seen"] = now
    window["outcome"] = outcome
    return window

def idle_reason(window):
    """The dominant reason of an idle window: the bucket that holds the most time."""
    reason, biggest = "no card", 0.0
    for bucket, name in IDLE_BUCKETS:
        seconds = float(window.get(bucket) or 0.0)
        if seconds > biggest: reason, biggest = name, seconds
    return reason if biggest > 0.0 else "no gap"

def idle_close(state, lane, now, last_alive=None, tick_started_at=None):
    """Close the idle window of a lane; returns (idle_s, reason) or None.

    The time between the last observation and the dispatch belongs to the reason the
    lane was still idle (the outcome of the last tick without a card), not to the
    dispatcher's own work.  The dispatcher may have been down in between: that part is
    accounted as the downtime of this window before the reason is decided, so a gap
    that was caused by the downtime is not explained by the empty pool (Refs #1688).
    Old heartbeat evidence ends at tick start: harvesting and dispatching in this tick
    are live dispatcher work, charged to `lag_s`, never to downtime or a prior outcome.
    The window is dropped, so the next window of the lane starts with empty buckets:
    the downtime of one window never explains the next one."""
    windows, last = idle_books(state)
    window = windows.pop(lane, None)
    if not isinstance(window, dict): return None
    idle_s = max(0.0, now - float(window.get("since") or now))
    last_seen = float(window.get("last_seen") or now)
    observed_at = now if tick_started_at is None else max(last_seen, min(now, tick_started_at))
    gap = max(0.0, observed_at - last_seen)
    down = min(dispatcher_downtime(last_alive, last_seen, observed_at), gap)
    window["down_s"] = float(window.get("down_s") or 0.0) + down
    bucket = idle_bucket(window.get("outcome", "no-capacity"))
    window[bucket] = float(window.get(bucket) or 0.0) + (gap - down)
    window["lag_s"] = float(window.get("lag_s") or 0.0) + max(0.0, now - observed_at)
    reason = idle_reason(window)
    last[lane] = {"idle_s": round(idle_s, 1), "reason": reason, "at": time.strftime("%F %T")}
    return idle_s, reason

def idle_metrics(state, now=None):
    """The idle metrics of the lanes: the last closed gap and the gaps still open."""
    now = time.time() if now is None else now
    windows, last = idle_books(state)
    idle_s, reasons, open_s = {}, {}, {}
    for lane, record in last.items():
        if not isinstance(record, dict): continue
        idle_s[lane] = record.get("idle_s"); reasons[lane] = record.get("reason")
    for lane, window in windows.items():
        if not isinstance(window, dict): continue
        since = to_epoch(window.get("since"))
        if since is None: continue
        open_s[lane] = round(max(0.0, now - since), 1)
    return idle_s, reasons, open_s

def tick(lanes, state, last_warn):
    # The snapshot is published at every step of the tick, not only at its end, and a refresher
    # thread republishes it while a slow step runs: a slow harvest, a hung `gh` call or a long
    # refill must not freeze `_at` while the dispatcher keeps working (Refs #1697).
    publisher = StatusPublisher(lanes, state)
    publisher.start()
    try:
        return run_tick(lanes, state, last_warn, publisher)
    finally:
        publisher.stop()

def run_tick(lanes, state, last_warn, publisher):
    """One tick; every step refreshes the snapshot (Refs #1697)."""
    audit_switches()  # the switch audit runs every tick, also when the loop itself is paused (Refs #1827)
    records = pause_records()  # the swrm pause store and the legacy PAUSE markers (Refs #1366, #1592)
    publisher.records = records
    scopes = paused_scopes(records=records)
    if "all" in scopes:
        publisher.publish(res={L: {"lane": "paused"} for L in lanes}, phase="paused")
        return last_warn
    label_cache_reset()  # authoritative label reads are cached for one tick only
    state.setdefault("_done", [])
    idle_books(state)  # the idle bookkeeping of the state exists from the first tick on
    now = time.time()
    # Read the previous heartbeat before publishing marks this tick alive (Refs #1688).
    last_alive = dispatcher_last_alive()
    publisher.publish(phase="probe")
    live = [L for L in lanes if host_ok(lanes[L]["host"])]
    with cf.ThreadPoolExecutor(4) as ex:
        res = dict(zip(live, ex.map(lambda L: remote(L, lanes[L], (state.get(L) or {}).get("run", "")), live)))
    for L in lanes:
        if L not in res: res[L] = {"error": "host in backoff", "skip": True}
    reconcile_review_queue(state, res)
    seen = set()
    recovered_hosts = []
    for L in live:
        h = lanes[L]["host"]
        if h in seen: continue
        errs = [res[x] for x in live if lanes[x]["host"] == h and "error" in res[x]]
        if errs:
            host_fail(h, errs[0]["error"]); seen.add(h)
            note_host_unreachable(state, h)  # the outage is recorded (Refs #1673)
        else:
            host_good(h); seen.add(h)
            if host_came_back(state, h): recovered_hosts.append(h)
    # the hosts that came back: re-check their pauses and verify the runs in flight
    # (Refs #1673).  The edge is consumed here; the verification of a run is retried
    # in the following ticks until it succeeds.
    for h in recovered_hosts:
        release_unreachable_pauses(h, host_pause_paths(h, lanes_of_host(lanes, h)))
        mark_recovery_runs(state, h, lanes)
        scopes = paused_scopes()  # the released pause opens the host again
    release_recovered_budget_holds(state, lanes)
    publisher.publish(res=res, phase="harvest")  # the remote probe of every lane is done
    q = None
    queue_failed = False
    queue_error = ""
    base = None
    outcomes = {}
    freed_lanes = set()
    # The review lanes (CX2/CX4/CX5) take the review cards by their backlog (Refs #1701).
    # The review is chosen at admission time: a work dispatch must not strand a review
    # on an obsolete plan. Waiting cards are counted separately from admitted runs.
    review_ids = review_lanes(lanes)
    memory_rejections = {}
    dispatch_eligible_lanes = set()

    def tick_queue():
        """Load the shared queue on the first dispatch attempt, after that lane's harvest.

        A queue outage must not prevent the remaining lanes from being harvested;
        cache that failure too, so each tick still makes only one queue request."""
        nonlocal q, queue_failed, queue_error
        if q is not None: return True
        if queue_failed: return False
        try: q = queue(state)
        except Exception as e:
            queue_failed = True; queue_error = str(e)
            state["_queue_failures"] = state.get("_queue_failures", 0) + 1
            save_state(state)
            try: log(f"queue error:\n{traceback.format_exc()[-4000:]}", brain=True)
            except Exception: pass  # logging must not replace the queue failure or stop harvest
            if isinstance(e, (RuntimeError, OSError, subprocess.SubprocessError, json.JSONDecodeError)):
                return False
            raise
        state["_queue_failures"] = 0
        try:
            for i in q:
                if "deps_ok" not in i: i["deps_ok"] = deps_met(i)
            metrics = verify_metrics(state)
            for item in q:
                if item.get("verify"):
                    # Use the order time, including time spent waiting while AD was stopped.
                    queued = item.get("queued")
                    metrics["cards"].setdefault(str(item["n"]), {"issue": item["n"], "queued": now if queued is None else queued})
            # Dispatch reads labels in score order before freshen_queue uses the remaining
            # read budget. Prefetching by issue number can starve a high-scoring old card,
            # so the early snapshot counts the rows as read; the authoritative freshen_queue
            # pass runs after the dispatches.
            blocked = sum(not i["deps_ok"] for i in q)
            publisher.publish(res=res, ready=len(q) - blocked, blocked=blocked, phase="deps")
        finally:
            # persist right after a successful read even if the steps above raise; the normal path
            # stamps the fresh counts first so the snapshot is never staler than the saved state
            save_state(state)
        return True

    def base_commit():
        """The upstream base commit of this tick (read once, then cached)."""
        nonlocal base
        if base is None:
            _, out, _ = sh(["git", "ls-remote", BASE_REPO_URL, f"refs/heads/{BASE_BRANCH}"]); base = out.split()[0]
        return base

    def give_card(L, cfg, remote_idle=True):
        """Give the lane its next card; returns the outcome of the attempt.

        The immediate re-dispatch after a run and the ordinary per-tick dispatch share
        this path (Refs #1688): the same lane pause check, the same host check, the same
        `dispatch()` call, so the re-dispatch cannot slip past a pause or a dead host.
        A lane that cannot take a card right now is not retried and holds no slot: the
        next tick tries again."""
        if not host_ok(cfg["host"]): return "no-capacity"
        if lane_paused(L, cfg, scopes): return "paused"
        if remote_idle and res[L].get("lane") != "idle": return "no-capacity"
        if not tick_queue(): return "no-card"
        asked = L  # the lane that was asked for a card (it may hand it to another lane)
        pick = None
        candidates = [i for i in q if i.get("deps_ok", True) and can_take(cfg["backend"], i, L)]
        candidates.sort(key=lambda i: rank(cfg["backend"], i))
        # A harvested lane is available in this tick even if the remote snapshot still
        # calls it busy. Keep the same pause/host/cap checks. The list is read in every
        # tick: the review balancing below needs the lanes that can take a card.
        idle = idle_lane_names(lanes, state, res, scopes, freed_lanes)
        if AD_VERIFY_SHARE_ENABLED:
            verify_slots, work_slots = queue_class_limits(lanes, state, idle)
            metrics = verify_metrics(state)
            metrics["deficit"] = verify_slots
            # The work class may hold at most `total - reserve` lanes: with no work slot left it takes
            # no further lane, the work cards in flight keep the lanes they hold (Refs #1864). The cap
            # is a limit, not a preference: a waiting work card is not dispatched on any lane.
            if not work_slots:
                waiting = [i for i in candidates if not i.get("verify")]
                if waiting:
                    metrics["work_cap_hits"] += 1
                    log(f"queue share: {L} work class at its cap (work slots=0, verify deficit={verify_slots}); "
                        f"{len(waiting)} work card(s) wait", brain=False)
                candidates = [i for i in candidates if i.get("verify")]
            # Admission precedes existing pin/priority/work/number ordering. The verify class takes
            # the reserved lane first; the work class never crosses its cap.
            preferred = True if verify_slots and is_review_lane(L) else None
            if preferred and any(bool(i.get("verify")) == preferred for i in candidates):
                candidates.sort(key=lambda i: bool(i.get("verify")) != preferred)
                log(f"queue share: {L} prefers verify (verify deficit={verify_slots}, work slots={work_slots}); other class yields", brain=False)
        if candidates: dispatch_eligible_lanes.add(L)
        # Share a probe across this lane's candidates only. Another lane on the
        # same host must recheck after a session starts or memory recovers.
        memory_cache = {}
        eligible = []
        for candidate in candidates:
            rejection = memory_preflight(L, cfg, candidate, memory_cache)
            if rejection:
                memory_rejections.setdefault(candidate["n"], {})[L] = rejection
                detail = rejection["available_mb"]
                shown = "unavailable" if detail is None else f"{detail}MB"
                log(f"{L} #{candidate['n']} skipped: blocked-by-host-memory host={rejection['host']} available={shown} threshold={rejection['threshold_mb']}MB", brain=False)
                continue
            eligible.append(candidate)
        for c in candidates:
            if c not in eligible: continue
            status, detail = label_state(c["n"])
            if status == "fresh":
                try:
                    if not pr_allows_requeue(c["n"]):
                        q.remove(c); continue
                except Exception as exc:
                    log(f"WARNING skip #{c['n']}: PR lookup/repair failed ({exc})", brain=True)
                    continue
                if hot_verdict(c.get("body", ""), hot_inflight(state))["blocked"]: continue
                pick = c; break
            if status == "stale":
                q.remove(c); skip_stale(c["n"], detail)  # try the next one this tick
                continue
            # the authoritative read failed: keep the row (it may still be dispatchable),
            # never count it as a stale skip and never dispatch on unverified labels
            log_unverified(c["n"], status, detail, "kept in queue (not dispatched this tick)")
        if not pick: return "no-card"
        if pick.get("verify"):
            # The review card goes to the review lane with the smallest review backlog:
            # equal backlogs rotate over the review lanes and a lane that is at its
            # in-flight cap is skipped - the review waits for a free lane (Refs #1701).
            running = review_in_flight(review_ids, state)
            waiting = [i for i in q if i is not pick and i.get("deps_ok", True) and i.get("verify")]
            # Memory admission applies to the lane that finally takes the review, not only to the
            # lane that was asked: a balanced-away review must not land on a host short of memory.
            eligible = []
            for lane in idle:
                if lane not in review_ids or not can_take(lanes[lane]["backend"], pick, lane): continue
                rejection = memory_preflight(lane, lanes[lane], pick, memory_cache)
                if rejection:
                    memory_rejections.setdefault(pick["n"], {})[lane] = rejection
                    shown = "unavailable" if rejection["available_mb"] is None else f"{rejection['available_mb']}MB"
                    log(f"{lane} #{pick['n']} review skipped: blocked-by-host-memory host={rejection['host']} available={shown} threshold={rejection['threshold_mb']}MB", brain=False)
                    continue
                eligible.append(lane)
            queued = review_waiting_counts(review_ids, waiting, running, head_eligible=eligible)
            # Rank the shared forecast; cap real queued and running reviews.
            # Shared waiting demand must not prevent an idle lane from draining it.
            loads = {lane: running[lane] + queued.get(lane, 0) for lane in review_ids}
            chosen, snapshot = pick_reviewer(review_ids, loads, eligible=eligible,
                                             max_in_flight=REVIEW_MAX_IN_FLIGHT,
                                             in_flight=running,
                                             pr=review_target_pr(pick),
                                             log=lambda row: log(row, brain=False))
            if chosen is None: return "no-capacity"
            if chosen != L:
                # The review belongs to another review lane: it takes the card, the lane
                # that was asked keeps its slot free (the next tick tries again).
                # recorded as dispatched only after the dispatch succeeded (below): a failed
                # dispatch must leave the lane free for the rest of this tick
                L, cfg = chosen, lanes[chosen]
            pick["tester_risk"] = tester_risk_decision(pick["body"])
        blocked = sum(not i["deps_ok"] for i in q)
        publisher.publish(res=res, ready=len(q) - blocked, blocked=blocked, phase="dispatch")
        commit = base_commit()
        gh(["issue", "edit", str(pick["n"]), "--add-label", "in-progress"])
        if pick.get("verify"):
            state.setdefault("_review_queued", {})[L] = {"issue": pick["n"]}
            save_state(state)
        try:
            if not pr_allows_requeue(pick["n"]): return "no-card"
            run, ver = dispatch(L, cfg, pick, commit)
        except Exception as e:
            if pick.get("verify"):
                state["_review_queued"].pop(L, None)
                save_state(state)
            dispatch_failed(L, cfg, pick, e, busy=bool(LANE_BUSY_RE.search(str(e))))
            return "no-capacity"
        try: allowed = pr_allows_requeue(pick["n"])
        except Exception as exc:
            log(f"WARNING #{pick['n']} post-dispatch PR lookup failed ({exc}); cancelling", brain=True)
            allowed = False
        if not allowed:
            pending = {"issue": pick["n"], "run": run, "cancel_pending": True}
            state[L] = pending; save_state(state)
            if cancel_guarded_dispatch(L, cfg, pending): state.pop(L); save_state(state)
            q.remove(pick)
            return "no-capacity"
        if L != asked: outcomes[L] = "dispatched"
        q.remove(pick)
        record = {"issue": pick["n"], "run": run, "host": cfg["host"], "base": commit, "at": time.strftime("%F %T"), "verify": ver, "class": "verify" if pick.get("verify") else "work", "hot": hot_files(pick.get("body", ""))}
        with publisher._lock:
            state[L] = record
            if pick.get("verify"):
                state["_review_queued"].pop(L, None)
                record["review_queued"] = True
            state.setdefault("_pool_entered_at", {}).pop(str(pick["n"]), None)
            metrics = verify_metrics(state)
            # The snapshot and saved state must reflect the slot just occupied.
            if AD_VERIFY_SHARE_ENABLED: metrics["deficit"] = queue_class_limits(lanes, state, idle)[0]
            metrics["dispatches"]["all"] += 1
            if pick.get("verify"):
                metrics["dispatches"]["verify"] += 1
                record_verify_time(state, pick["n"], "dispatched", time.time())
            if pick.get("verify"):
                state[L]["tester_risk"] = pick["tester_risk"]
                record_tester_risk(state, pick["n"], pick["tester_risk"])
            publisher.record_dispatch(L, record)
        if pick.get("verify"):
            # the review decision goes to the log with the backlog snapshot of that moment
            # (the PR, the chosen lane and the load of every review lane) (Refs #1701)
            backlog = ", ".join(f"{lane}={snapshot[lane]}" for lane in review_ids)
            log(f"review assignment: PR #{review_target_pr(pick)} → lane {L} (backlog: {backlog})")
        # Stop this lane's gap at its own successful dispatch. Later lanes may take
        # minutes to assign; their work cannot extend an already occupied lane's gap.
        closed = idle_close(state, L, time.time(), last_alive, tick_started_at=now)
        if closed:
            idle_s, reason = closed
            if idle_s > IDLE_LOG_MIN_S:
                log(f"{L} idle {idle_s:.0f}s before dispatch (reason: {reason})", brain=True)
        if AD_VERIFY_SHARE_ENABLED: log(f"queue share: dispatched {state[L]['class']} #{pick['n']} on {L}", brain=False)
        score, basis = priority_score(cfg["backend"], pick)
        save_state(state); log(f"dispatched #{pick['n']} → {L} ({cfg['host']}) run {run} base {commit[:8]} "
                               f"priority={score:g} basis={json.dumps(basis, sort_keys=True)}")
        # the lane that was asked keeps its slot free when the card went to another lane
        return "dispatched" if L == asked else "no-capacity"

    # 1) Re-dispatch each freed lane before harvesting the next one (Refs #1688).
    #    Another lane's slow fetch/push/PR creation must not hold up this lane.
    for L, r in res.items():
        st = state.get(L)
        if "error" in r: continue
        freed = False
        if st and st.get("cancel_pending"):
            if cancel_guarded_dispatch(L, lanes[L], st): state.pop(L); save_state(state)
            outcomes[L] = "no-capacity"
            continue
        if not st:
            recovery_runs(state).pop(L, None)  # the lane is free: nothing left to verify
        elif isinstance(r.get("run"), dict):
            if lane_in_fetch_backoff(st): continue  # harvest fetch is backing off after a transient failure (Refs #947)
            # the run ended: the lane is idle from its own end time on (Refs #1688)
            idle_open(state, L, r["run"].get("ended_at"), now, last_alive)
            result = finish(L, lanes[L], st, r["run"], state)
            # the harvest may have cooled down a lane (a startup failure pauses its
            # lane): the pause must be visible to the rest of this tick
            scopes = paused_scopes()
            if result.get("done"):
                recovery_runs(state).pop(L, None)
                state.pop(L); state.get("_review_queued", {}).pop(L, None); save_state(state); freed = True
            if result.get("error"): log(f"{L} #{st['issue']} run {st['run']}: {result['error']}", brain=False)
        elif reconcile(L, lanes[L], st, r, state):
            recovery_runs(state).pop(L, None)
            state.pop(L); state.get("_review_queued", {}).pop(L, None); save_state(state); freed = True
        elif verify_recovered_run(L, lanes[L], st, r, state):
            state.pop(L); state.get("_review_queued", {}).pop(L, None); save_state(state); freed = True
        if freed:
            freed_lanes.add(L)
            outcomes[L] = give_card(L, lanes[L], remote_idle=False)
        # The harvest of one lane can take minutes: refresh after every lane.
        publisher.publish(res=res, phase="harvest")
    queue_ok = tick_queue()
    update_busy_stale(lanes, state, res)
    if not queue_ok:
        # the pool level is unknown: the refill works on the last known level and is bounded
        # (Refs #1662)
        refill_on_unreadable_queue(state)
        with publisher._lock:
            st = status_snapshot(lanes, state, res, None, None, records)
            if isinstance(st, dict):
                st["_phase"] = "queue"
                st["_tick_at"] = publisher.tick_at or st.get("_at")
            st["_queue_error"] = queue_error or "queue unavailable"
            st["_ready_estimate"] = pool_level(state)  # the level the refill worked on (not authoritative)
            write_status_snapshot(publisher.path, st, publisher.logger)
        return last_warn
    # 2) the lanes that were idle before this tick
    for L, cfg in lanes.items():
        if L in outcomes: continue
        r = res[L]
        # a paused lane is paused before anything else is looked at (Refs #1688)
        if lane_paused(L, cfg, scopes): outcomes[L] = "paused"; continue
        if "error" in r or state.get(L) or r.get("lane") != "idle":
            outcomes[L] = "no-capacity"; continue
        outcomes[L] = give_card(L, cfg)
    # 3) successful dispatches already closed their windows; account the others
    windows, _ = idle_books(state)
    for L, window in list(windows.items()):
        idle_account(state, L, window, now, outcomes.get(L, "no-capacity"), last_alive)
    # stale search rows are not dispatchable: drop them (and log the skip) before counting
    q = freshen_queue(q, state)
    state["_pool_entered_at"] = {str(item["n"]): item["pool_entered_at"] for item in q if item.get("deps_ok", True) and "pool_entered_at" in item}
    # Publish the complete remaining queue, including overflow beyond the admission cap.
    if review_ids:
        running = review_in_flight(review_ids, state)
        waiting_reviews = [i for i in q if i.get("deps_ok", True) and i.get("verify")]
        state["_review_waiting"] = review_waiting_counts(review_ids, waiting_reviews, running, REVIEW_MAX_IN_FLIGHT)
        if waiting_reviews:
            backlog = ", ".join(f"{lane}={count}" for lane, count in review_backlog(review_ids, state).items())
            log(f"review queue: {len(waiting_reviews)} card(s) wait (backlog: {backlog})", brain=False)
        save_state(state)
    blocked = sum(not i["deps_ok"] for i in q)
    memory_blocks = {}
    for item in q:
        # A lane that took a card (or failed dispatch) no longer offers capacity
        # to the remaining queue, even though it was eligible earlier this tick.
        candidates = [L for L in dispatch_eligible_lanes
                      if outcomes.get(L) == "no-card" and not state.get(L)
                      and can_take(lanes[L]["backend"], item, L) and item.get("deps_ok", True)]
        rejections = memory_rejections.get(item["n"], {})
        if candidates and all(L in rejections for L in candidates):
            memory_blocks[item["n"]] = list(rejections.values())
    hot_blocked = {i["n"]: verdict for i in q if i.get("deps_ok", True)
                   for verdict in [hot_verdict(i.get("body", ""), hot_inflight(state))]
                   if verdict["blocked"]}
    details = blocked_details(q, hot_blocked, memory_blocks, state)
    blocked = len(details)
    ready = len(q) - blocked
    publisher.publish(res=res, ready=ready, blocked=blocked, phase="queue")  # the queue counters are measured
    # refill when the queue is short, or when some backend's lanes could not all be fed (counting cross-pin fallback)
    short = {b: sum(c["backend"] == b for c in lanes.values()) - sum(1 for i in q if i.get("deps_ok", True) and i["n"] not in hot_blocked and i["n"] not in memory_blocks and can_take(b, i, verify_gate=False))
             for b in {c["backend"] for c in lanes.values()}}
    starve = max(short.values(), default=0)
    need = refill_need(ready, low=LOW, target=TARGET, starved=starve)
    filed = []
    if need > 0:
        publisher.publish(res=res, ready=ready, blocked=blocked, phase="refill")
        why = f"queue low ({ready} < {LOW})" if ready < LOW else f"backend starved ({', '.join(f'{b} short {k}' for b, k in short.items() if k > 0)})"
        if mint_paused(scopes):
            if time.time() - last_warn > 600:
                log(f"{why}: mint paused, no new issue minted", brain=False); last_warn = time.time()
        else:
            filed = replenish(need, state)
            if filed: log(f"{why}: filed {len(filed)} issue(s) from backlog: {' '.join(filed)}")
            elif time.time() - last_warn > 600:
                log(f"WARNING queue low: {ready} dispatchable lane-ready issue(s) left (+{blocked} waiting on 依赖), need {need} card(s) (threshold {LOW}, target {TARGET}), none filed{mint_evidence()}"); last_warn = time.time()
        publisher.publish(res=res, ready=ready, blocked=blocked, phase="refill")  # the (slow) refill is done
    # the level of this tick is authoritative: it replaces the estimate the AD used while the
    # queue was unreadable, and the cards minted since then are counted on top of it (Refs #1662)
    state["_ready"] = ready; state["_minted"] = len(filed); state["_queue_failures"] = 0
    state["_tick_at"] = now
    observe_verify_verdicts(state)
    save_state(state)
    refresh_minimax_burn()  # the burn watchdog refreshes the reading the status shows (Refs #1671)
    with publisher._lock:
        publisher.memory_blocks = memory_blocks
        publisher.blocked_details = details
        publisher.publish(res=res, ready=ready, blocked=blocked, phase="idle")
    return last_warn

def preflight_card(text):
    """Reject a card that the card lint refuses; a missing lint script also refuses the card (fail closed, Refs #1732)."""
    script = next((p for p in (f"{LOCAL}/swrm/bin/swrm",
                               os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "swrm", "bin", "swrm"))
                   if os.path.isfile(p)), None)
    if script is None:
        log("card preflight unavailable: swrm/bin/swrm not found, card refused", brain=False)
        raise RuntimeError("card preflight unavailable: swrm/bin/swrm not found")
    result = subprocess.run([sys.executable, script, "card-lint", "-"],
                            input=text, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"card preflight rejected: {result.stdout.strip()} {result.stderr.strip()}")


def refill_need(ready, low=None, target=None, starved=0):
    """How many cards the pool needs (Refs #1662).

    Below LOW the pool is topped up to at least LOW or TARGET, whichever is higher.
    This preserves the low-water trigger when AD_LOW exceeds AD_TARGET.  A backend
    that cannot be fed can request more cards than the pool deficit."""
    low = LOW if low is None else low
    target = TARGET if target is None else target
    if ready < low: return max(target - ready, low - ready, starved, 0)
    return max(starved, 0)

def card_lines(text):
    """Read the first two nonempty header lines, keeping the body formatting intact."""
    lines = text.splitlines(); header = []
    for index, line in enumerate(lines):
        if not line.strip(): continue
        header.append(line.strip())
        if len(header) == 2: return header + lines[index + 1:]
    return header

def card_defect(text, strict=False):
    """Why a card cannot be minted as a lane-ready issue (None when it can).

    A card needs a `# ` title, a `labels:` line and a body: without them a lane has
    nothing to run.  A card of the repo backlog (`docs/backlog/next/*.md`, written by a
    planner run) must also carry the published contract of that batch: a body of at least
    two lines and an `验收：` acceptance line (the same rules as `is_thin_card` of
    tools/check-ad-ready-target.py).  The local backlog is the operator's drop box: a card
    there is minted as long as a lane can run it, because the acceptance line is the
    convention of the planner cards and not of the local ones.  A card without any of that
    can never be run by a lane, so minting it only fills the pool with dead cards
    (Refs #1662)."""
    lines = [line.strip() for line in card_lines(text) if line.strip()]
    if len(lines) < 2: return "thin card: needs a title and a labels line"
    if not lines[0].startswith("# "): return "thin card: the first line is not a '# ' title"
    if not lines[1].startswith("labels:"): return "thin card: the second line is not a 'labels:' line"
    if strict:
        if len(lines) < 4: return "thin card: needs a title, a labels line, a body and an acceptance line"
        if len(lines[2:]) < 2: return "thin card: no body"
        if not any(line.startswith("验收：") for line in lines[2:]): return "thin card: no '验收：' acceptance line"
    elif not lines[2:]: return "thin card: no body"
    return None

def mint_evidence():
    """One-line summary of the last refill attempt, for the queue-low warning."""
    parts = []
    if MINT_EVIDENCE.get("rejected"): parts.append(f"{MINT_EVIDENCE['rejected']} card(s) rejected")
    if MINT_EVIDENCE.get("planner"): parts.append(f"planner {MINT_EVIDENCE['planner']}")
    return f" ({'; '.join(parts)})" if parts else ""

def move_without_overwrite(source, destination):
    """Move a backlog card without ever overwriting an earlier card of the same name.

    The name is reserved with an exclusive create, so two AD processes cannot pick the
    same free name; an existing card (a card already quarantined in `rejected/`, a card
    already filed in `filed/`) keeps its content and the moved card gets the next free
    serial suffix (Refs #1662)."""
    stem, suffix = os.path.splitext(destination)
    target = destination; serial = 0
    while True:
        try: handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            serial += 1; target = f"{stem}.{serial}{suffix}"; continue
        os.close(handle)
        try: os.replace(source, target)
        except OSError:
            os.unlink(target); raise
        return target

def pool_level(state):
    """The pool level the AD knows: the last authoritative count plus the cards minted since.

    While the queue cannot be read there is no authoritative count; the cards minted in the
    meantime are added to the last known level, so a refill does not mint the same batch
    over and over (Refs #1662)."""
    ready = state.get("_ready")
    if isinstance(ready, bool) or not isinstance(ready, int): return None
    return ready + int(state.get("_minted", 0) or 0)

def refill_on_unreadable_queue(state):
    """Refill while the queue cannot be read, but only within a bounded number of retries.

    The pool level is unknown when the queue read fails.  The AD mints against the last
    known level (plus the cards it minted since), counts the consecutive failures and
    mints nothing any more once the bound is reached: it then waits for an authoritative
    read, so a broken `gh` cannot flood the backlog with duplicate cards (Refs #1662)."""
    # tick_queue() already counted this tick's failed read (one count per tick)
    failures = max(1, int(state.get("_queue_failures", 0) or 0))
    state["_queue_failures"] = failures
    level = pool_level(state)
    if level is None:
        log(f"queue unreadable #{failures}: pool level unknown, no card minted (waiting for an authoritative read)", brain=False)
        save_state(state); return []
    if failures > QUEUE_UNREADABLE_REFILL_MAX:
        log(f"queue unreadable #{failures}: no card minted any more (bounded retries exhausted, waiting for an authoritative read)", brain=False)
        save_state(state); return []
    need = refill_need(level)
    if need <= 0:
        log(f"queue unreadable #{failures}: pool level {level} is above the threshold ({LOW}), no card minted", brain=False)
        save_state(state); return []
    log(f"queue unreadable #{failures}: minting {need} card(s) for the last known level ({level} dispatchable)", brain=False)
    filed = replenish(need, state)
    state["_minted"] = int(state.get("_minted", 0) or 0) + len(filed)
    save_state(state)
    return filed

def file_issue(text, extra=()):
    preflight_card(text)
    lines = card_lines(text)
    title = lines[0].lstrip("# ").strip(); labels = [l.strip() for l in lines[1].split(":", 1)[1].split(",") if l.strip()]
    body = "\n".join(lines[2:]).strip() + "\n"
    args = ["issue", "create", "-t", title, "-b", body]
    for l in ["lane-ready", *labels, *extra]: args += ["-l", l]
    rc, o, e = gh(args)
    if rc: raise RuntimeError(e.strip()[-200:])
    return "#" + o.strip().rsplit("/", 1)[-1]

CARD_MARK = "ad-card:"
CREATE_SETTLE_S = max(120.0, env_number("AD_CREATE_SETTLE_S", 300.0))  # never below twice the gh timeout  # create timeout + server propagation: a request may still be in flight until then
CREATE_EMPTY_PROOFS = 2   # consecutive empty listings, all taken after the settle time, before a card is created again
LOOKBACK_SKEW_S = 300  # clock skew allowed between this host and the server when a listing is used as proof

def card_marker(key, text):
    """The identity of one card: its source and its text (written into the body of the issue)."""
    return hashlib.sha256((key + "\n" + text).encode("utf-8")).hexdigest()[:16]

def iso_epoch(value):
    """Epoch seconds of a UTC ISO-8601 stamp such as 2026-11-10T01:02:03Z, else None."""
    try: return datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError: return None

def find_issue_by_marker(marker, since):
    """The issue whose body carries this card marker: "#N", "" when it provably does not exist, None when unknown.

    The newest issues are listed (the list is read-your-writes consistent, unlike the search index, so a lag
    cannot hide an issue). Absence is proven only when the page is not full or reaches back to before the
    creation attempt (`since`, epoch seconds); otherwise the answer is unknown and the card must not be retried."""
    limit = 200
    rc, o, _ = gh(["issue", "list", "--state", "all", "--limit", str(limit), "--json", "number,body,createdAt"])
    if rc: return None
    try: rows = json.loads(o)
    except ValueError: return None
    if not isinstance(rows, list): return None
    for row in rows:
        if isinstance(row, dict) and row.get("number") is not None and f"{CARD_MARK}{marker}" in str(row.get("body") or ""):
            return "#" + str(row["number"])
    if len(rows) < limit: return ""
    created = [iso_epoch(row.get("createdAt")) if isinstance(row, dict) else None for row in rows]
    if created and all(c is not None for c in created) and min(created) <= float(since) - LOOKBACK_SKEW_S:
        return ""
    return None

def file_issue_once(text, key, state):
    """File the card once, even when the answer of `gh issue create` is lost or the process stops (Refs #1662).

    The body of the issue carries a marker of the card. Before the call the card is persisted as pending;
    after it, in ONE save, the pending record is replaced by a done record, so a stop between the two
    leaves either a pending card (looked up by its marker) or a done one, never nothing. A pending card is
    created again only when the request has certainly ended (CREATE_SETTLE_S) and the listing of the newest
    issues proves twice that it does not exist; an unknown answer (failed or truncated listing, a request that
    may still be in flight) never retries. Returns the issue, or None when the card must wait for the next tick."""
    pending = state.setdefault("_import_pending", {})
    done = state.setdefault("_import_done", {})
    marker = card_marker(key, text)
    if marker in done:
        return done[marker] + "(already filed)"
    if marker in pending:
        found = find_issue_by_marker(marker, float(pending[marker]))
        if found is None:
            log(f"card {key}: a pending creation cannot be checked (issue list failed or truncated); not created again, checked next tick", brain=False); return None
        if found:
            done[marker] = found; pending.pop(marker, None); save_state(state)
            log(f"card {key}: the earlier creation reached the server as {found}; not filed again", brain=False)
            return found + "(already filed)"
        # An empty listing is not proof: the lost request may still be in flight on the server and
        # succeed after the listing. Create again only once the request has certainly ended (settle
        # time passed) and the listing stayed empty twice after that.
        empties = state.setdefault("_import_empty", {})
        if time.time() - float(pending[marker]) < CREATE_SETTLE_S:
            log(f"card {key}: the earlier creation may still be in flight; checked again next tick", brain=False); return None
        empties[marker] = int(empties.get(marker, 0)) + 1
        if empties[marker] < CREATE_EMPTY_PROOFS:
            save_state(state)
            log(f"card {key}: not found once after the settle time; confirmed again next tick", brain=False); return None
        empties.pop(marker, None)
    pending[marker] = time.time(); save_state(state)
    issue = file_issue(text.rstrip("\n") + f"\n\n<!-- {CARD_MARK}{marker} -->\n")
    done[marker] = issue; pending.pop(marker, None); save_state(state)
    return issue


def record_mint(record):
    ledger = load(f"{D}/mint-log.json", [])
    if not isinstance(ledger, list): ledger = []
    ledger.append(record)
    tmp = f"{D}/mint-log.json.tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(ledger[-100:], stream, ensure_ascii=False, indent=1)
    os.replace(tmp, f"{D}/mint-log.json")


def mint_card(text, state, counts, source):
    lines = card_lines(text)
    if len(lines) < 2 or not lines[0].startswith("#") or not lines[1].startswith("labels:"):
        record_mint({"at": time.strftime("%F %T"), "source": source,
                     "minted": False, "reason": "bad_format"})
        raise ValueError("bad card format")
    verdict = hot_verdict("\n".join(lines[2:]), counts)
    record = {"at": time.strftime("%F %T"), "title": lines[0].lstrip("# ").strip(),
              "source": source, "preflight": verdict, "minted": False, "issue": None}
    if verdict["blocked"]:
        # A card that conflicts on a hot file is never created, not even for a starved
        # lane: the caller moves on to the next card that does not conflict (Refs #1721).
        record["reason"] = "hot_file_conflict"
        record_mint(record)
        log(f"mint refused {record['title']}: {verdict['advice']} {verdict['conflicts']}", brain=False)
        return None
    # Persist the decision before the external write, including if issue creation fails.
    record_mint({**record, "reason": "mint_pending"})
    try:
        ref = file_issue_once(text, source, state)
    except Exception:
        record_mint({**record, "reason": "mint_failed"})
        raise
    if ref is None:  # the earlier creation of this card is not settled: nothing new is created, retried next tick
        record_mint({**record, "reason": "creation_pending"})
        return None
    record.update(minted=True, issue=re.sub(r"\(.*\)$", "", ref).lstrip("#"))
    for path in verdict["files"]: counts[path] = counts.get(path, 0) + 1
    try: record_mint(record)
    except OSError as e: log(f"mint ledger final write failed: {e}", brain=False)
    return ref


PLANNER = """# 规划：为 lane 队列写 {k} 条新任务（{tag}）
labels: p1,backend:codex,planner
自动补队列任务。读 main 上的代码与 `docs/reviews/`、`docs/scenario/`、`examples/org-health-local/`，写 {k} 个新文件 `docs/backlog/next/{tag}-01.md` … `{tag}-{k:02d}.md`，每个文件是一条可独立派给 lane 的小任务，格式严格为：

```
# <中文标题>
labels: <p1 或 p2>,backend:<pi|codex|agy>
<任务正文：要改的文件路径、具体改动、边界；不得依赖其它未完成任务>

热文件：`<要改的仓库相对路径>`（多个用逗号分隔）

验收：`<一条在仓库根或 cd 子目录后可直接运行、只用 python3 标准库/现有脚本的命令>`

## 失败模式清单
对照 docs/defect-patterns.md 检查相关失败模式。
```

工具类卡的 plan 段必须列「复刻的引擎数据形状清单」，验收含真实形状 fixture 门槛。
派发/暂停/终态类卡的 design 段须逐项填写状态机边例矩阵的预期行为：单后端 / drain / 暂停 / 已派发后变化 / 合并后清理 / dry-run 无副作用。
涉及派发行为的验收必须写明 tick/集成级测试，仅函数级单测不算关账。
每条验收须有明确判定方法；逐卡运行 `swrm/bin/swrm card-lint`，通过后才可导入。

要求：{k} 条里 backend:pi / backend:agy / backend:codex / 无 backend 标签 大约 2:1:2:1（至少 3 条无 backend 标签供饥饿兜底）；优先 #6 三 Bot 组织诊断 e2e（examples/org-health-local）的测试覆盖、文档、健壮性小修，其次 swrm 的纯单元测试（不需要 Unix socket 的可标 codex，需要的标 pi）；每条 15 分钟内可完成；不要写安全加固、不要改 CI 的 macOS 矩阵、不要碰签名；不要与下列已有 issue 重复：

{open}

生成前逐项完成以下检查，并把相关证据写入各卡正文：
- 对照 main 确认目标文件确实存在。
- 写明当前行为和期望行为之间的差异。
- 给出能复现问题的输入与目标对象。
- 明确本卡覆盖的功能范围。
- 写清不在本卡范围内的工作。
- 确认不依赖另一张未完成卡。
- 检查已有 issue 中的同类问题。
- 给出最小实现入口和调用者。
- 标明输入字段的实际来源。
- 给出输出结构及消费方。
- 工具卡列出复刻的数据形状。
- 工具卡引用真实 fixture 的来源。
- fixture 必须覆盖真实嵌套字段。
- 派发卡填写单后端的预期结果。
- 派发卡填写 drain 的预期结果。
- 派发卡填写暂停的预期结果。
- 派发卡填写已派发后变化的预期结果。
- 派发卡填写合并后清理的预期结果。
- 派发卡填写 dry-run 的无副作用断言。
- 派发卡指定 tick 或等价集成测试入口。
- 验收命令应能从仓库根运行。
- 验收写明返回码或可观察判定结果。
- 每条验收对应一个明确的行为。
- 保留失败路径和边界输入的测试要求。
- 对照缺陷模式库说明相关风险。
- 检查测试是否针对真正的目标对象。
- 检查覆盖范围与结论范围是否一致。
- 检查状态写入后是否按同一结构读回。
- 检查冲突配置的优先级预期。
- 检查清理后待办任务是否仍存在。
- 对每张卡运行预审，逐条修正拒绝原因。

只新增 `docs/backlog/next/` 下的文件，不改其它文件。

验收：`ls docs/backlog/next/{tag}-*.md | wc -l | grep -qx {k}`

## 失败模式清单
对照 docs/defect-patterns.md；检查生成对象和全量卡数，拒绝薄卡及不可测条目。
"""

def replenish(k, state):
    """Queue refill, in order: (1) ~/phanes-ctl/autodispatch/backlog/*.md (`# title` / `labels: a,b` / body; moved to backlog/filed/);
    (2) docs/backlog/next/*.md on origin/main not yet imported (written by planner runs); (3) if still short and no planner
    issue is open, file one planner issue asking a lane to write the next batch into docs/backlog/next/.

    A card that does not satisfy the card rules (`card_defect`) is quarantined in backlog/rejected/
    with its reason and the rest of the batch is still minted: one unusable card must never block
    the whole refill, and the reason is logged so a silent pool can be diagnosed (Refs #1662).
    A quarantined card never overwrites an earlier card of the same name (Refs #1662)."""
    global LAST_MINT_LOG
    if mint_paused():
        log("replenish skipped: mint paused (PAUSE-MINT marker or the mint pause scope)", brain=False); return []
    bd = f"{D}/backlog"; os.makedirs(f"{bd}/filed", exist_ok=True); os.makedirs(f"{bd}/rejected", exist_ok=True); out = []
    counts = hot_inflight(state)
    MINT_EVIDENCE.clear(); MINT_EVIDENCE["need"] = k; rejected = 0
    for f in sorted(x for x in os.listdir(bd) if x.endswith(".md")):
        if len(out) >= k: break
        try:
            with open(f"{bd}/{f}") as stream: text = stream.read()
        except OSError as e: log(f"backlog {f}: {e}", brain=False); continue
        defect = card_defect(text)
        if defect:
            target = move_without_overwrite(f"{bd}/{f}", f"{bd}/rejected/{f}")
            rejected += 1
            record_mint({"at": time.strftime("%F %T"), "source": f"backlog/{f}", "minted": False, "reason": "bad_format", "detail": defect})
            log(f"backlog {f}: {defect} → {target}", brain=False); continue
        try: issue = mint_card(text, state, counts, f"backlog/{f}")
        except Exception as e:
            log(f"backlog {f}: {e}", brain=False); continue  # one failing card must not block the batch
        if issue is None: continue  # refused by the hot-file gate, or its earlier creation is not settled
        out.append(issue)
        move_without_overwrite(f"{bd}/{f}", f"{bd}/filed/{f}")
    MINT_EVIDENCE["rejected"] = rejected
    G = ["git", "-C", "/workspace/phanes-repo"]
    sh(G + ["fetch", "-q", "origin", "main"], timeout=120)
    rc, ls, _ = sh(G + ["ls-tree", "--name-only", "origin/main", "docs/backlog/next/"])
    imp = state.setdefault("_imported", [])
    for path in sorted(x for x in ls.split() if x.endswith(".md")) if not rc else []:
        if len(out) >= k: break
        if path in imp: continue
        read_rc, text, _ = sh(G + ["show", f"origin/main:{path}"])
        if read_rc: log(f"repo backlog {path}: read failed (rc={read_rc})", brain=False); continue
        defect = card_defect(text, strict=True)
        if defect:
            imp.append(path); save_state(state)
            log(f"repo backlog {path}: {defect} → skipped", brain=False); continue
        try: issue = mint_card(text, state, counts, path)
        except Exception as e:
            log(f"repo backlog {path}: {e}", brain=False); continue  # keep the card unimported: it is retried next tick
        if issue is None: continue
        imp.append(path); save_state(state); out.append(issue)
    if len(out) < k:
        rc, o, _ = gh(["issue", "list", "--state", "open", "--label", "planner", "--json", "number"])
        planners = None
        if rc:
            # the gate cannot prove that no planner is open: no card is minted (Refs #1662)
            MINT_EVIDENCE["planner"] = f"issue list failed (rc={rc})"
            log(f"planner query unreadable: gh issue list failed (rc={rc}); skip planner creation", brain=False)
        else:
            try:
                planners = json.loads(o)
                if not isinstance(planners, list): raise ValueError("the planner query did not return a JSON list")
            except (ValueError, TypeError) as e:
                planners = None
                # an unreadable planner query cannot prove that no planner is open: the gate stays
                # closed instead of filing a second planner card (Refs #1662)
                MINT_EVIDENCE["planner"] = f"query unreadable ({str(e)[:60]})"
                log(f"planner query unreadable: the planner JSON cannot be parsed ({e}); skip planner creation", brain=False)
        if planners:
            numbers = [i["number"] for i in planners if isinstance(i, dict) and i.get("number") is not None]
            MINT_EVIDENCE["planner"] = f"issue #{numbers[0]} already open" if numbers else "a planner issue is already open"
            if time.time() - LAST_MINT_LOG > 600:
                log(f"planner issue already open ({', '.join('#' + str(n) for n in numbers) or 'unknown number'}): no new planner card minted", brain=False); LAST_MINT_LOG = time.time()
        elif planners == []:
            rc, o, _ = gh(["issue", "list", "--state", "open", "--limit", "200", "--json", "number,title"])
            titles = ""
            if not rc:
                try: titles = "\n".join(f"- #{i['number']} {i['title']}" for i in json.loads(o))
                except (ValueError, TypeError, KeyError) as e: log(f"open issue list unreadable ({e}); the planner card carries no issue list", brain=False)
            tag = time.strftime("%Y%m%d-%H%M")
            try:
                out.append(file_issue(PLANNER.format(k=20, tag=tag, open=titles)) + "(planner)"); MINT_EVIDENCE["planner"] = "card filed"
            except Exception as e:
                MINT_EVIDENCE["planner"] = f"card failed ({str(e)[:60]})"; log(f"planner issue: {e}", brain=False)
    return out

def sleep_chunk(total=TICK, chunk=5):
    """Sleep between ticks in chunks <= 5s, refreshing heartbeat on each step to prevent suspend freeze (Refs #906)."""
    rem = max(0, total)
    if rem == 0:
        write_heartbeat()
        return
    while rem > 0:
        write_heartbeat()
        step = min(rem, chunk)
        time.sleep(step)
        rem -= step
    write_heartbeat()


SHADOW_VERDICT_LOG = f"{D}/shadow-verdicts.jsonl"  # append-only tester shadow log (Refs #1986)


def _validate_shadow(row):
    verdicts = ("pass", "fail", "insufficient_evidence")
    if type(row.get("pr")) is not int or row["pr"] <= 0:
        raise ValueError("shadow PR must be a positive integer")
    if row.get("lane_verdict") not in verdicts or row.get("tester_verdict") not in verdicts:
        raise ValueError("invalid shadow verdict")
    stamp = row.get("timestamp")
    if not isinstance(stamp, str):
        raise ValueError("shadow timestamp must be an ISO-8601 string")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("shadow timestamp requires a timezone")
    if row.get("final_outcome") not in (None, "pass", "fail"):
        raise ValueError("final outcome must be pass or fail")


def _shadow_samples(stream):
    samples = {}
    for line in stream:
        if not line.endswith("\n"):
            raise ValueError("incomplete shadow log record")
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("invalid shadow log record")
        _validate_shadow(row)
        old = samples.get(row["pr"])
        if old:
            if any(old[k] != row[k] for k in ("lane_verdict", "tester_verdict")):
                raise ValueError("conflicting shadow verdict")
            if old.get("final_outcome") not in (None, row.get("final_outcome")):
                raise ValueError("conflicting final outcome")
        samples[row["pr"]] = row
    return samples


def record_shadow_verdict(path=None, *, pr, lane_verdict, tester_verdict, timestamp,
                          final_outcome=None):
    """Append observational evidence only; never dispatch, merge, or alter gates.

    One immutable pair of opinions per PR. Retries (including a new timestamp)
    are no-ops; final pass/fail correctness may be filled once. Insufficient
    evidence is an abstention, never a hit. The log is append-only JSONL; the
    default path is the control plane shadow log, a caller may pass its own.
    I/O/corruption errors propagate; callers must not interpret them as approval.
    """
    import fcntl
    path = path or SHADOW_VERDICT_LOG
    row = dict(pr=pr, lane_verdict=lane_verdict, tester_verdict=tester_verdict,
               timestamp=timestamp, final_outcome=final_outcome)
    _validate_shadow(row)
    with open(path, "a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        old = _shadow_samples(stream).get(pr)
        if old:
            if any(old[k] != row[k] for k in ("lane_verdict", "tester_verdict")):
                raise ValueError("conflicting shadow verdict")
            if final_outcome is None or final_outcome == old.get("final_outcome"):
                return False
            if old.get("final_outcome") is not None:
                raise ValueError("conflicting final outcome")
        stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return True


def shadow_verdict_stats(path=None):
    """Count unique PRs, disagreements and hits; accuracy uses resolved PRs only."""
    import fcntl
    path = path or SHADOW_VERDICT_LOG
    try:
        stream = open(path, encoding="utf-8")
    except FileNotFoundError:
        rows = []
    else:
        with stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            rows = list(_shadow_samples(stream).values())
    resolved = [r for r in rows if r.get("final_outcome") is not None]
    result = dict(samples=len(rows),
                  disagreements=sum(r["lane_verdict"] != r["tester_verdict"] for r in rows),
                  resolved=len(resolved))
    for side in ("lane", "tester"):
        hits = sum(r[side + "_verdict"] == r["final_outcome"] for r in resolved)
        result[side + "_hits"] = hits
        result[side + "_accuracy"] = hits / len(resolved) if resolved else None
    return result


def main():
    lanes = load(f"{D}/lanes.json", {}); last_warn = 0
    if "--once" in sys.argv: tick(lanes, load(f"{D}/state.json", {}), 0); return
    while True:
        try: last_warn = tick(load(f"{D}/lanes.json", lanes), load(f"{D}/state.json", {}), last_warn)
        except Exception as e: log(f"tick error: {e!r}", brain=False)
        sleep_chunk(TICK, 5)
if __name__ == "__main__": main()
