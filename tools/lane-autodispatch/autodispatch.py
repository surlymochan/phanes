#!/usr/bin/env python3
"""Phanes lane autodispatcher (runs on the box).

Every TICK seconds: finish runs that ended (push branch + open PR, relabel issue), then give every idle
lane the next open issue labelled `lane-ready` (not `in-progress`), ordered p0 < p1 < p2 < unlabelled,
then by issue number. Optional `backend:pi|agy|codex` label pins an issue to that executor family.
State: ~/phanes-ctl/autodispatch/state.json   Log: ~/phanes-ctl/autodispatch/autodispatch.log + brain/log.md
Pause: touch ~/phanes-ctl/autodispatch/PAUSE (whole loop) or PAUSE-<LANE> (one lane).
"""
import base64, json, os, re, shlex, subprocess, sys, time, concurrent.futures as cf
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
# Host recovery (Refs #1673): the 2026-11-02 incident left PAUSE-HOST-box behind and the run that was in
# flight (M6, #1361) was lost for 44 minutes. The tick observes the reachability of every host (it probes
# them over ssh anyway) and acts on the down -> up edge: the hold of the host is reviewed and the runs that
# were in flight are verified.
PAUSE_HOST_PREFIX = "PAUSE-HOST-"
AUTO_PREFIX = "auto:"
# Holds another owner owns: the memory holds are healed by the health self-heal path, a machine cooldown has
# its own deadline, and a disk hold is not cured by the host answering ssh again (the disk can still be full).
RESOURCE_HOLD_RE = re.compile(r"disk|host_hard|mem_hard", re.I)
# An automatic hold whose body names the unreachable host: the cause of the hold is known, and the host
# answering ssh again cures it. An automatic hold without such a cause keeps its pause: the write time of a
# hold says nothing about its cause (an independent protection can be written during the very same outage).
UNREACHABLE_HOLD_RE = re.compile(r"ssh|unreachable|streak", re.I)
# A run is only given up when its assignment is at least this old: a runner that just died may still be
# writing its result.json.
LOST_RUN_MIN_AGE_S = 180
RECOVERY_LOG = "host-recoveries.jsonl"
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
# Reserved seam for the P2-3 verify-card queue share policy; the quota/份额 behaviour is not implemented here.
AD_VERIFY_SHARE_ENABLED = os.environ.get("AD_VERIFY_SHARE_ENABLED", "0").lower() in {"1", "true", "yes"}
# Verify cards rejected for missing fields, per issue: the field set that was logged. An invalid card
# must not repeat the same log line every tick, but a changed field set is logged again.
VERIFY_REJECT_LOGGED = {}

def ctl_state_dir():
    directory = os.path.expanduser(os.environ.get("OPENSWARM_CTL_STATE_DIR", f"{HOME}/phanes-ctl/state"))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory

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
      "backend":r.get("backend"),"model":r.get("model"),"error_class":r.get("error_class"),"summary":(str(res.get("summary") or "") if isinstance(res,dict) else str(res or ""))[:1200],
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

def requeue_orphaned_issue(L, st, r, why):
    """Give the issue of an orphaned (or lost) run back to the queue.

    The labels are the ones the scheduler expects: `in-progress` is removed and `lane-ready` is added, so the
    issue can be dispatched again. An issue that already got a PR (or that is closed) is only dropped.

    Returns whether the issue was released. A failed read or a failed label edit returns False: the lane keeps
    its record and a later tick retries, so a lane is never dropped while its issue still carries
    `in-progress` (the scheduler skips that label: such an issue would never be dispatched again)."""
    n = st["issue"]; rc, out, err = gh(["issue", "view", str(n), "--json", "state,labels"])
    if rc:
        log(f"{L} #{n} run {st['run']} {why} → issue read failed (gh rc={rc}: {(err or out).strip()[-120:]}); lane kept", brain=False)
        return False
    try: j = json.loads(out); labels = {l["name"] for l in j["labels"]}
    except Exception: return False
    issue_state = str(j.get("state", ""))
    if issue_state == "OPEN" and "lane-pr" not in labels:
        rc, _, err = gh(["issue", "edit", str(n), "--remove-label", "in-progress", "--add-label", "lane-ready"])
        if rc:
            log(f"{L} #{n} run {st['run']} {why} → requeue failed (gh issue edit rc={rc}: {(err or '').strip()[-120:]}); lane kept for a retry", brain=False)
            return False
        what = "requeued (lane-ready)"
    else: what = f"already {issue_state.lower()}/{'lane-pr' if 'lane-pr' in labels else '-'}; dropped"
    log(f"{L} #{n} run {st['run']} {why} → {what}")
    return True

def reconcile(L, cfg, st, r, state):
    """Local state says busy but the remote lane is idle on another/no run, the run has no result.json and its runner is dead:
    the run was orphaned (lane restart / reassignment). Drop it; requeue the issue unless it already got a PR or closed.

    A failed label edit keeps the lane record (the requeue helper returns False): the issue still carries
    `in-progress`, so the lane must not be dropped before the labels are back in sync."""
    if r.get("run") != "running" or r.get("lane") != "idle" or r.get("current_run") == st["run"]: return False
    if r.get("runner_alive") or (r.get("runner_alive") is None and age_s(st) < 600) or age_s(st) < 180: return False
    return requeue_orphaned_issue(L, st, r, f"orphaned (remote lane idle, current_run={r.get('current_run')}, run_state={r.get('run_state')}, runner dead)")

def run_is_lost(st, r, min_age_s=LOST_RUN_MIN_AGE_S):
    """True when the run of `st` is gone: it wrote no result, the lane does not run it any more and its runner is dead.

    The age guard keeps a runner that just died out of the verdict: it may still be writing its result.json."""
    if r.get("run") != "running": return False              # a result exists: the run finished
    if r.get("current_run") == st["run"]: return False       # the lane still runs our run
    if r.get("runner_alive"): return False                  # the runner is still there
    if r.get("runner_alive") is None and age_s(st) < 600: return False   # unknown runner: too early to call it lost
    return age_s(st) >= min_age_s

# ---------------------------------------------------------------- host recovery
def host_pause_path(host):
    """Return the path of the PAUSE-HOST-<host> marker of the autodispatch state dir."""
    return os.path.join(D, PAUSE_HOST_PREFIX + str(host))

def classify_host_hold(body):
    """Classify the body of a PAUSE-HOST marker.

    "manual"    an operator hold (no `auto:` prefix): never released here.
    "cooldown"  a machine cooldown with its own deadline.
    "resource"  a disk/memory hold owned by the health self-heal path.
    "outage"    an automatic hold written because the host was unreachable.
    "unknown"   an automatic hold of an unknown cause: its owner has to release it."""
    body = (body or "").strip()
    if not body.startswith(AUTO_PREFIX): return "manual"
    if body.startswith("auto:cooldown:"): return "cooldown"
    if RESOURCE_HOLD_RE.search(body): return "resource"
    if UNREACHABLE_HOLD_RE.search(body): return "outage"
    return "unknown"

def review_host_hold(host):
    """Review the PAUSE-HOST marker of a host that answered again.

    Only a hold whose body names the unreachable host is released: an operator hold, a machine cooldown, a
    disk/memory hold and an automatic hold of an unknown cause are kept, because the host answering ssh again
    says nothing about them. The cause is read from the body of the hold, never from its write time: an
    independent protection can be written while the host is down, and the host coming back does not cure it."""
    path = host_pause_path(host)
    try:
        with open(path, encoding="utf-8") as f: body = f.read()
    except OSError:
        return {"host": str(host), "present": False, "kind": "none", "released": False, "reason": "no-hold"}
    kind = classify_host_hold(body)
    record = {"host": str(host), "present": True, "kind": kind, "body": body.strip(), "released": False}
    if kind == "manual": record["reason"] = "manual-hold"; return record
    if kind == "cooldown": record["reason"] = "cooldown-hold"; return record
    if kind == "resource": record["reason"] = "resource-hold"; return record
    if kind == "unknown": record["reason"] = "unknown-hold"; return record
    record["reason"] = "host-recovered"
    try: os.unlink(path)
    except OSError as error:
        record["reason"] = f"release-failed: {error}"; return record
    record["released"] = True
    return record

def host_reachability(lanes, res):
    """Return the reachability of every host: True (it answered), False (a probe failed), None (not probed).

    A host that is in the ssh backoff is not probed at all, so its reachability is unknown in this tick and the
    last observation stands."""
    hosts = {}
    for L, cfg in lanes.items(): hosts.setdefault(cfg["host"], []).append(L)
    reach = {}
    for h, host_lanes in hosts.items():
        probed = [res[L] for L in host_lanes if L in res and not res[L].get("skip")]
        reach[h] = None if not probed else not any("error" in r for r in probed)
    return reach

def append_recovery(record):
    """Append a recovery record to the recovery log of the state dir."""
    try:
        with open(os.path.join(D, RECOVERY_LOG), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as error:
        log(f"cannot write {RECOVERY_LOG}: {error}", brain=False)

def recover_host(host, lanes, state, res, down_since, now=None):
    """A host answered again: review its hold and verify the runs that were in flight.

    The runs are verified with the probe of this tick: a run that survived keeps its lane, a run that was lost
    is given back to the queue with the labels of the orphan flow (`in-progress` removed, `lane-ready` added),
    and a lane whose probe did not answer is never touched."""
    now = time.time() if now is None else now
    hold = review_host_hold(host)
    kept, requeued = [], []
    for L, cfg in lanes.items():
        if cfg["host"] != host: continue
        st = state.get(L)
        if not isinstance(st, dict): continue
        r = res.get(L)
        if not r or "error" in r: continue        # the probe did not answer: never touch the lane
        # the run of this lane is gone: the issue goes back to the queue with the labels of the orphan flow.
        # A failed label edit keeps the lane record, so the next tick retries instead of losing the issue.
        if run_is_lost(st, r) and requeue_orphaned_issue(L, st, r, f"lost with the host (lane={r.get('lane')}, current_run={r.get('current_run')}, run_state={r.get('run_state')}, runner dead)"):
            state.pop(L, None); save_state(state); requeued.append(L); continue
        kept.append(L)
    record = {"host": str(host), "down_since": down_since, "down_s": round(float(now) - float(down_since), 3),
              "recovered_at": now, "hold": hold, "kept": kept, "requeued": requeued}
    append_recovery(record)
    log(f"host {host} recovered after {int(record['down_s'])}s: hold {hold['kind']} ({hold['reason']}), "
        f"lanes kept {' '.join(kept) or '-'}, requeued {' '.join(requeued) or '-'}")
    return record

def recover_hosts(lanes, state, res, now=None):
    """Act on the down -> up edge of every host (Refs #1673).

    The reachability comes from the ssh probes of this tick; the observation is kept in the control plane state
    (`_hosts`), so a restart of the loop does not lose the edge. When a host that was unreachable answers again
    its hold is reviewed and the runs that were in flight on it are verified. Returns the recovery records."""
    now = time.time() if now is None else now
    seen = state.get("_hosts") if isinstance(state.get("_hosts"), dict) else {}
    changed = False
    records = []
    for h, reachable in host_reachability(lanes, res).items():
        if reachable is None: continue       # the host was not probed (ssh backoff): keep the last observation
        record = seen.get(h) if isinstance(seen.get(h), dict) else {}
        was_reachable = bool(record.get("reachable")); down_since = record.get("down_since")
        if not reachable:
            new = {"reachable": False, "down_since": down_since or now}
            if new != {"reachable": was_reachable, "down_since": down_since}:
                seen[h] = new; changed = True
            continue
        seen[h] = {"reachable": True, "down_since": None}; changed = True
        if was_reachable or down_since is None: continue    # no outage: nothing to recover
        records.append(recover_host(h, lanes, state, res, down_since, now))
    if changed:
        state["_hosts"] = seen
        save_state(state)
    return records

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
        label_lane_failed(n, f"Lane {lane} ({cfg['host']}, run `{run}`) ended `{info.get('status')}` without a commit "
            f"(error_class `{info.get('error_class')}`).\n\nExecutor summary: {info.get('summary')}\n\nLabel `lane-failed`; re-add `lane-ready` to retry.")
        mark_done(state, n); log(f"{lane} ended #{n} without a commit (run {run}, {info.get('status')}/{info.get('error_class')}) → labelled lane-failed")
        return {"done": True, "status": "lane-failed", "error": None, "missing": []}

def tick(lanes, state, last_warn):
    if os.path.exists(f"{D}/PAUSE"): return last_warn
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
    # a host that answers again releases its hold and its in-flight runs are verified (Refs #1673)
    recover_hosts(lanes, state, res)
    for L, r in res.items():
        st = state.get(L)
        if "error" in r: continue
        if st and isinstance(r.get("run"), dict):
            if lane_in_fetch_backoff(st): continue  # harvest fetch is backing off after a transient failure (Refs #947)
            result = finish(L, lanes[L], st, r["run"], state)
            if result.get("done"): state.pop(L); save_state(state)
            if result.get("error"): log(f"{L} #{st['issue']} run {st['run']}: {result['error']}", brain=False)
        elif st and reconcile(L, lanes[L], st, r, state): state.pop(L); save_state(state)
    try: q = queue()
    except Exception as e: log(f"queue error: {e}", brain=False); return last_warn
    for i in q: i["deps_ok"] = deps_met(i)
    # one authoritative read per issue per tick, shared by the dispatch candidates and the count
    prefetch_labels([i["n"] for i in q])
    base = None
    for L, cfg in lanes.items():
        r = res[L]
        if "error" in r or not host_ok(cfg["host"]) or state.get(L) or r.get("lane") != "idle" or os.path.exists(f"{D}/PAUSE-{L}"): continue
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
        filed = replenish(max(LOW - ready, starve), state)
        why = f"queue low ({ready} < {LOW})" if ready < LOW else f"backend starved ({', '.join(f'{b} short {k}' for b, k in short.items() if k > 0)})"
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
