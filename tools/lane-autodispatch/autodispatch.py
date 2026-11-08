#!/usr/bin/env python3
"""Phanes lane autodispatcher (runs on the box).

Every TICK seconds: finish runs that ended (push branch + open PR, relabel issue), then give every idle
lane the next open issue labelled `lane-ready` (not `in-progress`), ordered p0 < p1 < p2 < unlabelled,
then by issue number. Optional `backend:pi|agy|codex` label pins an issue to that executor family.
State: ~/phanes-ctl/autodispatch/state.json   Log: ~/phanes-ctl/autodispatch/autodispatch.log + brain/log.md
Pause: the swrm pause store ($OPENSWARM_HOME/pauses, written by `swrm pause <scope>`) and the
legacy markers ~/phanes-ctl/autodispatch/PAUSE (whole loop), PAUSE-<LANE> (one lane),
PAUSE-HOST-<HOST>, PAUSE-BACKEND-<BACKEND> and PAUSE-MINT (no new issue minted). Both are read (Refs #1366).
"""
import base64, hashlib, json, math, os, re, shlex, stat, subprocess, sys, time, tempfile, concurrent.futures as cf
HOME = os.path.expanduser("~"); D = f"{HOME}/phanes-ctl/autodispatch"
REPO = "surlymochan/phanes"; LOCAL = "/workspace/phanes-repo"; BRAIN_LOG = "/workspace/phanes/brain/log.md"
TICK = int(os.environ.get("AD_TICK", "60")); BUDGET = os.environ.get("AD_BUDGET", "45"); LOW = int(os.environ.get("AD_LOW", "10"))
RULES = ("Rules: work only inside this repository worktree (the current directory). Never create, modify, move or delete "
         "files outside it — not the main repo, not $HOME, not /tmp; for scratch files use $TMPDIR (inside the worktree, cleaned "
         "automatically) and leave cleanup to the executor. Do not push, do not open PRs, do not touch credentials, no network "
         "installs. Keep the change small and factual; cite file paths and line numbers you checked. Commit your result with a "
         "clear message referencing the issue (\"Refs #{n}\"). Write in the same language as the issue. "
         "Your commit message must also carry a short body with a `Closes #{n}` line and a `验收：<command>` line, and the "
         "commit subject (the PR title) must contain `#{n}`, so the lane PR passes the pre-submit check (Refs #1704).")
IMPL_CARD_TASK_BOOK = ("## Impl card delivery requirements\n"
                       "1. 交付说明必须对每个新增函数列出入口→调用链→数据结构来源：由谁调用、输入来自哪个真实数据结构、每个字段由谁填写。\n"
                       "2. 至少补一个 tick/集成级测试：从真实入口（tick 或等价入口）驱动到可观测结果；只测函数加 mock 不算。\n")
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

def ctl_state_dir():
    directory = os.path.expanduser(os.environ.get("OPENSWARM_CTL_STATE_DIR", f"{HOME}/phanes-ctl/state"))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory

def pause_store_dir():
    """The swrm pause store ($OPENSWARM_HOME/pauses), the new home of the pause switches."""
    home = os.environ.get("OPENSWARM_HOME") or os.environ.get("XSWARM_HOME") or f"{HOME}/.claude/swrm"
    return os.path.join(os.path.expanduser(home), "pauses")

def marker_scope(name):
    """The pause scope of a legacy PAUSE marker file name, or None when it is none."""
    if name == "PAUSE": return "all"
    if name == "PAUSE-MINT": return "mint"
    for prefix, kind in (("PAUSE-HOST-", "host"), ("PAUSE-BACKEND-", "backend")):
        if name.startswith(prefix) and len(name) > len(prefix): return f"{kind}:{name[len(prefix):]}"
    if name.startswith("PAUSE-") and len(name) > len("PAUSE-"): return f"lane:{name[len('PAUSE-'):]}"
    return None

def pause_records(now=None):
    """The active pause records: the swrm pause store plus the legacy PAUSE markers.

    A store record carries a TTL (`expires_at`); an expired record is no pause. A legacy marker
    carries no clock, so it never expires. The result is sorted by scope."""
    now = time.time() if now is None else now
    records = []
    store = pause_store_dir()
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
        scope = marker_scope(name)
        if scope is None: continue
        records.append({"scope": scope, "reason": f"legacy marker: {name}", "created_at": 0.0,
                        "expires_at": None, "source": "marker"})
    return sorted(records, key=lambda row: row["scope"])

def paused_scopes(now=None):
    """The scopes that are paused right now (the store and the legacy markers)."""
    return {row["scope"] for row in pause_records(now)}

def lane_paused(lane, cfg=None, scopes=None):
    """Whether a lane is paused by an all/lane/host/backend scope."""
    scopes = paused_scopes() if scopes is None else scopes
    if "all" in scopes: return True
    if f"lane:{lane}" in scopes: return True
    cfg = cfg or {}
    if cfg.get("host") and f"host:{cfg['host']}" in scopes: return True
    if cfg.get("backend") and f"backend:{cfg['backend']}" in scopes: return True
    return False

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
    closing = closing_reference(issue, f"{report}\n{summary or ''}")
    if closing: parts.append(closing)
    evidence = acceptance_evidence(verify_rc, report, summary, tests)
    if evidence: parts.append(f"验收：{evidence}")
    verify_note = f", host verify rc `{verify_rc}`" if verify_rc is not None else ""
    parts.append(f"Autodispatched to lane **{lane}** on **{cfg.get('host')}** ({info.get('backend')}/{info.get('model')}), "
                 f"run `{st.get('run')}`, base `{str(st.get('base'))[:8]}`, lane status `{info.get('status')}`{verify_note}.")
    if summary: parts.append(f"Executor summary: {summary}")
    return title, "\n\n".join(parts)

def log(msg, brain=True):
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    with open(f"{D}/autodispatch.log", "a") as f: f.write(line + "\n")
    if brain:
        with open(BRAIN_LOG, "a") as f: f.write(f"- {now()} UTC+8 autodispatch: {msg}\n")
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
    return sh(["gh"] + args + ["-R", REPO], timeout=timeout)

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

def queue():
    rc, out, err = gh(["issue", "list", "--state", "open", "--label", "lane-ready", "--limit", "100", "--json", "number,title,labels,body"])
    if rc: raise RuntimeError(err)
    items = []
    for i in json.loads(out):
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
        items.append({"n": i["number"], "title": i["title"], "body": body, "labels": sorted(labels), "prio": pr, "backend": be, "strict": "strict-backend" in labels, "verify": verify})
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
def is_review_lane(lane):
    """The review lanes a verify card may be dispatched to (CX2/CX4/CX5)."""
    return bool(re.fullmatch(r"CX(?:2|4|5)(?:[-_].*)?", str(lane), re.I))
def rank(backend, item):  # own pin first, then unpinned, then cross-pinned; within each by priority, work before verify, number
    return (0 if item["backend"] == backend else 1 if item["backend"] is None else 2, item["prio"], bool(item.get("verify")), item["n"])

def queue_class_limits(lanes, state, idle):
    """Reserve review-capable capacity using available lanes and active runs only."""
    active = [L for L in lanes if L in idle or state.get(L)]
    total = len(active)
    review = sum(is_review_lane(L) for L in active)
    reserve = min(review, max(0, total - 1), max(0, VERIFY_MIN, round(total * max(0.0, VERIFY_SHARE))))
    verify_busy = sum(1 for L in active if (state.get(L) or {}).get("class") == "verify")
    work_busy = sum(1 for L in active if state.get(L) and (state[L].get("class") != "verify"))
    return max(0, reserve - verify_busy), max(0, total - reserve - work_busy)

def age_s(st):
    try: return time.time() - time.mktime(time.strptime(st["at"], "%Y-%m-%d %H:%M:%S"))
    except Exception: return 1e9

def reconcile(L, cfg, st, r, state):
    """Local state says busy but the remote lane is idle on another/no run, the run has no result.json and its runner is dead:
    the run was orphaned (lane restart / reassignment). Drop it; requeue the issue unless it already got a PR or closed."""
    if r.get("run") != "running" or r.get("lane") != "idle" or r.get("current_run") == st["run"]: return False
    # Missing status is not evidence that the runner exited. Keep the lane claimed
    # until the remote probe positively reports a dead runner (Refs #1673).
    if r.get("runner_alive") is not False or age_s(st) < 180: return False
    n = st["issue"]
    rc, out, _ = gh(["issue", "view", str(n), "--json", "state,labels"])
    if rc: return False
    try: j = json.loads(out); labels = {l["name"] for l in j["labels"]}
    except Exception: return False
    if j["state"] == "OPEN" and "lane-pr" not in labels:
        rc, _, _ = gh(["issue", "edit", str(n), "--remove-label", "in-progress", "--add-label", "lane-ready"])
        if rc: return False
        what = "requeued (lane-ready)"
    else: what = f"already {j['state'].lower()}/{'lane-pr' if 'lane-pr' in labels else '-'}; dropped"
    log(f"{L} #{n} run {st['run']} orphaned (remote lane idle, current_run={r.get('current_run')}, run_state={r.get('run_state')}, runner dead) → {what}")
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

def status_snapshot(lanes, state, res, ready, blocked):
    """Build the observable lane status row, including this process's stale skips."""
    status = {L: (("stale #%s (review required)" if "stale_since" in state[L] else "busy #%s") % state[L]["issue"]) if state.get(L) else (res[L].get("error") or res[L].get("lane") or "?") for L in lanes}
    status["_busy"] = sum(1 for L in lanes if state.get(L) and "stale_since" not in state[L])
    status["_stale"] = sum(1 for L in lanes if state.get(L) and "stale_since" in state[L])
    status["_ready"] = ready
    status["_blocked"] = blocked
    status["_stale_stripped_n"] = STALE_STRIPPED_N
    status["_stale_search_skips"] = STALE_STRIPPED_N
    status["_stale_lane_failed_skips"] = STALE_LANE_FAILED_N
    idle_s, idle_reason, idle_open_s = idle_metrics(state)
    status["_idle_s"] = idle_s              # the last idle gap of every lane (run end -> dispatch)
    status["_idle_reason"] = idle_reason    # why that gap was long
    status["_idle_open_s"] = idle_open_s    # the gap the idle lanes are in right now
    status["_idle_worst_s"] = max(idle_open_s.values(), default=0.0)
    status["_at"] = time.strftime("%F %T")
    return status

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
    host_fail(cfg["host"], f"dispatch #{item['n']}: {error}")

LANE_BUSY_RE = re.compile(r"lane \S+ is busy", re.I)  # the host still runs the finished run
CAPACITY_EXHAUSTED_RE = re.compile(r"\b(?:capacity exhausted|no (?:available )?capacity)\b", re.I)

def dispatch(lane, cfg, item, base):
    n = item["n"]
    body = item["body"]
    labels = set(item.get("labels", ()))
    # PLANNER's ordinary work cards have Chinese titles and only priority/backend
    # labels, so impl is the default. Explicit non-impl markers take precedence,
    # including when an old impl label or title prefix remains on the card.
    excluded_labels = {prefix + kind for kind in NON_IMPL_CARD_TYPES for prefix in ("", "card:", "type:")}
    excluded_labels.add("upstream(swrm)")
    excluded_title = re.match(r"(?i)^(?:upstream|ops|follow-up|planner|verify-intent)(?:\s*[:：]|\s+|$)", item["title"])
    is_impl = not (labels & excluded_labels or excluded_title)
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
        frc, _, ferr = sh(ssh_argv(ssh_opts(h), h, base_fetch_command(R, BASE_REPO_URL, BASE_BRANCH)), timeout=180)
        if frc:
            # A failing fetch aborts the dispatch right away: the base could not be validated, and
            # a later successful check (e.g. a concurrent tick that fetched the object in the
            # meantime) must never mask the fetch failure. The caller backs the host off as usual.
            # The fetch failure ends the message: the host backoff log keeps the last 80 chars.
            raise RuntimeError(f"base check: {(err or out).strip()[-120:]} | "
                               f"base fetch failed rc={frc}: {ferr.strip()[-40:]}")
        time.sleep(DISPATCH_BASE_FETCH_DELAY)
    raise RuntimeError(f"assign rc={rc}: {(err or out).strip()[-300:]}")

def git_fetch_command(ssh_options, local_repo, remote, branch, lane):
    """Build the harvest fetch argv without splitting SSH option values twice."""
    ssh_command = git_ssh_command(ssh_options)
    return ["git", "-c", f"core.sshCommand={ssh_command}", "-C", local_repo,
            "fetch", "-q", remote, f"+{branch}:refs/fleet/{lane}"]

def classify_harvest_fetch_error(error):
    """Classify a harvest fetch failure: `transient` (retry after a backoff) or `fatal` (a retry cannot help)."""
    return "transient" if HARVEST_FETCH_TRANSIENT_RE.search(error or "") else "fatal"

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

def harvest_fetch(lane, n, fetch, st):
    """Fetch a finished lane branch with bounded attempts.

    Returns (ok, error, failures); `failures` counts the consecutive failed harvest
    cycles of this lane and is reset by the first successful fetch.
    """
    err = ""
    for attempt in range(1, HARVEST_FETCH_MAX_ATTEMPTS + 1):
        rc, _, err = sh(fetch, timeout=180)
        if not rc:
            st.pop("fetch_failures", None); st.pop("fetch_backoff_until", None)
            return True, "", 0
        if not should_retry_harvest_fetch(err, attempt):
            log(f"{lane} #{n} fetch failed after {attempt}/{HARVEST_FETCH_MAX_ATTEMPTS} attempts ({classify_harvest_fetch_error(err)}: {err.strip()[-120:]})", brain=False)
            break
        log(f"{lane} #{n} fetch failed attempt {attempt}/{HARVEST_FETCH_MAX_ATTEMPTS} (transient: {err.strip()[-90:]}); retrying in {HARVEST_FETCH_RETRY_DELAY}s", brain=False)
        time.sleep(HARVEST_FETCH_RETRY_DELAY)
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

def block_pr_validation(lane, cfg, st, info, state, title, body, missing):
    """Block a lane PR whose required fields are missing and hand the issue back for a quick fix.

    The missing items are returned to the runner as an explicit error and listed one by
    one on the issue; the run is recorded as a quick-fix class, so the lane-failed
    statistics stay clean (Refs #1704)."""
    n, run = st["issue"], st["run"]
    record = {"run": run, "issue": n, "lane": lane, "class": PR_VALIDATION_FIX_CLASS,
              "error_class": PR_VALIDATION_CLASS, "missing": list(missing), "title": title,
              "at": time.strftime("%F %T")}
    record_pr_validation(cfg, run, record)
    state["_pr_validation"] = (state.get("_pr_validation", []) + [record])[-200:]
    gh(["issue", "edit", str(n), "--remove-label", "in-progress", "--remove-label", "lane-failed", "--add-label", "lane-ready"])
    gh(["issue", "comment", str(n), "--body", f"Lane {lane} (run `{run}`) 的 PR 未通过提交前校验，未创建 PR：\n\n"
        + "".join(f"- {item}\n" for item in missing)
        + f"\n按可快速修复类（`{PR_VALIDATION_CLASS}`）处理，不计入 lane-failed 统计；已重新标记 `lane-ready`。"])
    mark_done(state, n)
    return {"done": True, "status": "pr-validation-failed",
            "error": f"PR 提交前校验未通过（{PR_VALIDATION_STRICT}）：{'；'.join(missing)}",
            "missing": list(missing)}

# The artifacts that never enter a harvested branch (Refs #1835): the hidden build
# leftovers (swrm/tests/.dde4*-00000000.so) and the review products
# (.lane-review/PR-N.md).  The rules are a list constant: one more artifact kind is
# one more pattern. This legacy AD owns its filtering rules.
HARVEST_EXCLUDE_PATTERNS = [re.compile(r"(^|/)\.[^/]*\.so$"), re.compile(r"(^|/)\.lane-review/")]

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

def finish(lane, cfg, st, info, state):
    """Harvest a finished lane run: push the branch, open the PR, relabel the issue.

    Returns a result for the runner: {"done": bool, "status": str, "error": str|None,
    "missing": [...]}. `done` means the lane slot is free again; `error` carries the
    explicit reason back to the caller (Refs #1704)."""
    n, run = st["issue"], st["run"]
    if info.get("branch") and info["branch"] != "None" and info.get("commit"):
        b = f"fleet/{lane}-issue{n}-{run}"
        fetch = git_fetch_command(ssh_opts(cfg["host"]), LOCAL, f"{cfg['host']}:{info['repo']}", info["branch"], lane)
        ok, err, failures = harvest_fetch(lane, n, fetch, st)
        if not ok: return handle_harvest_fetch_failure(lane, cfg, st, info, state, err, failures)
        try:
            head, removed, empty = filter_harvest(f"refs/fleet/{lane}", st.get("base"))
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            # fail closed: an undecided filter never pushes anything
            log(f"{lane} #{n} run {run} harvest filter failed: {error}")
            return {"done": False, "status": "filter-failed", "error": str(error), "missing": []}
        if removed:
            log(f"{lane} #{n} run {run} harvest excluded: {json.dumps(removed, ensure_ascii=True)}")
        if empty:
            label_lane_failed(n, f"Lane {lane} run `{run}` has no effective change after the artifact filter "
                               f"({len(removed)} excluded path(s)).\n\nLabel `lane-failed`; re-add `lane-ready` to retry.")
            mark_done(state, n)
            return {"done": True, "status": "lane-failed", "error": "empty after artifact filtering", "missing": []}
        rc, _, err = sh(["git", "-C", LOCAL, "push", "-q", "origin", f"{head}:refs/heads/{b}"], timeout=180)
        if rc: return {"done": False, "status": "push-failed", "error": f"push failed: {err.strip()[-200:]}", "missing": []}
        _, lj, _ = gh(["issue", "view", str(n), "--json", "labels"])
        if "lane-pr" in lj:
            gh(["issue", "comment", str(n), "--body", f"Lane {lane} also finished this issue (run `{run}`, duplicate dispatch); branch `{b}` pushed for comparison, no second PR opened."])
            log(f"{lane} finished #{n} (run {run}) duplicate → branch {b}, no PR"); mark_done(state, n)
            return {"done": True, "status": "duplicate", "error": None, "missing": []}
        _, subj, _ = sh(["git", "-C", LOCAL, "log", "-1", "--format=%s", head])
        _, msg, _ = sh(["git", "-C", LOCAL, "log", "-1", "--format=%B", head])
        title, body = lane_pr_content(n, lane, cfg, st, info, subj, msg, info.get("summary"), info.get("tests"), info.get("verify_rc"))
        missing = validate_lane_pr(n, title, body)
        mode = pr_validation_mode()
        if missing and mode == PR_VALIDATION_STRICT:
            return block_pr_validation(lane, cfg, st, info, state, title, body, missing)
        if missing:
            log(f"{lane} #{n} run {run} PR validation ({mode}): 缺 {'；'.join(missing)}", brain=False)
        rc, out, err = gh(["pr", "create", "--base", "main", "--head", b, "--title", title, "--body", body])
        url = (out.strip().splitlines() or [""])[-1] if not rc else f"(pr create failed: {err.strip()[-160:]})"
        gh(["issue", "edit", str(n), "--remove-label", "in-progress", "--remove-label", "lane-ready", "--add-label", "lane-pr"])
        log(f"{lane} finished #{n} (run {run}, {info.get('status')}) → {url}"); mark_done(state, n)
        return {"done": True, "status": "pr-created", "error": None, "missing": []}
    else:
        label_lane_failed(n, f"Lane {lane} ({cfg['host']}, run `{run}`) ended `{info.get('status')}` without a commit "
            f"(error_class `{info.get('error_class')}`).\n\nExecutor summary: {info.get('summary')}\n\nLabel `lane-failed`; re-add `lane-ready` to retry.")
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

def write_heartbeat():
    """Mark the dispatcher as alive (the heartbeat is the stamp of its last life)."""
    try:
        with open(heartbeat_path(), "w") as stream: stream.write(time.strftime("%F %T") + "\n")
    except OSError: pass

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
    audit_switches()  # the switch audit runs every tick, also when the loop itself is paused (Refs #1827)
    scopes = paused_scopes()  # the swrm pause store and the legacy PAUSE markers (Refs #1366)
    if "all" in scopes: return last_warn
    label_cache_reset()  # authoritative label reads are cached for one tick only
    state.setdefault("_done", [])
    idle_books(state)  # the idle bookkeeping of the state exists from the first tick on
    now = time.time()
    # the heartbeat is the stamp of the dispatcher's last life: the gap to now is its
    # downtime, and it is read before the dispatcher marks itself alive again (Refs #1688)
    last_alive = dispatcher_last_alive()
    write_heartbeat()
    live = [L for L in lanes if host_ok(lanes[L]["host"])]
    with cf.ThreadPoolExecutor(4) as ex:
        res = dict(zip(live, ex.map(lambda L: remote(L, lanes[L], (state.get(L) or {}).get("run", "")), live)))
    for L in lanes:
        if L not in res: res[L] = {"error": "host in backoff", "skip": True}
    seen = set()
    for L in live:
        h = lanes[L]["host"]
        if h in seen: continue
        errs = [res[x] for x in live if lanes[x]["host"] == h and "error" in res[x]]
        if errs: host_fail(h, errs[0]["error"]); seen.add(h)
        else: host_good(h); seen.add(h)
    q = None
    queue_failed = False
    queue_error = ""
    base = None
    outcomes = {}
    freed_lanes = set()

    def tick_queue():
        """Load the shared queue on the first dispatch attempt, after that lane's harvest.

        A queue outage must not prevent the remaining lanes from being harvested;
        cache that failure too, so each tick still makes only one queue request."""
        nonlocal q, queue_failed, queue_error
        if q is not None: return True
        if queue_failed: return False
        try: q = queue()
        except Exception as e:
            queue_failed = True; queue_error = str(e)
            log(f"queue error: {e}", brain=False)
            return False
        for i in q: i["deps_ok"] = deps_met(i)
        # one authoritative read per issue per tick, shared by all dispatches and the count
        prefetch_labels([i["n"] for i in q])
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
        pick = None
        candidates = [i for i in q if i.get("deps_ok", True) and can_take(cfg["backend"], i, L)]
        candidates.sort(key=lambda i: rank(cfg["backend"], i))
        if AD_VERIFY_SHARE_ENABLED:
            # A harvested lane is available in this tick even if the remote
            # snapshot still calls it busy. Keep the same pause/host/cap checks.
            idle = [name for name, c in lanes.items() if not state.get(name) and "error" not in res[name]
                    and (name in freed_lanes or res[name].get("lane") == "idle")
                    and host_ok(c["host"]) and not lane_paused(name, c, scopes)]
            verify_slots, work_slots = queue_class_limits(lanes, state, idle)
            # The work class may hold at most `total - reserve` lanes: with no work slot left it takes
            # no further lane, the work cards in flight keep the lanes they hold (Refs #1864). The cap
            # is a limit, not a preference: a waiting work card is not dispatched on any lane.
            if not work_slots:
                waiting = [i for i in candidates if not i.get("verify")]
                if waiting:
                    log(f"queue share: {L} work class at its cap (work slots=0, verify deficit={verify_slots}); "
                        f"{len(waiting)} work card(s) wait", brain=False)
                candidates = [i for i in candidates if i.get("verify")]
            # Admission precedes existing pin/priority/work/number ordering. The verify class takes
            # the reserved lane first; the work class never crosses its cap.
            preferred = True if verify_slots and is_review_lane(L) else None
            if preferred and any(bool(i.get("verify")) == preferred for i in candidates):
                candidates.sort(key=lambda i: bool(i.get("verify")) != preferred)
                log(f"queue share: {L} prefers verify (verify deficit={verify_slots}, work slots={work_slots}); other class yields", brain=False)
        for c in candidates:
            status, detail = label_state(c["n"])
            if status == "fresh": pick = c; break
            if status == "stale":
                q.remove(c); skip_stale(c["n"], detail)  # try the next one this tick
                continue
            # the authoritative read failed: keep the row (it may still be dispatchable),
            # never count it as a stale skip and never dispatch on unverified labels
            log_unverified(c["n"], status, detail, "kept in queue (not dispatched this tick)")
        if not pick: return "no-card"
        commit = base_commit()
        gh(["issue", "edit", str(pick["n"]), "--add-label", "in-progress"])
        try: run, ver = dispatch(L, cfg, pick, commit)
        except Exception as e:
            dispatch_failed(L, cfg, pick, e, busy=bool(LANE_BUSY_RE.search(str(e))))
            return "no-capacity"
        q.remove(pick); state[L] = {"issue": pick["n"], "run": run, "host": cfg["host"], "base": commit, "at": time.strftime("%F %T"), "verify": ver, "class": "verify" if pick.get("verify") else "work"}
        # Stop this lane's gap at its own successful dispatch. Later lanes may take
        # minutes to assign; their work cannot extend an already occupied lane's gap.
        closed = idle_close(state, L, time.time(), last_alive, tick_started_at=now)
        if closed:
            idle_s, reason = closed
            if idle_s > IDLE_LOG_MIN_S:
                log(f"{L} idle {idle_s:.0f}s before dispatch (reason: {reason})", brain=True)
        if AD_VERIFY_SHARE_ENABLED: log(f"queue share: dispatched {state[L]['class']} #{pick['n']} on {L}", brain=False)
        save_state(state); log(f"dispatched #{pick['n']} → {L} ({cfg['host']}) run {run} base {commit[:8]}")
        return "dispatched"

    # 1) Re-dispatch each freed lane before harvesting the next one (Refs #1688).
    #    Another lane's slow fetch/push/PR creation must not hold up this lane.
    for L, r in res.items():
        st = state.get(L)
        if "error" in r: continue
        freed = False
        if st and isinstance(r.get("run"), dict):
            if lane_in_fetch_backoff(st): continue  # harvest fetch is backing off after a transient failure (Refs #947)
            # the run ended: the lane is idle from its own end time on (Refs #1688)
            idle_open(state, L, r["run"].get("ended_at"), now, last_alive)
            result = finish(L, lanes[L], st, r["run"], state)
            if result.get("done"):
                state.pop(L); save_state(state); freed = True
            if result.get("error"): log(f"{L} #{st['issue']} run {st['run']}: {result['error']}", brain=False)
        elif st and reconcile(L, lanes[L], st, r, state):
            state.pop(L); save_state(state); freed = True
        if freed:
            freed_lanes.add(L)
            outcomes[L] = give_card(L, lanes[L], remote_idle=False)
    update_busy_stale(lanes, state, res)
    if not tick_queue():
        st = status_snapshot(lanes, state, res, None, None)
        st["_queue_error"] = queue_error or "queue unavailable"
        with open(f"{D}/lanes-status.json", "w") as stream:
            json.dump(st, stream, ensure_ascii=False, indent=1)
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
    blocked = sum(not i["deps_ok"] for i in q)
    ready = len(q) - blocked
    # refill when the queue is short, or when some backend's lanes could not all be fed (counting cross-pin fallback)
    short = {b: sum(c["backend"] == b for c in lanes.values()) - sum(1 for i in q if i.get("deps_ok", True) and can_take(b, i, verify_gate=False))
             for b in {c["backend"] for c in lanes.values()}}
    starve = max(short.values(), default=0)
    if ready < LOW or starve > 0:
        why = f"queue low ({ready} < {LOW})" if ready < LOW else f"backend starved ({', '.join(f'{b} short {k}' for b, k in short.items() if k > 0)})"
        if mint_paused(scopes):
            if time.time() - last_warn > 600:
                log(f"{why}: mint paused, no new issue minted", brain=False); last_warn = time.time()
        else:
            filed = replenish(max(LOW - ready, starve), state)
            if filed: log(f"{why}: filed {len(filed)} issue(s) from backlog: {' '.join(filed)}")
            elif time.time() - last_warn > 600:
                log(f"WARNING queue low: {ready} dispatchable lane-ready issue(s) left (+{blocked} waiting on 依赖), backlog/ empty (threshold {LOW})"); last_warn = time.time()
    state["_tick_at"] = now
    save_state(state)
    st = status_snapshot(lanes, state, res, ready, blocked)
    with open(f"{D}/lanes-status.json", "w") as stream:
        json.dump(st, stream, ensure_ascii=False, indent=1)
    return last_warn

def file_issue(text, extra=()):
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip(); labels = [l.strip() for l in lines[1].split(":", 1)[1].split(",") if l.strip()]
    body = "\n".join(lines[2:]).strip() + "\n"
    args = ["issue", "create", "-t", title, "-b", body]
    for l in ["lane-ready", *labels, *extra]: args += ["-l", l]
    rc, o, e = gh(args)
    if rc: raise RuntimeError(e.strip()[-200:])
    return "#" + o.strip().rsplit("/", 1)[-1]

PLANNER = """# 规划：为 lane 队列写 {k} 条新任务（{tag}）
labels: p1,backend:codex,planner
自动补队列任务。读 main 上的代码与 `docs/reviews/`、`docs/scenario/`、`examples/org-health-local/`，写 {k} 个新文件 `docs/backlog/next/{tag}-01.md` … `{tag}-{k:02d}.md`，每个文件是一条可独立派给 lane 的小任务，格式严格为：

```
# <中文标题>
labels: <p1 或 p2>,backend:<pi|codex|agy>
<任务正文：要改的文件路径、具体改动、边界；不得依赖其它未完成任务>

验收：`<一条在仓库根或 cd 子目录后可直接运行、只用 python3 标准库/现有脚本的命令>`
```

要求：{k} 条里 backend:pi / backend:agy / backend:codex / 无 backend 标签 大约 2:1:2:1（至少 3 条无 backend 标签供饥饿兜底）；优先 #6 三 Bot 组织诊断 e2e（examples/org-health-local）的测试覆盖、文档、健壮性小修，其次 swrm 的纯单元测试（不需要 Unix socket 的可标 codex，需要的标 pi）；每条 15 分钟内可完成；不要写安全加固、不要改 CI 的 macOS 矩阵、不要碰签名；不要与下列已有 issue 重复：

{open}

只新增 `docs/backlog/next/` 下的文件，不改其它文件。

验收：`ls docs/backlog/next/{tag}-*.md | wc -l | grep -qx {k}`
"""

def replenish(k, state):
    """Queue refill, in order: (1) ~/phanes-ctl/autodispatch/backlog/*.md (`# title` / `labels: a,b` / body; moved to backlog/filed/);
    (2) docs/backlog/next/*.md on origin/main not yet imported (written by planner runs); (3) if still short and no planner
    issue is open, file one planner issue asking a lane to write the next batch into docs/backlog/next/."""
    bd = f"{D}/backlog"; os.makedirs(f"{bd}/filed", exist_ok=True); out = []
    for f in sorted(x for x in os.listdir(bd) if x.endswith(".md")):
        if len(out) >= k: return out
        try: out.append(file_issue(open(f"{bd}/{f}").read()))
        except Exception as e: log(f"backlog {f}: {e}", brain=False); return out
        os.replace(f"{bd}/{f}", f"{bd}/filed/{f}")
    G = ["git", "-C", "/workspace/phanes-repo"]
    sh(G + ["fetch", "-q", "origin", "main"], timeout=120)
    rc, ls, _ = sh(G + ["ls-tree", "--name-only", "origin/main", "docs/backlog/next/"])
    imp = state.setdefault("_imported", [])
    for path in sorted(x for x in ls.split() if x.endswith(".md")) if not rc else []:
        if len(out) >= k: break
        if path in imp: continue
        rc, text, _ = sh(G + ["show", f"origin/main:{path}"])
        imp.append(path); save_state(state)
        try:
            if rc or not text.startswith("#") or not text.splitlines()[1].startswith("labels:"): raise RuntimeError("bad format")
            out.append(file_issue(text))
        except Exception as e: log(f"repo backlog {path}: {e}", brain=False)
    if len(out) < k:
        rc, o, _ = gh(["issue", "list", "--state", "open", "--label", "planner", "--json", "number"])
        if not rc and json.loads(o) == []:
            rc, o, _ = gh(["issue", "list", "--state", "open", "--limit", "200", "--json", "number,title"])
            titles = "\n".join(f"- #{i['number']} {i['title']}" for i in json.loads(o)) if not rc else ""
            tag = time.strftime("%Y%m%d-%H%M")
            try: out.append(file_issue(PLANNER.format(k=20, tag=tag, open=titles)) + "(planner)")
            except Exception as e: log(f"planner issue: {e}", brain=False)
    return out

def sleep_chunk(total=TICK, chunk=5):
    """Sleep between ticks in chunks <= 5s, refreshing heartbeat on each step to prevent suspend freeze (Refs #906)."""
    rem = max(0, total)
    if rem == 0:
        open(f"{D}/heartbeat", "w").write(time.strftime("%F %T") + "\n")
        return
    while rem > 0:
        open(f"{D}/heartbeat", "w").write(time.strftime("%F %T") + "\n")
        step = min(rem, chunk)
        time.sleep(step)
        rem -= step
    open(f"{D}/heartbeat", "w").write(time.strftime("%F %T") + "\n")

def main():
    lanes = load(f"{D}/lanes.json", {}); last_warn = 0
    if "--once" in sys.argv: tick(lanes, load(f"{D}/state.json", {}), 0); return
    while True:
        try: last_warn = tick(load(f"{D}/lanes.json", lanes), load(f"{D}/state.json", {}), last_warn)
        except Exception as e: log(f"tick error: {e!r}", brain=False)
        sleep_chunk(TICK, 5)
if __name__ == "__main__": main()
