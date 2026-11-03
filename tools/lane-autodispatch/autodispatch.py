#!/usr/bin/env python3
"""Phanes lane autodispatcher (runs on the box).

Every TICK seconds: finish runs that ended (push branch + open PR, relabel issue), then give every idle
lane the next open issue labelled `lane-ready` (not `in-progress`), ordered p0 < p1 < p2 < unlabelled,
then by issue number. Optional `backend:pi|agy|codex` label pins an issue to that executor family.
State: ~/phanes-ctl/autodispatch/state.json   Log: ~/phanes-ctl/autodispatch/autodispatch.log + brain/log.md
Pause: the openswarm pause store ($OPENSWARM_HOME/pauses, written by `openswarm pause <scope>`) and the
legacy markers ~/phanes-ctl/autodispatch/PAUSE (whole loop), PAUSE-<LANE> (one lane),
PAUSE-HOST-<HOST>, PAUSE-BACKEND-<BACKEND> and PAUSE-MINT (no new issue minted). Both are read (Refs #1366).
"""
import base64, json, os, re, shlex, subprocess, sys, time, concurrent.futures as cf
from datetime import datetime, timezone
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
# Refs #1668: a run that died because a backend budget window was spent (the MiniMax 5h Token
# Plan window, for example) is not a card failure: the card never got a fair try.  Such a card
# is held (it keeps `in-progress`) until the window recovers and then returns to the queue.
BUDGET_CLASSES = {"budget_exceeded", "budget_exceeded_daily", "budget_exceeded_weekly",
                  "budget_exceeded_monthly", "budget_exhausted"}
BUDGET_REQUEUE_DELAY = int(os.environ.get("AD_BUDGET_REQUEUE_DELAY", "1800"))  # seconds, no reset time known
BUDGET_REQUEUE_MAX = int(os.environ.get("AD_BUDGET_REQUEUE_MAX", "2"))  # holds per card before it fails for real
BUDGET_REQUEUE_MAX_HOLD = int(os.environ.get("AD_BUDGET_REQUEUE_MAX_HOLD", str(6 * 3600)))  # seconds, unknown window state
# The provider error may say when the window recovers ("resets in 2h13m", "Retry-After: 120").
BUDGET_RESETS_IN_RE = re.compile(r"\bresets in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\b", re.I)
BUDGET_RESETS_IN_PLAIN_RE = re.compile(r"\bresets in\s+(\d+)\b", re.I)
BUDGET_RETRY_AFTER_RE = re.compile(r"\bretry[-_ ]?after\"?\s*[:=]\s*\"?(\d+)", re.I)
# The window state: open (the card may be requeued), exhausted (still closed) or unknown.
BUDGET_WINDOW_OPEN, BUDGET_WINDOW_EXHAUSTED, BUDGET_WINDOW_UNKNOWN = "open", "exhausted", "unknown"
STALE_STRIPPED_N = 0  # stale search-index entries removed by this process
STALE_LANE_FAILED_N = 0  # stale hits whose issue still carries both lane-failed and lane-ready (Refs #985)
LABEL_CACHE = {}  # issue number -> (status, payload): authoritative label reads of the current tick
LABEL_READS_N = 0  # authoritative `gh issue view` reads issued during the current tick
READ_ERROR_LOGGED = set()  # issues whose unreadable state was already logged in this tick
# GitHub charges one GraphQL point per `gh issue view`: cap the authoritative reads of one
# tick so a 100-row queue and a 60s tick cannot blow through the hourly quota (Refs #950).
FRESH_READ_BUDGET = int(os.environ.get("AD_FRESH_BUDGET", "60"))
# Reserved seam for the P2-3 verify-card queue share policy; the quota/份额 behaviour is not implemented here.
AD_VERIFY_SHARE_ENABLED = os.environ.get("AD_VERIFY_SHARE_ENABLED", "0").lower() in {"1", "true", "yes"}
# Verify cards rejected for missing fields, per issue: the field set that was logged. An invalid card
# must not repeat the same log line every tick, but a changed field set is logged again.
VERIFY_REJECT_LOGGED = {}

def ctl_state_dir():
    directory = os.path.expanduser(os.environ.get("OPENSWARM_CTL_STATE_DIR", f"{HOME}/phanes-ctl/state"))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory

def pause_store_dir():
    """The openswarm pause store ($OPENSWARM_HOME/pauses), the new home of the pause switches."""
    home = os.environ.get("OPENSWARM_HOME") or os.environ.get("XSWARM_HOME") or f"{HOME}/.claude/openswarm"
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
    """The active pause records: the openswarm pause store plus the legacy PAUSE markers.

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
    tmp = f"{D}/state.json.tmp"; json.dump(s, open(tmp, "w"), indent=1, ensure_ascii=False); os.replace(tmp, f"{D}/state.json")
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

REMOTE_STATUS = r'''
import json,os,sys
O=os.path.expanduser("~/.claude/openswarm-executor-candidate"); L=sys.argv[1]; R=sys.argv[2] if len(sys.argv)>2 else ""
out={"lane":None,"run":None}
try:
  ls=json.load(open(f"{O}/lanes/{L}/state.json")); out["lane"]=ls.get("state"); out["current_run"]=ls.get("current_run")
except Exception as e: out["lane"]="unknown"
if R:
  p=f"{O}/runs/{R}/result.json"
  if os.path.exists(p):
    with open(p) as stream: r=json.load(stream)
    res=r.get("result") or {}
    out["run"]={"status":r.get("status"),"branch":r.get("branch"),"commit":r.get("commit"),"repo":(r.get("workspace") or {}).get("repo"),
      "backend":r.get("backend"),"model":r.get("model"),"error_class":r.get("error_class"),"resets_at":r.get("resets_at"),"summary":(str(res.get("summary") or "") if isinstance(res,dict) else str(res or ""))[:1200],
      "tests":(str(res.get("tests") or "") if isinstance(res,dict) else "")[:400],
      "verify_rc":(r.get("verify") or {}).get("rc") if isinstance(r.get("verify"),dict) else None}
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
        items.append({"n": i["number"], "title": i["title"], "body": body, "prio": pr, "backend": be, "strict": "strict-backend" in labels, "verify": verify})
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

def age_s(st):
    try: return time.time() - time.mktime(time.strptime(st["at"], "%Y-%m-%d %H:%M:%S"))
    except Exception: return 1e9

def reconcile(L, cfg, st, r, state):
    """Local state says busy but the remote lane is idle on another/no run, the run has no result.json and its runner is dead:
    the run was orphaned (lane restart / reassignment). Drop it; requeue the issue unless it already got a PR or closed."""
    if r.get("run") != "running" or r.get("lane") != "idle" or r.get("current_run") == st["run"]: return False
    if r.get("runner_alive") or (r.get("runner_alive") is None and age_s(st) < 600) or age_s(st) < 180: return False
    n = st["issue"]; _, out, _ = gh(["issue", "view", str(n), "--json", "state,labels"])
    try: j = json.loads(out); labels = {l["name"] for l in j["labels"]}
    except Exception: return False
    if j["state"] == "OPEN" and "lane-pr" not in labels:
        gh(["issue", "edit", str(n), "--remove-label", "in-progress", "--add-label", "lane-ready"])
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

def status_snapshot(lanes, state, res, ready, blocked):
    """Build the observable lane status row, including this process's stale skips."""
    status = {L: ("busy #%s" % state[L]["issue"]) if state.get(L) else (res[L].get("error") or res[L].get("lane") or "?") for L in lanes}
    status["_busy"] = sum(1 for L in lanes if state.get(L))
    status["_ready"] = ready
    status["_blocked"] = blocked
    status["_stale_stripped_n"] = STALE_STRIPPED_N
    status["_stale_search_skips"] = STALE_STRIPPED_N
    status["_stale_lane_failed_skips"] = STALE_LANE_FAILED_N
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

def dispatch_failed(lane, cfg, item, error):
    """A failed dispatch drops the in-progress label and backs the host off (transient, unchanged)."""
    gh(["issue", "edit", str(item["n"]), "--remove-label", "in-progress"])
    host_fail(cfg["host"], f"dispatch #{item['n']}: {error}")

def dispatch(lane, cfg, item, base):
    n = item["n"]; task = f"# #{n} {item['title']}\n\n{item['body']}\n\n{RULES.format(n=n)}\n"
    path = f"{D}/tasks/{lane}-{n}.md"; os.makedirs(f"{D}/tasks", exist_ok=True); open(path, "w").write(task)
    h = cfg["host"]; F = cfg.get("fleet", "~/.local/bin/openswarm-fleet"); R = cfg.get("repo", "~/work/phanes-fleet")
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
    if str(n) not in state.get("_budget_requeue", {}):
        state.get("_budget_tries", {}).pop(str(n), None)

def is_budget_failure(error_class):
    """True when the run died because a backend budget window was spent (Refs #1668).

    `budget_exceeded` and its daily/weekly/monthly variants name a spent window, not a
    broken card: the run never got to the end of the card."""
    return str(error_class or "").strip().lower().replace("-", "_") in BUDGET_CLASSES

def parse_epoch(value):
    """Epoch seconds from an epoch number or an ISO-8601 timestamp (None when unreadable)."""
    if isinstance(value, bool) or value is None: return None
    if isinstance(value, (int, float)): return float(value)
    text = str(value).strip()
    if not text: return None
    try: return float(text)
    except ValueError: pass
    try: stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError: return None
    if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()

def budget_requeue_at(now, resets_at=None, delay=BUDGET_REQUEUE_DELAY, max_hold=BUDGET_REQUEUE_MAX_HOLD):
    """Epoch second at which a budget-failed card may return to the queue (Refs #1668).

    The window reset time wins when the run knows it (`resets_at`); without it the card
    waits `delay` seconds.  The wait is capped by `max_hold` so a stale reset time cannot
    park a card forever."""
    reset = parse_epoch(resets_at)
    now = float(now)
    target = max(now, reset) if reset is not None else now + max(0, delay)
    return min(target, now + max(0, max_hold))

def budget_reset_seconds(text):
    """The seconds until the window recovers according to the run text (None when unknown).

    The provider error often says when the window recovers ("resets in 2h13m",
    "Retry-After: 120"); that is a trustworthy recovery time."""
    if not isinstance(text, str) or not text: return None
    m = BUDGET_RESETS_IN_RE.search(text)
    if m and any(m.groups()):
        h, mi, s = (int(x or 0) for x in m.groups())
        return h * 3600 + mi * 60 + s
    m = BUDGET_RESETS_IN_PLAIN_RE.search(text)
    if m: return int(m.group(1))
    m = BUDGET_RETRY_AFTER_RE.search(text)
    if m: return int(m.group(1))
    return None

def issue_state_labels(n):
    """The state and the labels of an issue as read back (None, set() when unreadable)."""
    rc, out, _ = gh(["issue", "view", str(n), "--json", "state,labels"])
    if rc: return None, set()
    try: issue = json.loads(out)
    except Exception: return None, set()
    return issue.get("state"), label_names(issue)

def budget_window_probe():
    """The window probe answer ({exhausted: bool, resets_at?: ...}); {} when there is none.

    `AD_BUDGET_PROBE_CMD` is a shell command that answers JSON.  Only an explicit
    `{"exhausted": false}` confirms that the window is open again."""
    cmd = os.environ.get("AD_BUDGET_PROBE_CMD")
    if not cmd: return {}
    try:
        rc, out, _ = sh(cmd, timeout=20)
        if rc: return {}
        data = json.loads(out)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError, AttributeError, subprocess.TimeoutExpired, OSError):
        return {}

def budget_window_state(probe, reset_passed=False):
    """The window state the harvester may act on: open, exhausted or unknown (Refs #1668).

    The probe knows the window that is closed right now, so its answer wins.  Without a
    probe answer a recovery time that has passed means the window is back; anything else
    is unknown and the card stays held."""
    if isinstance(probe, dict):
        exhausted = probe.get("exhausted")
        if exhausted is True: return BUDGET_WINDOW_EXHAUSTED
        if exhausted is False: return BUDGET_WINDOW_OPEN
    return BUDGET_WINDOW_OPEN if reset_passed else BUDGET_WINDOW_UNKNOWN

def budget_window_reset(info):
    """The epoch second at which the backend window recovers (None when unknown).

    The run result knows the reset time of the provider error it hit, the run text may
    say when the window recovers ("resets in 2h13m") and the probe knows it for the
    window that is currently closed.  The real executor writes no top level `resets_at`,
    so the text and the probe are what a 5h window failure can rely on."""
    reset = parse_epoch((info or {}).get("resets_at"))
    if reset is not None: return reset
    seconds = budget_reset_seconds(" ".join(str((info or {}).get(k) or "") for k in ("error", "summary")))
    if seconds: return time.time() + seconds
    reset = parse_epoch(budget_window_probe().get("resets_at"))
    if reset is not None: return reset
    return None

def hold_budget_card(lane, cfg, st, info, state):
    """Hold a budget-failed card until the backend window recovers (Refs #1668).

    The card keeps its `in-progress` label, so no lane picks it up while the window is
    closed; the next tick returns it to `lane-ready` once the window is back."""
    n, run = st["issue"], st["run"]
    now = time.time()
    reset = budget_window_reset(info)
    at = budget_requeue_at(now, reset)
    pending = state.setdefault("_budget_requeue", {})
    row = dict(pending.get(str(n)) or {})
    counts = state.setdefault("_budget_tries", {})
    row.update({"issue": n, "lane": lane, "run": run, "backend": info.get("backend"),
                "error_class": info.get("error_class"), "tries": int(counts.get(str(n), row.get("tries") or 0)) + 1,
                "requeue_at": at, "reset_at": reset, "since": row.get("since") or now})
    pending[str(n)] = row
    counts[str(n)] = row["tries"]
    save_state(state)
    when = time.strftime("%F %T", time.localtime(at))
    gh(["issue", "comment", str(n), "--body",
        f"Lane {lane} ({cfg['host']}, run `{run}`) ended `{info.get('status')}` without a commit because a backend "
        f"budget window was spent (`{info.get('error_class')}`), not a card failure. Held until the window "
        f"recovers (at `{when}`); the card returns to `lane-ready` by itself then.\n\n"
        f"Executor summary: {info.get('summary')}"])
    log(f"{lane} #{n} run {run} budget failure ({info.get('error_class')}) → held until the window recovers "
        f"(hold {row['tries']}/{BUDGET_REQUEUE_MAX})")
    return True

def budget_hold_expired(row, now):
    """True when a held card waited longer than the hold bound without a known recovery time."""
    since = parse_epoch((row or {}).get("since"))
    if since is None: return False
    return now - since >= BUDGET_REQUEUE_MAX_HOLD

def requeue_budget_cards(state):
    """Return the held budget cards to the queue once the window recovered (Refs #1668).

    The window state decides: a probe that still reports an exhausted window (or no
    answer at all) keeps the card held, so a card is never put back into the queue while
    its window is still closed."""
    pending = state.get("_budget_requeue") or {}
    if not pending: return
    now = time.time()
    probe = budget_window_probe()  # one window probe per tick
    for key, row in list(pending.items()):
        if float(row.get("requeue_at") or 0) > now: continue
        n = row.get("issue") or key
        issue_state, labels = issue_state_labels(n)
        if issue_state is None: continue  # unreadable: try again next tick
        if issue_state != "OPEN" or "lane-pr" in labels:
            pending.pop(key, None)
            state.get("_budget_tries", {}).pop(key, None)
            continue  # closed or already a PR: the hold is void
        tries = int(row.get("tries") or 0)
        if tries >= BUDGET_REQUEUE_MAX:
            pending.pop(key, None)
            (state.get("_budget_tries") or {}).pop(key, None)
            label_lane_failed(n, f"Lane {row.get('lane')} ({row.get('backend') or 'unknown'}) hit a closed budget "
                               f"window `{row.get('error_class')}` {tries} time(s) for this card; the window never "
                               f"recovered in time.\n\nLabel `lane-failed`; re-add `lane-ready` to retry.")
            log(f"{row.get('lane')} #{n} budget hold exhausted ({tries} holds) → labelled lane-failed")
            continue
        reset = parse_epoch(row.get("reset_at"))
        reset_passed = reset is not None and now >= reset
        wstate = budget_window_state(probe, reset_passed=reset_passed)
        if wstate != BUDGET_WINDOW_OPEN:
            # the window is still closed or its state cannot be confirmed: wait another round
            if reset is None and budget_hold_expired(row, now):
                pending.pop(key, None)  # the window state stayed unknown for too long
                (state.get("_budget_tries") or {}).pop(key, None)
                label_lane_failed(n, f"Lane {row.get('lane')} ({row.get('backend') or 'unknown'}) stayed on a closed "
                                   f"budget window `{row.get('error_class')}`; the window never recovered within the "
                                   f"hold bound.\n\nLabel `lane-failed`; re-add `lane-ready` to retry.")
                log(f"{row.get('lane')} #{n} budget hold expired (window {wstate}) → labelled lane-failed")
                continue
            delay = BUDGET_REQUEUE_DELAY
            row["requeue_at"] = budget_requeue_at(now, reset if reset is not None and reset > now else None, delay=delay)
            pending[key] = row
            log(f"{row.get('lane')} #{n} budget hold: window {wstate}; retry in {delay}s", brain=False)
            continue
        rc, _, _ = gh(["issue", "edit", str(n), "--remove-label", "in-progress", "--add-label", "lane-ready"])
        if rc: continue  # the relabel failed: keep the hold and retry next tick
        gh(["issue", "comment", str(n), "--body",
            f"The backend budget window recovered; the card is requeued as `lane-ready` "
            f"(lane {row.get('lane')}, run `{row.get('run')}`)."])
        pending.pop(key, None)
        log(f"{row.get('lane')} #{n} budget window recovered → requeued lane-ready (hold {tries})")
    save_state(state)

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

RUN_RECORD_SNIPPET = r'''
import base64,json,os,sys
run, payload = sys.argv[1], sys.argv[2]
d = os.path.expanduser("~/.claude/openswarm-executor-candidate/runs") + "/" + run
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
        rc, _, err = sh(["git", "-C", LOCAL, "push", "-q", "origin", f"refs/fleet/{lane}:refs/heads/{b}"], timeout=180)
        if rc: return {"done": False, "status": "push-failed", "error": f"push failed: {err.strip()[-200:]}", "missing": []}
        _, lj, _ = gh(["issue", "view", str(n), "--json", "labels"])
        if "lane-pr" in lj:
            gh(["issue", "comment", str(n), "--body", f"Lane {lane} also finished this issue (run `{run}`, duplicate dispatch); branch `{b}` pushed for comparison, no second PR opened."])
            log(f"{lane} finished #{n} (run {run}) duplicate → branch {b}, no PR"); mark_done(state, n)
            return {"done": True, "status": "duplicate", "error": None, "missing": []}
        _, subj, _ = sh(["git", "-C", LOCAL, "log", "-1", "--format=%s", f"refs/fleet/{lane}"])
        _, msg, _ = sh(["git", "-C", LOCAL, "log", "-1", "--format=%B", f"refs/fleet/{lane}"])
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
        if is_budget_failure(info.get("error_class")):
            # A spent budget window is not a card failure: the card never ran to the end, so it is
            # held until the window recovers instead of being labelled lane-failed (Refs #1668).
            hold_budget_card(lane, cfg, st, info, state)
            mark_done(state, n); return {"done": True, "status": "budget-held", "error": None, "missing": []}
        label_lane_failed(n, f"Lane {lane} ({cfg['host']}, run `{run}`) ended `{info.get('status')}` without a commit "
            f"(error_class `{info.get('error_class')}`).\n\nExecutor summary: {info.get('summary')}\n\nLabel `lane-failed`; re-add `lane-ready` to retry.")
        mark_done(state, n); log(f"{lane} ended #{n} without a commit (run {run}, {info.get('status')}/{info.get('error_class')}) → labelled lane-failed")
        return {"done": True, "status": "lane-failed", "error": None, "missing": []}

def tick(lanes, state, last_warn):
    scopes = paused_scopes()  # the openswarm pause store and the legacy PAUSE markers (Refs #1366)
    if "all" in scopes: return last_warn
    label_cache_reset()  # authoritative label reads are cached for one tick only
    state.setdefault("_done", [])
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
    for L, r in res.items():
        st = state.get(L)
        if "error" in r: continue
        if st and isinstance(r.get("run"), dict):
            if lane_in_fetch_backoff(st): continue  # harvest fetch is backing off after a transient failure (Refs #947)
            result = finish(L, lanes[L], st, r["run"], state)
            if result.get("done"): state.pop(L); save_state(state)
            if result.get("error"): log(f"{L} #{st['issue']} run {st['run']}: {result['error']}", brain=False)
        elif st and reconcile(L, lanes[L], st, r, state): state.pop(L); save_state(state)
    requeue_budget_cards(state)  # budget-failed cards return to the queue once the window recovered (Refs #1668)
    try: q = queue()
    except Exception as e: log(f"queue error: {e}", brain=False); return last_warn
    for i in q: i["deps_ok"] = deps_met(i)
    # one authoritative read per issue per tick, shared by the dispatch candidates and the count
    prefetch_labels([i["n"] for i in q])
    base = None
    for L, cfg in lanes.items():
        r = res[L]
        if "error" in r or not host_ok(cfg["host"]) or state.get(L) or r.get("lane") != "idle" or lane_paused(L, cfg, scopes): continue
        pick = None
        for c in sorted((i for i in q if i.get("deps_ok", True) and can_take(cfg["backend"], i, L)), key=lambda i: rank(cfg["backend"], i)):
            status, detail = label_state(c["n"])
            if status == "fresh": pick = c; break
            if status == "stale":
                q.remove(c); skip_stale(c["n"], detail)  # try the next one this tick
                continue
            # the authoritative read failed: keep the row (it may still be dispatchable),
            # never count it as a stale skip and never dispatch on unverified labels
            log_unverified(c["n"], status, detail, "kept in queue (not dispatched this tick)")
        if not pick: continue
        if base is None:
            _, out, _ = sh(["git", "ls-remote", BASE_REPO_URL, f"refs/heads/{BASE_BRANCH}"]); base = out.split()[0]
        gh(["issue", "edit", str(pick["n"]), "--add-label", "in-progress"])
        try: run, ver = dispatch(L, cfg, pick, base)
        except Exception as e:
            dispatch_failed(L, cfg, pick, e); continue
        q.remove(pick); state[L] = {"issue": pick["n"], "run": run, "host": cfg["host"], "base": base, "at": time.strftime("%F %T"), "verify": ver}
        save_state(state); log(f"dispatched #{pick['n']} → {L} ({cfg['host']}) run {run} base {base[:8]}")
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
    st = status_snapshot(lanes, state, res, ready, blocked)
    json.dump(st, open(f"{D}/lanes-status.json", "w"), ensure_ascii=False, indent=1)
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

要求：{k} 条里 backend:pi / backend:agy / backend:codex / 无 backend 标签 大约 2:1:2:1（至少 3 条无 backend 标签供饥饿兜底）；优先 #6 三 Bot 组织诊断 e2e（examples/org-health-local）的测试覆盖、文档、健壮性小修，其次 openswarm 的纯单元测试（不需要 Unix socket 的可标 codex，需要的标 pi）；每条 15 分钟内可完成；不要写安全加固、不要改 CI 的 macOS 矩阵、不要碰签名；不要与下列已有 issue 重复：

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
