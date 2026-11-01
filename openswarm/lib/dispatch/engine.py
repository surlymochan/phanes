#!/usr/bin/env python3
"""openswarm dispatch: push-mode dispatcher for resident lanes driven by a GitHub label queue.

Every tick it (1) reads every lane's state on its host (one ssh per host, local hosts in-process),
(2) harvests finished runs (push branch, open PR, relabel the issue; review runs post a verdict and a
commit status), (3) dispatches PR reviews to idle lanes, then (4) gives each remaining idle lane the next
`ready` issue (priority label, backend pin, cross-backend fallback, hot-file limits, dependencies).

Everything project specific (repo, labels, hosts, prompts, file limits) lives in one JSON config:

    python3 openswarm/lib/dispatch/engine.py dispatch serve --config dispatch.json      # resident loop
    python3 openswarm/lib/dispatch/engine.py dispatch tick  --config dispatch.json      # one tick
    python3 openswarm/lib/dispatch/engine.py dispatch tick  --config dispatch.json --dry-run --json  # shadow

The `openswarm dispatch` subcommand wiring (and swapping the inline policy helpers for the pure
lib/dispatch/{queue,lane_pick,fallback,hotfiles,pause,quota,...}.py modules) belongs to #1359.

State, pause markers and logs live in `state_dir`:
  state.json, reviews.json, lane-lru.json, lanes-status.json, heartbeat, <log_file>
  PAUSE (whole loop), PAUSE-<lane>, PAUSE-HOST-<host>, PAUSE-BACKEND-<backend>, NO-LANE-REVIEW,
  and an optional burn marker (config burn.file) that gives burn.backend lanes first pick.
"""
import os
import sys

# Run as a script, Python puts this directory first on sys.path, where sibling modules (queue.py, ...) would
# shadow the standard library (concurrent.futures needs the real `queue`). Drop it before any other import.
if sys.path and os.path.abspath(sys.path[0] or ".") == os.path.dirname(os.path.abspath(__file__)):
    del sys.path[0]

import argparse  # noqa: E402
import calendar  # noqa: E402
import concurrent.futures as cf
import copy
import fnmatch
import importlib.util
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time


def _load_review_status():
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "review", "status.py"),
        os.path.join(os.getcwd(), "openswarm", "lib", "review", "status.py"),
        os.path.join(os.getcwd(), "lib", "review", "status.py"),
    ]
    for p in candidates:
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("openswarm_review_status", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


status = _load_review_status()


_DISPATCH_MODULES = {}


def dispatch_module(name):
    """Load siblings by path without adding dispatch/ to the stdlib search path.

    Lazy loading also keeps standalone check-config usable without runtime modules.
    """
    if name not in _DISPATCH_MODULES:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
        spec = importlib.util.spec_from_file_location("openswarm_dispatch_" + name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _DISPATCH_MODULES[name] = module
    return _DISPATCH_MODULES[name]


def compatible_lru_picker(path):
    module = dispatch_module("lane_pick")

    class CompatiblePicker(module.LruPicker):
        def state(self):
            current = super().state()
            if current["lanes"] or current["counter"]:
                return current
            try:
                with open(self.state_path, encoding="utf-8") as stream:
                    raw = json.load(stream)
                if not isinstance(raw, dict) or not raw:
                    return current
                if not all(isinstance(name, str) and name.strip() and isinstance(stamp, str)
                           for name, stamp in raw.items()):
                    return current
                # Flat timestamp maps were written by the old dispatcher. Validate
                # every entry before translating; a damaged history is never partial.
                stamps = {name: time.strptime(stamp, "%Y-%m-%d %H:%M:%S")
                          for name, stamp in raw.items()}
            except (OSError, ValueError, TypeError):
                return current
            ranks = {stamp: index + 1 for index, stamp in enumerate(sorted(set(stamps.values())))}
            return {"version": module.STATE_VERSION, "counter": len(ranks),
                    "lanes": {name: ranks[stamp] for name, stamp in stamps.items()}}

    return CompatiblePicker(path)

# ---------------------------------------------------------------- config

DEFAULTS = {
    "repo": "",
    "local_repo": "",
    "state_dir": "~/.claude/openswarm-dispatch",
    "lanes_file": "",                     # default <state_dir>/lanes.json
    "log_file": "dispatch.log",
    "brain_log": "",                      # optional markdown log that gets short lines
    "brain_log_prefix": "dispatch",
    "tick_s": 60,
    "budget_minutes": "45",
    "base_branch": "main",
    "pr_branch_template": "fleet/{lane}-issue{n}-{run}",
    "labels": {
        "ready": "lane-ready", "in_progress": "in-progress", "pr": "lane-pr", "failed": "lane-failed",
        "reviewed": "lane-reviewed", "needs_changes": "needs-changes", "needs_human": "needs-human",
        "strict_backend": "strict-backend", "backend_prefix": "backend:",
    },
    "priorities": {"p0": 0, "p1": 1, "p2": 2},
    "fallback": {"pi": ["codex", "copilot", "agy"], "codex": ["pi", "copilot", "agy"],
                 "copilot": ["codex", "pi", "agy"], "agy": ["pi", "codex", "copilot"]},
    "never_take_re": {"codex": r"unix\s*socket|AF_UNIX|socketpair|needs-socket|Unix 套接字|套接字"},
    "verify_line_re": r"\s*(?:验收|Verify|verify|Acceptance)\s*[:：]",
    "depends_line_re": r"^\s*(?:依赖|Depends on)\s*[:：](.*)$",
    "rebase_line_re": r"^\s*(?:重基PR|Rebase-PR)\s*[:：]\s*#(\d+)\s+(\S+)",
    "hot_files": {"line_re": r"^\s*(?:热文件|Hot-files)\s*[:：](.*)$", "union": [], "limits": {}, "default_limit": 1},
    "leak_grace_s": 900,
    "resume_grace_s": 120,
    "stale_strip_after": 5,
    "fresh_read_budget": 60,              # authoritative `gh issue view` reads per tick (1 GraphQL point each)
    "assign_idempotency_key": True,       # pass `lane assign --idempotency-key` (needs a fleet release with #1388)
    "status_tick_cap_s": 55,
    "harvest_cap_s": 100,
    "review": {
        "enabled": True, "max": 8, "burn_max": 12, "rounds": 3, "max_fails": 4,
        "pref": {"pi": 0, "codex": 1, "copilot": 2, "agy": 3},
        "status_context": "lane-review", "ignore_checks_re": "macos",
        # with this marker file in state_dir, a concluded ignored-check failure counts as red on PRs whose title matches
        "ignored_checks_red_file": "MACOS-UPSTREAM-RED", "ignored_checks_red_title_re": "upstream",
        "head_ref_re": r"(fleet|brain)/", "fix_ref_prefix": "fleet/",
        "author_lane_re": r"fleet/([A-Za-z]+\d+)-issue", "diff_limit": 60000, "verdict_dir": ".lane-review",
        "cifix_needs_burn": True,
    },
    # Timed mode: backend, window.resets_at (epoch), burn_before_reset_h and factor.
    # A legacy file marker remains supported when no timed parameters are supplied.
    "burn": {"file": "", "backend": ""},
    "local_hosts": [],
    "local_env": {},
    "local_inode": {"pause_pct": 95, "resume_pct": 92, "path": "~"},
    "ssh": {
        "base_opts": ["-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "-o", "ConnectionAttempts=1",
                      "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                      "-o", "ControlMaster=auto", "-o", "ControlPath={control_dir}/%r@%h:%p",
                      "-o", "ControlPersist=120"],
        "control_dir": "~/.ssh/cm-dispatch",
        "jump": {},                       # host -> ProxyCommand (ProxyJump=none is added)
        "status_timeout_s": 30, "jump_status_timeout_s": 45,
    },
    "remote_home": "~/.claude/openswarm-executor-candidate",   # = OPENSWARM_HOME forced by bin/openswarm-fleet
    "default_fleet_bin": "~/.local/bin/openswarm-fleet",
    "default_repo_path": "~/work/repo",
    "watchdog_requeue_file": "",
    "quota": {
        "backend_pause": True,
        "lane_pause": {},                 # backend -> regex: matching run errors pause that lane
        "requeue_classes": ["rate_limit", "quota"],
        # Per-backend quota table in lib/dispatch/quota.py's own shape: quota_error_re is
        # matched against the run's error/summary/status text, cooldown_s is the fallback
        # cooldown (a "resets in ..." / "Retry-After" in the text wins), scope says what is
        # paused: "lane" -> PAUSE-<lane>, "backend" -> PAUSE-BACKEND-<backend>.
        "backends": {
            "copilot": {"quota_error_re": r"402|429|RESOURCE_EXHAUSTED|quotaReached|Individual quota|usage.?limit|weekly.?limit|rate.?limit|quota\s+(exceeded|reached|exhausted)",
                        "cooldown_s": 3600, "scope": "lane"},
            "codex": {"quota_error_re": r"429|RESOURCE_EXHAUSTED|quotaReached|Individual quota|usage.?limit|weekly.?limit|rate.?limit|quota\s+(exceeded|reached|exhausted)",
                      "cooldown_s": 3600, "scope": "backend"},
            "agy": {"quota_error_re": r"429|RESOURCE_EXHAUSTED|quotaReached|Individual quota|usage.?limit|weekly.?limit|rate.?limit|quota\s+(exceeded|reached|exhausted)",
                    "cooldown_s": 1800, "scope": "backend"},
            "pi": {"quota_error_re": r"429|RESOURCE_EXHAUSTED|quotaReached|Individual quota|usage.?limit|weekly.?limit|rate.?limit|quota\s+(exceeded|reached|exhausted)",
                   "cooldown_s": 1800, "scope": "backend"},
        },
    },
    "upstream_manifest": "",              # e.g. upstream-sync.json: prefix PR title with upstream(<comp>):
    "thin_pr": {"min_effective_diff": 30,
                "doc_ext": [".md", ".txt", ".rst", ".adoc"],
                "code_ext": [".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".go", ".rs", ".sh"],
                "code_dirs": ["tests/", "tools/", "bin/"]},
    "replenish": {"cmd": [], "low": 20, "target": 24, "skip_files": ["PAUSE-MINT", "NO_SEED_MINT"]},
    "templates": {
        "rules": ("Rules: work only inside this repository worktree (the current directory). Never create, modify, move or "
                  "delete files outside it. Do not push, do not open PRs, do not touch credentials, no network installs. "
                  "Commit your result with a clear message referencing the issue (\"Refs #{n}\")."),
        "rebase_line": "Rebase-PR: #{pr} {branch}",
        "rebase_section_title": "Auto rebase (conflict)",
        "rebase_section": ("PR #{pr} head `{sha10}` conflicts with main. Start from branch `rebase-pr-{pr}` (the PR head), "
                           "rebase onto main, resolve conflicts keeping main's merged changes and this PR's intent, run the "
                           "acceptance and commit; the dispatcher force-pushes back to `{branch}` and re-reviews."),
        "cifix_section_title": "Auto fix CI (red)",
        "cifix_section": ("PR #{pr} head `{sha10}` failed CI: {checks}. Start from branch `rebase-pr-{pr}`, rebase onto main, "
                          "reproduce and fix the failing tests (fix the implementation; do not delete or skip tests), run the "
                          "acceptance and commit; the dispatcher force-pushes back to `{branch}` and re-reviews."),
        "changes_section_title": "Lane review requested changes",
        "changes_section": ("Review round {round} ({who}) on PR #{pr} head `{sha10}` returned CHANGES. Start from branch "
                            "`rebase-pr-{pr}`, rebase onto main, apply the notes below and commit; the dispatcher force-pushes "
                            "back to `{branch}` and re-reviews.\n\n{notes}"),
        "review_task": ("Task type: PR review (review only, do not change code).\n\nPR #{pr} `{branch}` (author lane {author}): {title}\n"
                        "Head sha `{sha}`. The worktree is main; below are the PR diff and its issue's requirements.\n\n"
                        "Check correctness, whether the acceptance is really met, out-of-scope changes (unrelated files, deleted "
                        "tests, hard-coded secrets/paths), obvious bugs or regressions. Do not run tests, do not leave the worktree, "
                        "do not modify existing files.\n\nOnly output: create `{file}` and commit it (Refs #{pr}), exactly:\n"
                        "```\nVERDICT: APPROVE   (or VERDICT: CHANGES)\nSUMMARY: one line\n## Findings\n- file:line findings\n"
                        "## Required changes\n- only for CHANGES\n```\nGive CHANGES only for errors, unmet acceptance or scope "
                        "violations; style is not a reason.\n\n{issue}\n\n### PR diff\n\n````diff\n{diff}\n````\n\n"
                        "Acceptance: `grep -qE '^VERDICT: (APPROVE|CHANGES)' {file}`"),
    },
}


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path):
    with open(os.path.expanduser(path)) as f:
        raw = json.load(f)
    cfg = deep_merge(DEFAULTS, raw)
    if not cfg["repo"]:
        raise SystemExit("dispatch config: `repo` is required")
    cfg["state_dir"] = os.path.expanduser(cfg["state_dir"])
    cfg["local_repo"] = os.path.expanduser(cfg["local_repo"] or "")
    if not cfg["lanes_file"]:
        cfg["lanes_file"] = os.path.join(cfg["state_dir"], "lanes.json")
    cfg["lanes_file"] = os.path.expanduser(cfg["lanes_file"])
    if cfg["brain_log"]:
        cfg["brain_log"] = os.path.expanduser(cfg["brain_log"])
    return cfg


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data, indent=1):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)


def _kill_pg(pid):
    for fn in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
        try:
            fn()
        except Exception:
            pass


def run_cmd(cmd, timeout=120, inp=None, env=None):
    """Hard wall-clock deadline: timeout(1) plus a watchdog that SIGKILLs the process group.
    timeout(1) alone has left ssh/ProxyCommand wedged for half an hour."""
    t = str(max(1, int(timeout)))
    wrapped = ["timeout", "--signal=KILL", "-k", "2", t] + (["bash", "-lc", cmd] if isinstance(cmd, str) else list(cmd))
    proc = subprocess.Popen(wrapped, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                            stdin=subprocess.PIPE if inp is not None else None, start_new_session=True)
    done = {"v": False}

    def watch():
        deadline = time.time() + int(t) + 3
        while time.time() < deadline:
            if done["v"] or proc.poll() is not None:
                return
            time.sleep(0.5)
        if proc.poll() is None:
            _kill_pg(proc.pid)

    threading.Thread(target=watch, daemon=True).start()
    try:
        out, err = proc.communicate(input=inp, timeout=int(t) + 8)
    except subprocess.TimeoutExpired:
        done["v"] = True
        _kill_pg(proc.pid)
        try:
            proc.communicate(timeout=3)
        except Exception:
            pass
        raise
    finally:
        done["v"] = True
    return (proc.returncode if proc.returncode is not None else 124), out or "", err or ""


_QUOTA_MOD = None


def quota_module():
    """The pure quota classifier (lib/dispatch/quota.py), loaded from its sibling path.

    It is loaded by path and not by name: the dispatch directory sits first on sys.path when
    this file runs as a script, and its modules must never shadow the stdlib (see above).
    """
    global _QUOTA_MOD
    if _QUOTA_MOD is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quota.py")
        spec = importlib.util.spec_from_file_location("openswarm_dispatch_quota", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _QUOTA_MOD = mod
    return _QUOTA_MOD


REMOTE_STATUS = r'''
import json,os,sys
O=os.path.expanduser(sys.argv[3] if len(sys.argv)>3 and sys.argv[3] else "~/.claude/openswarm"); L=sys.argv[1]; R=sys.argv[2] if len(sys.argv)>2 else ""
out={"lane":None,"run":None}
try:
  ls=json.load(open(f"{O}/lanes/{L}/state.json")); out["lane"]=ls.get("state"); out["current_run"]=ls.get("current_run")
except Exception as e: out["lane"]="unknown"
if R:
  p=f"{O}/runs/{R}/result.json"
  if os.path.exists(p):
    r=json.load(open(p)); res=r.get("result") or {}
    out["run"]={"status":r.get("status"),"branch":r.get("branch"),"commit":r.get("commit"),"repo":(r.get("workspace") or {}).get("repo"),
      "backend":r.get("backend"),"model":r.get("model"),"error_class":r.get("error_class"),"summary":str(res.get("summary") if isinstance(res,dict) else res)[:600],
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


class DryRun(Exception):
    pass


class Dispatcher:
    """One dispatcher over one repo. `runner(cmd, timeout, inp, env)` is injectable for tests."""

    def __init__(self, cfg, runner=None, dry_run=False, clock=time.time, out=None):
        self.cfg = cfg
        self.D = cfg["state_dir"]
        if not dry_run:                       # shadow mode creates nothing
            os.makedirs(self.D, exist_ok=True)
        self.runner = runner or run_cmd
        self.dry_run = dry_run
        self.label_cache, self.label_reads = {}, 0
        self.clock = clock
        self.out = out or sys.stdout
        self.L = cfg["labels"]
        self.R = cfg["review"]
        self.backoff = {}
        self.state_lock = threading.Lock()
        self.rev_lock = threading.RLock()
        self.inode = {"t": 0.0, "low": False}
        self.last_leak_sweep = 0.0
        self.last_warn = 0.0
        self.plan = []   # dry-run decisions
        self.hot_union = set(cfg["hot_files"].get("union") or [])
        self.hot_limits = dict(cfg["hot_files"].get("limits") or {})
        self.local_hosts = set(cfg.get("local_hosts") or [])
        self.closes_re = re.compile(r"(?:Closes|Fixes|Resolves)\s+#(\d+)", re.I)
        self._lru_picker = None

    # ------------------------------------------------------------ io helpers
    def path(self, name):
        return os.path.join(self.D, name)

    def sh(self, cmd, timeout=120, inp=None, env=None):
        return self.runner(cmd, timeout=timeout, inp=inp, env=env)

    def write(self, cmd, timeout=120, inp=None, env=None, what=""):
        """A side-effecting command. In dry-run it is recorded instead of executed."""
        if self.dry_run:
            self.plan.append({"would": what or "exec", "cmd": cmd if isinstance(cmd, str) else " ".join(map(str, cmd))[:300]})
            return 0, "", ""
        return self.runner(cmd, timeout=timeout, inp=inp, env=env)

    def gh(self, args, timeout=60):
        return self.sh(["gh"] + args + ["-R", self.cfg["repo"]], timeout=timeout)

    def gh_write(self, args, timeout=60):
        """Every GitHub write (labels, bodies, comments, statuses): a failure is logged, never silent."""
        try:
            rc, out, err = self.write(["gh"] + args + ["-R", self.cfg["repo"]], timeout=timeout, what="gh " + " ".join(args[:2]))
        except subprocess.TimeoutExpired:
            rc, out, err = 124, "", "timeout"
        if rc:
            flags = " ".join(a for a in args if a.startswith("--") and a != "--body")
            self.log(f"WRITE FAILED rc={rc}: gh {' '.join(args[:3])} {flags[:120]}: {(err or out or '').strip()[-160:]}", brain=False)
        return rc, out, err

    def git(self, *args, timeout=120):
        return self.sh(["git", "-C", self.cfg["local_repo"]] + list(args), timeout=timeout)

    def now_hm(self):
        return time.strftime("%H:%M")

    def log(self, msg, brain=True):
        line = f"{time.strftime('%F %T')} {msg}"
        print(line, file=self.out, flush=True)
        if self.dry_run:
            self.plan.append({"log": msg})
            return
        with open(self.path(self.cfg["log_file"]), "a") as f:
            f.write(line + "\n")
        if brain and self.cfg.get("brain_log"):
            with open(self.cfg["brain_log"], "a") as f:
                f.write(f"- {self.now_hm()} UTC+8 {self.cfg['brain_log_prefix']}: {msg}\n")

    def heartbeat(self):
        if self.dry_run:
            return
        with open(self.path("heartbeat"), "w") as f:
            f.write(time.strftime("%F %T") + "\n")

    def touch_file(self, name, text):
        if self.dry_run:
            self.plan.append({"would": "touch", "file": name})
            return
        with open(self.path(name), "w") as f:
            f.write(text)

    def save_state(self, s):
        if self.dry_run:
            return
        with self.state_lock:
            write_json(self.path("state.json"), s)

    # ------------------------------------------------------------ ssh / hosts
    def ssh_opts(self, host):
        s = self.cfg["ssh"]
        cd = os.path.expanduser(s["control_dir"])
        o = [x.replace("{control_dir}", cd) for x in s["base_opts"]]
        pc = (s.get("jump") or {}).get(host)
        if pc:
            o += ["-o", "ProxyJump=none", "-o", f"ProxyCommand={pc}"]
        return o

    def is_jump(self, host):
        return host in (self.cfg["ssh"].get("jump") or {})

    def git_ssh_command(self, host, limit=45):
        parts = ["timeout", "--signal=TERM", "-k", "2", str(int(limit)), "ssh"] + self.ssh_opts(host)
        return " ".join(shlex.quote(x) for x in parts)

    def host_ok(self, h):
        return self.clock() >= self.backoff.get(h, {}).get("until", 0)

    def host_fail(self, h, why, started_at=None, timeout_s=None):
        now = self.clock()
        # A command that ran far past its own deadline commonly crossed a VM suspend.
        if (started_at is not None and timeout_s is not None
                and now - started_at > float(timeout_s) + 30):
            self.log(f"{h} ssh transient error ignored after long call ({str(why)[-80:]})", brain=False)
            return
        try:
            with open(self.path("box-resume"), encoding="utf-8") as stream:
                resumed_at = float(stream.read().strip())
        except (OSError, ValueError):
            resumed_at = None
        grace = float(self.cfg.get("resume_grace_s", 120))
        if resumed_at is not None and 0 <= now - resumed_at < grace:
            self.log(f"{h} ssh transient error ignored during VM resume grace ({str(why)[-80:]})", brain=False)
            return
        b = self.backoff.setdefault(h, {"n": 0, "until": 0})
        b["n"] += 1
        wait = min(600, 30 * 2 ** (b["n"] - 1))
        b["until"] = now + wait
        self.log(f"{h} ssh transient error #{b['n']} ({str(why)[-80:]}); backing off {wait}s", brain=(b["n"] == 4))

    def host_good(self, h):
        if self.backoff.pop(h, None):
            self.log(f"{h} ssh recovered", brain=False)

    def local_inode_low(self):
        """In-process inode guard for local-host lanes (works with 0 inodes left)."""
        li = self.cfg.get("local_inode") or {}
        if not li:
            return False
        now = self.clock()
        if now - self.inode["t"] < 30:
            return self.inode["low"]
        self.inode["t"] = now
        try:
            st = os.statvfs(os.path.expanduser(li.get("path") or "~"))
            pct = 100 * (1 - st.f_favail / max(st.f_files, 1)) if st.f_files else 0
        except Exception:
            return self.inode["low"]
        return self.inode_decide(pct)

    def inode_decide(self, pct):
        li = self.cfg.get("local_inode") or {}
        hi, lo = float(li.get("pause_pct", 95)), float(li.get("resume_pct", 92))
        was = self.inode["low"]
        self.inode["low"] = pct >= hi or (was and pct >= lo)
        if self.inode["low"] != was:
            self.log(f"local inode {pct:.1f}% -> local lanes {'PAUSED' if self.inode['low'] else 'resumed'}")
        return self.inode["low"]

    def paused_lane(self, lane, lc):
        if os.path.exists(self.path(f"PAUSE-{lane}")):
            return True
        host = lc.get("host") or ""
        if host and os.path.exists(self.path(f"PAUSE-HOST-{host}")):
            return True
        if host in self.local_hosts and self.local_inode_low():
            return True
        be = lc.get("backend") or ""
        return bool(be and os.path.exists(self.path(f"PAUSE-BACKEND-{be}")))

    def burn_state(self):
        """Return the selected backend and factor, with legacy marker compatibility."""
        cfg = self.cfg.get("burn") or {}
        backend = cfg.get("backend") or ""
        now = self.clock()
        window = cfg.get("window")
        factor_cfg = cfg
        if window is None and not any(key in cfg for key in ("burn_before_reset_h", "factor")):
            marker = cfg.get("file")
            if not marker or not os.path.exists(self.path(marker)):
                return backend, 1
            # A legacy marker is an explicit operator-requested window. Keep its
            # review ceiling, while delegating the multiplier calculation as well.
            window = {"resets_at": now}
            factor_cfg = {"burn_before_reset_h": 1,
                          "factor": max(2, int(self.R["burn_max"]) / max(1, int(self.R["max"])))}
        factor = dispatch_module("burn").burn_factor(backend, now, window, {backend: factor_cfg})
        return backend, factor

    # ------------------------------------------------------------ remote status
    def remote_multi(self, h, items):
        prog = ("import json,sys,io,contextlib\nSRC=" + repr(REMOTE_STATUS) + "\nout={}\nH=" + repr(self.cfg["remote_home"]) +
                "\nargs=sys.argv[1:]\nfor a in args:\n  L,_,R=a.partition('@')\n  buf=io.StringIO()\n  sys.argv=['x',L,R,H]\n"
                "  try:\n    with contextlib.redirect_stdout(buf): exec(SRC,{'__name__':'__main__'})\n"
                "    out[L]=json.loads(buf.getvalue().strip().splitlines()[-1])\n"
                "  except Exception as e: out[L]={'error':'remote status exc: '+str(e)[:150]}\nprint(json.dumps(out))\n")
        argv = [f"{L}@{R or ''}" for L, R in items]
        s = self.cfg["ssh"]
        try:
            rc, out, err = self.sh(["ssh"] + self.ssh_opts(h) + [h, "python3", "-"] + argv,
                                   timeout=s["jump_status_timeout_s"] if self.is_jump(h) else s["status_timeout_s"], inp=prog)
        except subprocess.TimeoutExpired:
            return {L: {"error": "ssh status timeout"} for L, _ in items}
        try:
            d = json.loads(out.strip().splitlines()[-1])
            if not isinstance(d, dict):
                raise ValueError
        except Exception:
            e = "ssh status timeout" if rc in (124, 137) else ((err or out).strip()[-200:] or f"rc={rc}")
            return {L: {"error": e} for L, _ in items}
        return {L: d.get(L, {"error": "missing in batch"}) for L, _ in items}

    def remote_local(self, lane, run=""):
        try:
            rc, out, err = self.sh(["python3", "-", lane, run or "", self.cfg["remote_home"]], timeout=25, inp=REMOTE_STATUS)
        except subprocess.TimeoutExpired:
            return {"error": "ssh status timeout"}
        if rc:
            return {"error": "ssh status timeout"} if rc in (124, 137) else {"error": (err or out).strip()[-200:]}
        try:
            return json.loads(out.strip().splitlines()[-1])
        except Exception:
            return {"error": (out or "")[-200:]}

    # ------------------------------------------------------------ queue
    def skip_labels(self):
        """Labels that mean "not ours": cfg skip_labels (e.g. lane-claimed by an external lane) plus `backend:<b>` for
        every enabled external backend listed in the openswarm policy file (cfg external_backends_policy)."""
        skip = set(self.cfg.get("skip_labels") or [])
        pol = self.cfg.get("external_backends_policy")
        if pol:
            try:
                with open(os.path.expanduser(pol)) as f:
                    eb = json.load(f).get("external_backends") or {}
                skip |= {f"{self.L['backend_prefix']}{k}" for k, v in eb.items()
                         if not k.startswith("_") and (v or {}).get("enabled", True)}
            except (OSError, ValueError):
                pass
        return skip

    def queue(self):
        rc, out, err = self.gh(["issue", "list", "--state", "open", "--label", self.L["ready"], "--limit", "100",
                                "--json", "number,title,labels,body"])
        if rc:
            raise RuntimeError(err)
        pr = self.cfg["priorities"]
        bp = self.L["backend_prefix"]
        items = []
        skip = self.skip_labels()
        for i in json.loads(out):
            labels = {l["name"] for l in i["labels"]}
            if self.L["in_progress"] in labels or labels & skip:
                continue
            items.append({"n": i["number"], "title": i["title"], "body": i["body"] or "",
                          "prio": min([pr[l] for l in labels if l in pr] or [len(pr)]),
                          "backend": next((l[len(bp):] for l in labels if l.startswith(bp)), None),
                          "strict": self.L["strict_backend"] in labels})
        return sorted(items, key=lambda x: (x["prio"], x["n"]))

    def can_take(self, backend, item):
        if item["backend"] in (None, backend):
            return True
        if item.get("strict"):
            return False
        rx = (self.cfg.get("never_take_re") or {}).get(backend)
        return not (rx and re.search(rx, item["title"] + "\n" + item["body"], re.I))

    def rank(self, backend, item):
        if item["backend"] == backend:
            g = 0
        elif item["backend"] is None:
            g = 1
        else:
            fb = self.cfg["fallback"].get(backend) or []
            g = 2 + (fb.index(item["backend"]) if item["backend"] in fb else len(fb))
        return (g, item["prio"], item["n"])

    def verify_of(self, body):
        rx = re.compile(self.cfg["verify_line_re"])
        for line in reversed((body or "").splitlines()):
            if rx.match(line):
                cmds = re.findall(r"`([^`]+)`", line)
                if cmds:
                    return " && ".join(cmds) if len(cmds) > 1 and all(not c.startswith("cd ") for c in cmds[1:]) else cmds[0]
        return None

    def rebase_of(self, body):
        m = re.search(self.cfg["rebase_line_re"], body or "", re.M)
        return (int(m.group(1)), m.group(2)) if m else None

    def hot_of(self, body):
        m = re.search(self.cfg["hot_files"]["line_re"], body or "", re.M)
        if not m:
            return set()
        return {x.strip().strip("`") for x in re.split(r"[,，\s]+", m.group(1)) if x.strip()} - self.hot_union

    def hot_blocked(self, hot, held):
        dl = int(self.cfg["hot_files"].get("default_limit", 1))
        return any(held.get(f, 0) >= int(self.hot_limits.get(f, dl)) for f in hot)

    def deps_met(self, item):
        m = re.search(self.cfg["depends_line_re"], item["body"], re.M)
        for d in re.findall(r"#(\d+)", m.group(1)) if m else []:
            rc, out, _ = self.gh(["issue", "view", d, "--json", "state"])
            if rc or json.loads(out)["state"] != "CLOSED":
                return False
        return True

    def issue_labels(self, n):
        rc, out, _ = self.gh(["issue", "view", str(n), "--json", "state,labels"])
        if rc:
            return None, set()
        j = json.loads(out)
        return j["state"], {l["name"] for l in j["labels"]}

    def label_state(self, n):
        """The search index behind `issue list --label` lags label edits; re-read the issue directly.
        Returns "fresh" (dispatchable), "stale" (labels say no) or "unknown" (read failed or the per-tick read budget
        is spent). Only "stale" counts towards the stale-skip strip; "unknown" just waits for the next tick."""
        if n in self.label_cache:
            return self.label_cache[n]
        if self.label_reads >= int(self.cfg["fresh_read_budget"]):
            return "unknown"
        self.label_reads += 1
        st, labels = self.issue_labels(n)
        if st is None:
            res = "unknown"
        elif st == "OPEN" and self.L["ready"] in labels and \
                not labels & ({self.L["in_progress"], self.L["pr"], self.L["failed"]} | self.skip_labels()):
            res = "fresh"
        else:
            res = "stale"
        self.label_cache[n] = res
        return res

    def fresh_ready(self, n):
        return self.label_state(n) == "fresh"

    def note_stale_skip(self, n, state):
        key = str(n)
        skips = state.setdefault("_stale_skips", {})
        skips[key] = int(skips.get(key) or 0) + 1
        count = skips[key]
        self.save_state(state)
        if count < int(self.cfg["stale_strip_after"]):
            return
        st, labels = self.issue_labels(n)
        if st is None:
            return
        # Strip only residual `ready` next to a finished state (pr/failed/closed). An issue that is in progress
        # (e.g. a rebase/fix run) keeps its labels; harvest settles them.
        if self.L["ready"] in labels and self.L["in_progress"] not in labels:
            if not self.gh_write(["issue", "edit", str(n), "--remove-label", self.L["ready"]])[0]:
                self.log(f"stale-skip auto-strip {self.L['ready']} #{n} after {count} skips (state={st} labels={sorted(labels)})")
        skips.pop(key, None)
        self.save_state(state)

    def sweep_leaked_inprogress(self, state):
        """`in-progress` with no lane holding the issue: drop the stale label after leak_grace_s."""
        if self.clock() - self.last_leak_sweep < 300:
            return
        self.last_leak_sweep = self.clock()
        held = {st.get("issue") for L, st in state.items() if not L.startswith("_") and isinstance(st, dict)}
        rc, out, _ = self.gh(["issue", "list", "--state", "open", "--label", self.L["in_progress"], "--limit", "100",
                              "--json", "number,updatedAt"])
        if rc:
            return
        for i in json.loads(out):
            n = i["number"]
            if n in held:
                continue
            try:
                age = self.clock() - calendar.timegm(time.strptime(i["updatedAt"], "%Y-%m-%dT%H:%M:%SZ"))
            except Exception:
                continue
            if age < int(self.cfg["leak_grace_s"]):
                continue
            self.gh_write(["issue", "edit", str(n), "--remove-label", self.L["in_progress"]])
            self.log(f"leak-sweep: #{n} had {self.L['in_progress']} but no lane holds it (idle {int(age)}s) → removed")

    # ------------------------------------------------------------ LRU
    def lru_picker(self):
        if self._lru_picker is None:
            self._lru_picker = compatible_lru_picker(self.path("lane-lru.json"))
        return self._lru_picker

    # ------------------------------------------------------------ dispatch one task
    def task_text(self, item):
        n = item["n"]
        return f"# #{n} {item['title']}\n\n{item['body']}\n\n{self.cfg['templates']['rules'].format(n=n)}\n"

    def main_sha(self):
        rc, out, err = self.sh(["git", "ls-remote", f"https://github.com/{self.cfg['repo']}.git",
                                f"refs/heads/{self.cfg['base_branch']}"], timeout=30)
        if rc or not out.strip():
            raise RuntimeError(f"ls-remote failed: {(err or out)[-120:]}")
        return out.split()[0]

    def dispatch(self, lane, lc, item, base):
        if self.dry_run:
            self.plan.append({"would": "assign", "lane": lane, "issue": item["n"], "review": bool(item.get("review"))})
            return "dry-run", self.verify_of(item["body"])
        n = item["n"]
        task = self.task_text(item)
        os.makedirs(self.path("tasks"), exist_ok=True)
        with open(self.path(f"tasks/{lane}-{n}.md"), "w") as f:
            f.write(task)
        h = lc["host"]
        F = lc.get("fleet", self.cfg["default_fleet_bin"])
        R = lc.get("repo", self.cfg["default_repo_path"])
        repo_url = f"https://github.com/{self.cfg['repo']}.git"
        if h in self.local_hosts:
            with open(f"/tmp/task-{lane}.md", "w") as f:
                f.write(task)
            rc, err = 0, ""
        else:
            rc, _, err = self.sh(["ssh"] + self.ssh_opts(h) + [h, f"cat > /tmp/task-{lane}.md"], timeout=30, inp=task)
        if rc:
            raise RuntimeError(f"task-upload: {(err or '').strip()[-200:]}")
        ver = self.verify_of(item["body"])
        title = f"#{n} {item['title']}"[:120]
        rb = None if item.get("review") else self.rebase_of(item["body"])
        if lc.get("seed_from_box") and h not in self.local_hosts:
            # Host cannot reach GitHub reliably: the dispatcher fetches base / PR head and pushes them over ssh.
            refs = []
            if rb:
                try:
                    frc = self.git("fetch", "-q", "-f", repo_url, f"+pull/{rb[0]}/head:refs/ad/pr-{rb[0]}", timeout=40)[0]
                except subprocess.TimeoutExpired:
                    frc = 1
                if not frc:
                    refs.append(f"+refs/ad/pr-{rb[0]}:refs/heads/rebase-pr-{rb[0]}")
            if self.git("cat-file", "-e", f"{base}^{{commit}}")[0]:
                self.git("fetch", "-q", repo_url, self.cfg["base_branch"], timeout=60)
            sshc = self.git_ssh_command(h, limit=100)
            prc, _, perr = self.sh(["git", "-c", f"core.sshCommand={sshc}", "-C", self.cfg["local_repo"], "push", "-q", "-f",
                                    f"{h}:{R}", f"{base}:refs/ad/base"] + refs, timeout=120)
            if prc:
                raise RuntimeError(f"seed push to {h}: {(perr or '').strip()[-200:]}")
            pre = fetch_main = ""
        else:
            pre = (f"for _i in 1 2 3; do timeout 35 git -C {R} fetch -q -f {repo_url} "
                   f"+pull/{rb[0]}/head:refs/heads/rebase-pr-{rb[0]} && break; sleep $((_i*2)); done; ") if rb else ""
            fetch_main = (f"for _i in 1 2 3 4; do timeout 40 git -C {R} fetch -q {repo_url} {self.cfg['base_branch']} && break; "
                          f"sleep $((_i*2)); done && ")
        cmd = (pre + fetch_main + f"git -C {R} cat-file -e {base}^{{commit}} && "
               f"{F} lane assign {lane} --task-file /tmp/task-{lane}.md --base {base} --budget-minutes "
               f"{self.cfg['budget_minutes']} --title {shlex.quote(title)}" + (f" --verify {shlex.quote(ver)}" if ver else ""))
        key = None
        if self.cfg.get("assign_idempotency_key"):
            # one key per dispatch attempt: a retry after a dropped transport returns the run that already landed
            key = f"{lane}-{n}-{base[:12]}-{int(self.clock())}"
            cmd += f" --idempotency-key {shlex.quote(key)}"

        def run_assign():
            try:
                if h in self.local_hosts:
                    env = os.environ.copy()
                    for k, v in (self.cfg.get("local_env") or {}).items():
                        env.setdefault(k, os.path.expanduser(v))
                    return self.sh(["bash", "-lc", cmd], timeout=90, env=env)
                return self.sh(["ssh"] + self.ssh_opts(h) + [h, cmd], timeout=150 if rb else 140)
            except subprocess.TimeoutExpired as e:
                self.log(f"{lane} assign wall-timeout on {h}", brain=False)
                return 124, (getattr(e, "stdout", None) or ""), (getattr(e, "stderr", None) or "")

        rc, out, err = run_assign()
        if key and rc in (124, 255) and h not in self.local_hosts:
            self.log(f"{lane} assign transport rc={rc} on {h}; retrying with the same idempotency key", brain=False)
            rc, out, err = run_assign()
        m = re.search(r'"id":\s*"([^"]+)"', out or "")
        if (rc or not m) and h not in self.local_hosts:
            # The transport may drop after the assign landed: adopt the new run instead of re-dispatching.
            try:
                stt = self.remote_multi(h, [(lane, None)]).get(lane, {})
                if stt.get("current_run") and stt.get("lane") == "busy":
                    self.log(f"{lane} assign rc={rc} but run {stt['current_run']} exists on {h}; adopting", brain=False)
                    return stt["current_run"], ver
            except Exception:
                pass
        if rc or not m:
            raise RuntimeError(f"assign rc={rc}: {(err or out).strip()[-300:]}")
        return m.group(1), ver

    # ------------------------------------------------------------ harvest
    @staticmethod
    def mark_done(state, n):
        state["_done"] = (state.get("_done", []) + [n])[-200:]

    def age_s(self, st):
        try:
            return self.clock() - time.mktime(time.strptime(st["at"], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return 1e9

    def relabel(self, n, add, remove):
        args = ["issue", "edit", str(n)]
        for l in remove:
            args += ["--remove-label", l]
        args += ["--add-label", add]
        return self.gh_write(args)

    def reconcile(self, L, lc, st, r, state):
        """Local state busy but the remote lane is idle on another run with a dead runner: the run was orphaned."""
        if r.get("run") != "running" or r.get("lane") != "idle" or r.get("current_run") == st["run"]:
            return False
        if r.get("runner_alive") or (r.get("runner_alive") is None and self.age_s(st) < 600) or self.age_s(st) < 180:
            return False
        if st.get("review_pr"):
            self.log(f"{L} review PR#{st['review_pr']} run {st['run']} orphaned; dropped (will re-review)")
            return True
        n = st["issue"]
        sst, labels = self.issue_labels(n)
        if sst is None:
            return False
        if sst == "OPEN" and self.L["pr"] not in labels:
            self.relabel(n, self.L["ready"], [self.L["in_progress"]])
            what = f"requeued ({self.L['ready']})"
        else:
            what = f"already {sst.lower()}/{self.L['pr'] if self.L['pr'] in labels else '-'}; dropped"
        self.log(f"{L} #{n} run {st['run']} orphaned (remote lane idle, current_run={r.get('current_run')}, "
                 f"run_state={r.get('run_state')}, runner dead) → {what}")
        return True

    def quota_table(self):
        """The per-backend quota table lib/dispatch/quota.py classifies with.

        Shape (the module's own): {"<backend>": {"quota_error_re": str, "cooldown_s": int,
        "scope": "lane"|"backend"}}.  cfg["quota"]["backends"] holds it; the legacy
        cfg["quota"]["lane_pause"] (backend -> regex) is still read as a lane-scoped entry.
        """
        q = self.cfg.get("quota") or {}
        table = {}
        for be, bc in (q.get("backends") or {}).items():
            if isinstance(bc, dict) and bc.get("quota_error_re"):
                table[be] = dict(bc)
        for be, rx in (q.get("lane_pause") or {}).items():
            table.setdefault(be, {"scope": "lane"})
            table[be].setdefault("quota_error_re", rx)
        return table

    def quota_text(self, info):
        """The text a quota signal is judged on: error, summary and status only.

        A run id, a branch name or a path is never scanned: "run 20261102-042901-429abc" is
        not a rate limit.  A run that ended well is judged on its error text alone, so a
        summary that merely mentions 429 handling ("处理 429 限流") is feature text.
        """
        info = info or {}
        err = " ".join(str(info.get(k)) for k in ("error", "error_class") if info.get(k))
        status = str(info.get("status") or "")
        summary = str(info.get("summary") or "")
        if status in ("done", "partial") and not err:
            summary = ""
        return "\n".join(x for x in (err, summary, status) if x)

    def quota_hit(self, lane, lc, info):
        """Classify the run text with lib/dispatch/quota.py; None when there is no signal.

        The hit is {"kind", "scope", "cooldown_s"}: the kind is what the backend's pattern
        matched, the scope says what to pause, the cooldown is the seconds to wait.
        """
        be = (info or {}).get("backend") or (lc or {}).get("backend") or ""
        text = self.quota_text(info)
        if not be or not text:
            return None
        try:
            res = quota_module().classify(be, text, self.quota_table())
        except Exception as e:
            self.log(f"quota classifier unavailable: {e!r}", brain=False)
            return None
        if not res:
            return None
        return {"kind": re.sub(r"\s+", "_", str(res.get("reason") or "quota"))[:40],
                "scope": str(res.get("scope") or "backend"),
                "cooldown_s": int(res.get("ttl_s") or 0)}

    def quota_pause(self, name, lane, hit):
        """Write the automatic pause file; a manual (non-auto) pause is never overwritten."""
        p = self.path(name)
        if os.path.exists(p):
            try:
                with open(p) as f:
                    if not f.read().startswith("auto:"):
                        return False
            except Exception:
                return False
        cd = int(hit.get("cooldown_s") or 0)
        body = f"auto:quota:{lane}:{hit['kind']}"
        if cd:
            body += f" until={int(self.clock()) + cd}"
        self.touch_file(name, body + "\n")
        self.log(f"{name} (quota signal from {lane}: {hit['kind']})")
        return True

    def quota_lane_guard(self, lane, lc, info):
        """Pause the lane when the run text shows a lane-scoped quota error."""
        hit = self.quota_hit(lane, lc, info)
        if not hit or hit["scope"] != "lane":
            return
        self.quota_pause(f"PAUSE-{lane}", lane, hit)

    def quota_backend_guard(self, lane, lc, info):
        """Pause the backend when the run text shows a backend-scoped quota error."""
        if not (self.cfg.get("quota") or {}).get("backend_pause"):
            return
        hit = self.quota_hit(lane, lc, info)
        if not hit or hit["scope"] != "backend":
            return
        be = (info or {}).get("backend") or (lc or {}).get("backend") or ""
        if not be:
            return
        self.quota_pause(f"PAUSE-BACKEND-{be}", lane, hit)

    def upstream_title(self, subj, base, ref):
        man = self.cfg.get("upstream_manifest")
        if not man or "upstream" in subj.lower():
            return subj
        try:
            comps = json.loads(self.git("show", f"{ref}:{man}", timeout=30)[1])["components"]
            names = self.git("diff", "--name-only", f"{base}...{ref}", timeout=60)[1].split()
        except Exception:
            return subj
        for p in names:
            comp, _, rest = p.partition("/")
            if comp in comps and rest and not any(fnmatch.fnmatchcase(rest, pat) for pat in comps[comp].get("phanes_only") or []):
                return f"upstream({comp}): {subj}"
        return subj

    def pr_diff_too_thin(self, base, ref):
        tp = self.cfg.get("thin_pr") or {}
        mn = int(tp.get("min_effective_diff") or 0)
        if not mn:
            return ""
        rc, out, _ = self.git("diff", "--numstat", base, ref, timeout=30)
        if rc:
            return ""
        return classify_numstat(out, mn, tuple(tp.get("doc_ext") or ()), tuple(tp.get("code_ext") or ()),
                                tuple(tp.get("code_dirs") or ()))

    def fetch_lane_branch(self, lane, lc, info):
        if lc["host"] in self.local_hosts:
            src = os.path.expanduser(info.get("repo") or self.cfg["default_repo_path"])
            return ["git", "-C", self.cfg["local_repo"], "fetch", "-q", src, f"+{info['branch']}:refs/fleet/{lane}"]
        sshc = self.git_ssh_command(lc["host"], limit=35)
        return ["git", "-c", f"core.sshCommand={sshc}", "-C", self.cfg["local_repo"], "fetch", "-q",
                f"{lc['host']}:{info['repo']}", f"+{info['branch']}:refs/fleet/{lane}"]

    TRANSIENT = r"Permission denied|Connection closed|timed out|banner|124|Usage:|hung up|reset by peer"
    GIVEUP = r"Connection closed|Permission denied|timed out|banner|124|hung up|reset by peer|Could not resolve|No route"

    def finish(self, lane, lc, st, info, state):
        if self.dry_run:
            self.plan.append({"would": "harvest", "lane": lane, "issue": st.get("issue"), "status": info.get("status")})
            return False
        n, run = st["issue"], st["run"]
        self.quota_lane_guard(lane, lc, info)
        has_commit = info.get("branch") and info["branch"] != "None" and info.get("commit")
        if st.get("review_pr"):
            if has_commit:
                try:
                    frc, _, ferr = self.sh(self.fetch_lane_branch(lane, lc, info), timeout=40)
                except subprocess.TimeoutExpired:
                    frc, ferr = 124, "timeout"
                if frc:
                    self.log(f"{lane} review PR#{st['review_pr']} fetch failed ({(ferr or '').strip()[-90:]}); retry next tick", brain=False)
                    return False
            return self.finish_review(lane, lc, st, info, state)
        self.heartbeat()
        L = self.L
        if has_commit:
            b = self.cfg["pr_branch_template"].format(lane=lane, n=n, run=run)
            fetch = self.fetch_lane_branch(lane, lc, info)
            try:
                rc, _, err = self.sh(fetch, timeout=40)
            except subprocess.TimeoutExpired:
                st["fetch_fails"] = int(st.get("fetch_fails") or 0) + 1
                self.log(f"{lane} #{n} fetch timeout (fail #{st['fetch_fails']}); retry next tick", brain=False)
                return False
            if rc and re.search(self.TRANSIENT, err or "", re.I):
                st["fetch_fails"] = int(st.get("fetch_fails") or 0) + 1
                self.log(f"{lane} #{n} fetch transient fail #{st['fetch_fails']} ({(err or '').strip()[-90:]})", brain=False)
                try:
                    rc, _, err = self.sh(fetch, timeout=40)
                except subprocess.TimeoutExpired:
                    st["fetch_fails"] = int(st.get("fetch_fails") or 0) + 1
                    self.log(f"{lane} #{n} fetch timeout on retry (fail #{st['fetch_fails']}); next tick", brain=False)
                    return False
            if rc:
                st["fetch_fails"] = int(st.get("fetch_fails") or 0) + 1
                if st["fetch_fails"] >= 3 and re.search(self.GIVEUP, err or "", re.I):
                    self.relabel(n, L["failed"], [L["in_progress"], L["ready"]])
                    self.gh_write(["issue", "comment", str(n), "--body",
                                   f"Lane {lane} harvest gave up after {st['fetch_fails']} fetch failures "
                                   f"(last: `{(err or '').strip()[-160:]}`). Labelled `{L['failed']}`; re-add `{L['ready']}` to retry."])
                    self.mark_done(state, n)
                    self.log(f"{lane} #{n} run {run}: fetch failed {st['fetch_fails']}x → {L['failed']}")
                    return True
                self.log(f"{lane} #{n} run {run}: fetch failed: {(err or '').strip()[-200:]}")
                return False
            st["fetch_fails"] = 0
            if st.get("rebase_pr"):
                pr, ob = st["rebase_pr"], st["rebase_branch"]
                anc = self.git("merge-base", "--is-ancestor", st["base"], f"refs/fleet/{lane}")[0]
                df = self.git("diff", "--stat", st["base"], f"refs/fleet/{lane}")[1]
                if anc != 0 or not df.strip():
                    # nothing usable to push: the PR is still conflicting, so do not report it as rebased
                    why = (f"not based on {self.cfg['base_branch']} {st['base'][:8]}" if anc != 0
                           else f"empty diff vs {self.cfg['base_branch']}")
                    self.relabel(n, L["failed"], [L["in_progress"], L["ready"]])
                    self.gh_write(["issue", "comment", str(n), "--body",
                                   f"Lane {lane} rebase of PR #{pr} (run `{run}`) produced nothing to push ({why}). "
                                   f"Labelled `{L['failed']}`; re-add `{L['ready']}` to retry."])
                    self.log(f"{lane} rebase #{n} PR#{pr}: {why}; not pushing → {L['failed']}")
                    self.mark_done(state, n)
                    return True
                else:
                    expect = st.get("rebase_head")
                    if not expect:      # never force blindly: lease against the head as it is now
                        ls = self.sh(["git", "ls-remote", f"https://github.com/{self.cfg['repo']}.git", f"refs/heads/{ob}"], timeout=30)
                        expect = ls[1].split()[0] if not ls[0] and ls[1].split() else ""
                        if not expect:
                            self.log(f"{lane} rebase #{n} PR#{pr}: cannot read head of {ob}; retry next tick", brain=False)
                            return False
                        st["rebase_head"] = expect
                    lease = f"--force-with-lease=refs/heads/{ob}:{expect}"
                    try:
                        rc, _, err = self.git("push", "-q", lease, "origin", f"refs/fleet/{lane}:refs/heads/{ob}", timeout=60)
                    except subprocess.TimeoutExpired:
                        self.log(f"{lane} rebase push timeout; retry next tick", brain=False)
                        return False
                    if rc and st.get("rebase_head") and ("stale info" in (err or "") or "rejected" in (err or "")):
                        # someone pushed a newer head (manual fix) while the lane ran: keep it, don't clobber
                        self.relabel(n, L["pr"], [L["in_progress"], L["ready"]])
                        self.log(f"{lane} rebase #{n} PR#{pr}: PR head moved during the run (not {st['rebase_head'][:8]}); "
                                 "kept the newer head, not force-pushing")
                        self.mark_done(state, n)
                        return True
                    if rc:
                        self.log(f"{lane} rebase #{n} PR#{pr} push failed: {(err or '').strip()[-160:]}")
                        return False
                    self.gh_write(["pr", "comment", str(pr), "--body",
                             f"Rebased onto {self.cfg['base_branch']} by lane **{lane}** ({info.get('backend')}/{info.get('model')}), "
                             f"run `{run}`, lane status `{info.get('status')}`, verify rc `{info.get('verify_rc')}`. "
                             f"Force-pushed to `{ob}`; it is merged after a fresh lane review and green CI."])
                self.relabel(n, L["pr"], [L["in_progress"], L["ready"]])
                self.log(f"{lane} rebased #{n} → PR #{pr} ({ob}, run {run}, {info.get('status')})")
                self.mark_done(state, n)
                return True
            try:
                rc, _, err = self.git("push", "-q", "origin", f"refs/fleet/{lane}:refs/heads/{b}", timeout=60)
            except subprocess.TimeoutExpired:
                self.log(f"{lane} #{n} origin push timeout; retry next tick", brain=False)
                return False
            if rc:
                sst, labs = self.issue_labels(n)
                if L["pr"] in labs or sst == "CLOSED":
                    self.log(f"{lane} #{n} push failed after PR already exists/issue closed ({(err or '').strip()[-120:] or 'empty err'}); clearing lane")
                    self.mark_done(state, n)
                    return True
                ls = self.sh(["git", "ls-remote", f"https://github.com/{self.cfg['repo']}.git", f"refs/heads/{b}"], timeout=30)[1]
                if not ls.strip():
                    self.log(f"{lane} #{n} run {run}: push failed: {(err or '').strip()[-200:]}")
                    return False
                self.log(f"{lane} #{n} push rc={rc} but branch on origin; continuing to PR", brain=False)
            _, labs = self.issue_labels(n)
            if L["pr"] in labs:
                self.gh(["issue", "comment", str(n), "--body", f"Lane {lane} also finished this issue (run `{run}`, duplicate "
                         f"dispatch); branch `{b}` pushed for comparison, no second PR opened."])
                self.log(f"{lane} finished #{n} (run {run}) duplicate → branch {b}, no PR")
                self.mark_done(state, n)
                return True
            thin = self.pr_diff_too_thin(st["base"], f"refs/fleet/{lane}")
            if thin:
                mn = (self.cfg.get("thin_pr") or {}).get("min_effective_diff")
                self.relabel(n, L["failed"], [L["in_progress"], L["ready"]])
                self.gh(["issue", "comment", str(n), "--body",
                         f"Lane {lane} run `{run}` produced a thin diff (`{thin}`): effective code <{mn} lines or mostly docs. "
                         f"Labelled `{L['failed']}`; PR not opened."])
                self.mark_done(state, n)
                self.log(f"{lane} #{n} thin PR blocked ({thin}) → {L['failed']}")
                return True
            subj = self.git("log", "-1", "--format=%s", f"refs/fleet/{lane}")[1]
            body = (f"Closes #{n}\n\nAutodispatched to lane **{lane}** on **{lc['host']}** ({info.get('backend')}/{info.get('model')}), "
                    f"run `{run}`, base `{st['base'][:8]}`, lane status `{info.get('status')}`, host verify rc `{info.get('verify_rc')}`."
                    f"\n\nExecutor summary: {info.get('summary')}")
            title = self.upstream_title(subj.strip() or f"#{n}", st["base"], f"refs/fleet/{lane}")[:200]
            rc, out, err = self.gh(["pr", "create", "--base", self.cfg["base_branch"], "--head", b, "--title", title, "--body", body])
            url = (out.strip().splitlines() or [""])[-1] if not rc else f"(pr create failed: {err.strip()[-160:]})"
            self.relabel(n, L["pr"], [L["in_progress"], L["ready"]])
            self.log(f"{lane} finished #{n} (run {run}, {info.get('status')}) → {url}")
            self.mark_done(state, n)
            self.quota_backend_guard(lane, lc, info)
        elif run in self.watchdog_requeue():
            why = self.watchdog_requeue().get(run) or "watchdog"
            self.relabel(n, L["ready"], [L["in_progress"], L["failed"]])
            self.gh(["issue", "comment", str(n), "--body", f"Lane {lane} run `{run}` was aborted by the liveness watchdog ({why}). "
                     f"Requeued as `{L['ready']}`."])
            self.mark_done(state, n)
            self.log(f"{lane} #{n} run {run} watchdog-aborted ({why}) → requeued {L['ready']}")
        else:
            error_class = info.get("error_class") or ""
            requeue_classes = (self.cfg.get("quota") or {}).get("requeue_classes", ["rate_limit", "quota"])
            if error_class in requeue_classes:
                self.relabel(n, L["ready"], [L["in_progress"], L["failed"]])
                self.gh(["issue", "comment", str(n), "--body",
                         f"Lane {lane} ({lc['host']}, run `{run}`) ended `{info.get('status')}` without a commit "
                         f"because of backend quota/rate limiting (`{error_class}`), not a card failure. "
                         f"Requeued as `{L['ready']}`.\n\nExecutor summary: {info.get('summary')}"])
            else:
                self.relabel(n, L["failed"], [L["in_progress"], L["ready"]])
                self.gh(["issue", "comment", str(n), "--body",
                         f"Lane {lane} ({lc['host']}, run `{run}`) ended `{info.get('status')}` without a commit "
                         f"(error_class `{error_class}`).\n\nExecutor summary: {info.get('summary')}\n\n"
                         f"Label `{L['failed']}`; re-add `{L['ready']}` to retry."])
            self.mark_done(state, n)
            label = L["ready"] if error_class in requeue_classes else L["failed"]
            self.log(f"{lane} ended #{n} without a commit (run {run}, {info.get('status')}/{error_class}) → labelled {label}")
            self.quota_backend_guard(lane, lc, info)
        return True

    def watchdog_requeue(self):
        f = self.cfg.get("watchdog_requeue_file")
        return load_json(os.path.expanduser(f), {}) if f else {}

    # ------------------------------------------------------------ reviews
    def load_reviews(self):
        return load_json(self.path("reviews.json"), {})

    def put_review(self, pr, r):
        """Read-modify-write ONE entry under a lock so parallel harvests never drop each other's verdicts."""
        if self.dry_run:
            return
        with self.rev_lock:
            d = self.load_reviews()
            d[str(pr)] = r
            write_json(self.path("reviews.json"), d)

    def _checks(self, pr):
        ig = self.R.get("ignore_checks_re") or ""
        red_file = self.R.get("ignored_checks_red_file")
        count_ignored = bool(red_file and os.path.exists(self.path(red_file))
                             and re.search(self.R.get("ignored_checks_red_title_re") or "$^", pr.get("title") or "", re.I))
        out = []
        for c in pr.get("statusCheckRollup") or []:
            if c.get("context") == self.R["status_context"]:
                continue
            if ig and re.search(ig, c.get("name") or "", re.I):
                # ignored checks are slow to schedule and never gate, except a concluded failure in count_ignored mode
                if not (count_ignored and (c.get("conclusion") or c.get("state")) in ("FAILURE", "ERROR", "TIMED_OUT")):
                    continue
            out.append(c)
        return out

    def green(self, pr):
        cs = [(c.get("conclusion") or c.get("state") or "PENDING") for c in self._checks(pr)]
        return bool(cs) and all(c in ("SUCCESS", "SKIPPED", "NEUTRAL") for c in cs)

    def ci_red(self, pr):
        return any((c.get("conclusion") or c.get("state")) in ("FAILURE", "ERROR", "TIMED_OUT") for c in self._checks(pr))

    def author_lane(self, branch):
        m = re.match(self.R["author_lane_re"], branch or "")
        return m.group(1) if m else None

    def review_max(self):
        _, factor = self.burn_state()
        limits = {"review": int(self.R["max"]),
                  "max_review": max(int(self.R["max"]), int(self.R["burn_max"]))}
        cfg = self.cfg.get("burn") or {}
        if (factor > 1 and limits["review"] == 0 and cfg.get("window") is None
                and not any(key in cfg for key in ("burn_before_reset_h", "factor"))):
            # Legacy markers could enable burn review capacity even with normal reviews off.
            limits["review"] = 1
        return dispatch_module("burn").apply(limits, factor)["review"]

    def review_candidates(self, lanes, state, q):
        """Open fleet PRs to act on: conflicting → rebase task (checked BEFORE the already-reviewed skip, so an approved
        PR that went DIRTY still gets rebased); red CI → fix task (burn mode); green+mergeable+unreviewed head → review."""
        rc, out, err = self.gh(["pr", "list", "--state", "open", "--limit", "200", "--json",
                                "number,title,headRefName,headRefOid,isDraft,mergeable,statusCheckRollup,labels,body"], timeout=90)
        if rc:
            self.log(f"review: pr list failed: {(err or '')[-120:]}", brain=False)
            return []
        revs = self.load_reviews()
        busy_pr = {(s or {}).get("review_pr") for s in state.values() if isinstance(s, dict)}
        busy_pr |= {(s or {}).get("rebase_pr") for s in state.values() if isinstance(s, dict)}
        pending = {i["n"] for i in q} | {s.get("issue") for k, s in state.items()
                                          if not k.startswith("_") and isinstance(s, dict) and s.get("issue") is not None}
        L, fix = self.L, self.R["fix_ref_prefix"]
        out_l = []
        for p in sorted(json.loads(out), key=lambda p: p["number"]):
            if p["isDraft"] or not re.match(self.R["head_ref_re"], p["headRefName"]):
                continue
            labs = {l["name"] for l in p["labels"]}
            r = revs.get(str(p["number"])) or {}
            stale = sorted(labs & {L["reviewed"], L["needs_changes"]})
            if stale and r.get("sha") != p["headRefOid"]:
                args = ["pr", "edit", str(p["number"])]
                for lab in stale:                 # only labels that are present: gh fails on absent ones
                    args += ["--remove-label", lab]
                if not self.gh_write(args)[0]:
                    self.log(f"review: PR #{p['number']} head moved since review; removed {', '.join(stale)}", brain=False)
            if L["needs_human"] in labs or p["number"] in busy_pr:
                continue
            m = self.closes_re.search(p["body"] or "")
            p["issue"] = int(m.group(1)) if m else None
            if p["issue"] in pending:
                continue
            if (p["mergeable"] == "CONFLICTING" and p["issue"] and p["headRefName"].startswith(fix)
                    and r.get("rebase_sha") != p["headRefOid"]):
                self.queue_fix(p, revs, "rebase")
                continue
            if r.get("sha") == p["headRefOid"]:
                continue
            if ((self.burn_state()[1] > 1 or not self.R.get("cifix_needs_burn", True)) and p["mergeable"] == "MERGEABLE"
                    and not self.green(p) and p["issue"] and p["headRefName"].startswith(fix)
                    and r.get("cifix_sha") != p["headRefOid"] and self.ci_red(p)):
                self.queue_fix(p, revs, "cifix")
                continue
            if p["mergeable"] != "MERGEABLE" or not self.green(p):
                continue
            out_l.append(p)
        return out_l

    def section_strip(self, body, title):
        return re.sub(r"\n*## " + re.escape(title) + r".*?(?=\n## |\Z)", "", body or "", flags=re.S)

    def with_rebase_line(self, body, pr, branch):
        if self.rebase_of(body):
            return body
        return self.cfg["templates"]["rebase_line"].format(pr=pr, branch=branch) + "\n\n" + body

    def queue_fix(self, p, revs, kind):
        """Requeue the PR's issue as a rebase (conflict) or CI-fix task on the PR head, once per head sha."""
        pr, n = p["number"], p["issue"]
        rc, iv, _ = self.gh(["issue", "view", str(n), "--json", "body,state"])
        try:
            j = json.loads(iv)
        except Exception:
            return
        if j["state"] != "OPEN":
            return
        T = self.cfg["templates"]
        title = T[f"{kind}_section_title"]
        bad = ""
        if kind == "cifix":
            bad = ", ".join(sorted({c.get("name") or "?" for c in self._checks(p)
                                    if (c.get("conclusion") or c.get("state")) in ("FAILURE", "ERROR", "TIMED_OUT")}))[:400]
        ib = self.with_rebase_line(self.section_strip(j["body"], title), pr, p["headRefName"])
        ib += f"\n\n## {title}\n\n" + T[f"{kind}_section"].format(pr=pr, sha10=p["headRefOid"][:10], branch=p["headRefName"], checks=bad) + "\n"
        L = self.L
        self.gh_write(["issue", "edit", str(n), "--body", ib, "--add-label", L["ready"], "--remove-label", L["pr"],
                       "--remove-label", L["failed"], "--remove-label", L["in_progress"]])
        r = revs.get(str(pr)) or {}
        r["rebase_sha" if kind == "rebase" else "cifix_sha"] = p["headRefOid"]
        revs[str(pr)] = r
        self.put_review(pr, r)
        if kind == "rebase":
            self.log(f"review: PR #{pr} CONFLICTING → rebase task queued on #{n}")
        else:
            self.log(f"review: PR #{pr} CI red ({bad[:80]}) → fix task queued on #{n}")

    def pick_reviewer(self, idle, lanes, author):
        ab = (lanes.get(author) or {}).get("backend")
        c = [L for L in idle if L != author]
        if not c:
            return None
        backend, factor = self.burn_state()
        def priority(lane):
            return (factor > 1 and lanes[lane]["backend"] != backend, lanes[lane]["backend"] == ab)
        best_priority = min(map(priority, c))
        available = [lane for lane in c if priority(lane) == best_priority]
        picker = self.lru_picker()
        oldest = picker.pick(available)
        if factor > 1:
            return oldest
        history = picker.state()["lanes"]
        tied = [lane for lane in available if history.get(lane) == history.get(oldest)]
        return min(tied, key=lambda lane: (self.R["pref"].get(lanes[lane]["backend"], 9), lane))

    def review_item(self, p):
        pr = p["number"]
        diff = self.gh(["pr", "diff", str(pr)], timeout=60)[1]
        lim = int(self.R["diff_limit"])
        if len(diff) > lim:
            diff = diff[:lim] + f"\n... [diff truncated at {lim} chars; read the files in the worktree if needed]\n"
        issue = ""
        if p.get("issue"):
            iv = self.gh(["issue", "view", str(p["issue"]), "--json", "title,body"])[1]
            try:
                j = json.loads(iv)
                issue = f"### Issue #{p['issue']}: {j['title']}\n\n{(j['body'] or '')[:6000]}"
            except Exception:
                pass
        f = f"{self.R['verdict_dir']}/PR-{pr}.md"
        body = self.cfg["templates"]["review_task"].format(pr=pr, branch=p["headRefName"], author=self.author_lane(p["headRefName"]) or "?",
                                                           title=p["title"], sha=p["headRefOid"], file=f, issue=issue, diff=diff)
        return {"n": pr, "title": f"Review PR #{pr}: {p['title']}"[:110], "body": body, "review": True}

    def finish_review(self, lane, lc, st, info, state):
        pr, sha = st["review_pr"], st["review_sha"]
        r = self.load_reviews().get(str(pr)) or {}
        txt = ""
        if info.get("branch") and info["branch"] != "None" and info.get("commit"):
            txt = self.git("show", f"refs/fleet/{lane}:{self.R['verdict_dir']}/PR-{pr}.md")[1]
        v, _ = status.parse_verdict(txt)
        if not v:
            error_class = info.get("error_class") or ""
            if error_class in {"rate_limit", "quota", "vm_paused"}:
                r["quota_backend"] = info.get("backend") or lc.get("backend")
                self.log(f"review PR #{pr} by {lane}: no verdict file ({info.get('status')}/{error_class}); quota (not counted), will retry")
            else:
                r["fails"] = int(r.get("fails") or 0) + 1
                self.log(f"review PR #{pr} by {lane}: no verdict file ({info.get('status')}/{error_class}); fail #{r['fails']}, will retry")
            self.put_review(pr, r)
            return True
        rc, cur, _ = self.gh(["pr", "view", str(pr), "--json", "headRefOid,state,headRefName,body"])
        try:
            cj = json.loads(cur)
        except Exception:
            self.log(f"review PR #{pr}: pr view failed; retry next tick", brain=False)
            return False
        if cj.get("state") != "OPEN":
            self.log(f"review PR #{pr} by {lane}: PR {cj.get('state')}; verdict discarded")
            return True
        if status.head_changed(sha, cj.get("headRefOid")):
            self.log(f"review PR #{pr} by {lane}: PR {cj.get('state')} / head moved ({sha[:8]}→{str(cj.get('headRefOid'))[:8]}); verdict discarded")
            return True
        bk_backend = info.get("backend") or lc.get("backend") or "?"
        bk_model = info.get("model") or "?"
        bk = f"{bk_backend}/{bk_model}"
        who = f"{lane} ({bk})"
        L = self.L

        try:
            payload = status.build_status(
                v,
                sha,
                reviewer=lane,
                backend=bk_backend,
                model=bk_model,
                run_id=st.get("run") or "?",
                context=self.R.get("status_context") or status.DEFAULT_CONTEXT,
            )
        except ValueError as e:
            self.log(f"review PR #{pr}: build status failed: {e}", brain=False)
            return False

        def _runner(cmd, **kwargs):
            try:
                if cmd[:3] == ["gh", "auth", "status"]:
                    if self.dry_run:
                        return subprocess.CompletedProcess(cmd, 0, "", "")
                    res = self.sh(cmd, timeout=30)
                else:
                    res = self.write(cmd, timeout=30, what="review-status")
            except subprocess.TimeoutExpired:
                res = (124, "", "timeout")
            if isinstance(res, tuple):
                return subprocess.CompletedProcess(cmd, res[0], res[1] if len(res) > 1 else "", res[2] if len(res) > 2 else "")
            return res

        post_res = status.post_status(self.cfg.get("repo"), payload, runner=_runner)
        if "error" in post_res:
            self.log(f"review PR #{pr} by {lane}: post status failed ({post_res['error']}); retry next tick", brain=False)
            return False
        if "skipped" in post_res:
            self.log(f"review PR #{pr}: commit status skipped ({post_res['skipped']})", brain=False)

        self.gh(["pr", "comment", str(pr), "--body",
                 f"**Lane review — {'✅ APPROVE' if v == 'APPROVE' else '❌ CHANGES REQUESTED'}** by {who}, run `{st['run']}`, "
                 f"head `{sha[:10]}`\n\n{txt.strip()[:60000]}\n\n<!-- lane-review pr={pr} sha={sha} verdict={v} -->"])
        r.update({"sha": sha, "verdict": v, "lane": lane, "at": time.strftime("%F %T"), "fails": 0})
        if v == "APPROVE":
            self.gh(["pr", "edit", str(pr), "--add-label", L["reviewed"], "--remove-label", L["needs_changes"]])
        else:
            r["rounds"] = int(r.get("rounds") or 0) + 1
            self.gh(["pr", "edit", str(pr), "--add-label", L["needs_changes"], "--remove-label", L["reviewed"]])
            m2 = self.closes_re.search(cj.get("body") or "")
            n = int(m2.group(1)) if m2 else None
            if r["rounds"] > int(self.R["rounds"]) or not n:
                self.gh(["pr", "edit", str(pr), "--add-label", L["needs_human"]])
                self.log(f"review PR #{pr}: CHANGES round {r['rounds']} (issue {n}) → {L['needs_human']}")
            else:
                iv = self.gh(["issue", "view", str(n), "--json", "body"])[1]
                ib = (json.loads(iv)["body"] or "") if iv else ""
                T = self.cfg["templates"]
                ib = self.with_rebase_line(self.section_strip(ib, T["changes_section_title"]), pr, cj["headRefName"])
                ib += (f"\n\n## {T['changes_section_title']}\n\n" +
                       T["changes_section"].format(round=r["rounds"], who=who, pr=pr, sha10=sha[:10], branch=cj["headRefName"],
                                                   notes=txt.strip()[:8000]) + "\n")
                self.gh(["issue", "edit", str(n), "--body", ib, "--add-label", L["ready"], "--remove-label", L["pr"], "--remove-label", L["failed"]])
                self.log(f"review PR #{pr}: CHANGES → fix task queued on #{n} (round {r['rounds']})")
        self.put_review(pr, r)
        self.log(f"reviewed PR #{pr} by {who}: {v}")
        return True

    def idle_lanes(self, lanes, res, state):
        return [L for L, lc in lanes.items() if "error" not in res[L] and self.host_ok(lc["host"]) and not state.get(L)
                and res[L].get("lane") == "idle" and not self.paused_lane(L, lc) and not lc.get("drain")]

    def dispatch_reviews(self, lanes, res, state, q):
        if not self.R.get("enabled", True) or os.path.exists(self.path("NO-LANE-REVIEW")):
            return
        inflight = sum(1 for s in state.values() if isinstance(s, dict) and s.get("review_pr"))
        idle = self.idle_lanes(lanes, res, state)
        if inflight >= self.review_max() or not idle:
            return
        revs = self.load_reviews()
        for p in self.review_candidates(lanes, state, q):
            if inflight >= self.review_max() or not idle:
                break
            if int((revs.get(str(p["number"])) or {}).get("fails") or 0) >= int(self.R["max_fails"]):
                continue
            L = self.pick_reviewer(idle, lanes, self.author_lane(p["headRefName"]))
            previous_backend = (revs.get(str(p["number"])) or {}).get("quota_backend")
            alternatives = [lane for lane in idle if lanes[lane].get("backend") != previous_backend]
            if previous_backend and alternatives:
                L = self.pick_reviewer(alternatives, lanes, self.author_lane(p["headRefName"]))
            if not L:
                continue
            host_started = self.clock()
            try:
                base = self.main_sha()
                run, ver = self.dispatch(L, lanes[L], self.review_item(p), base)
            except Exception as e:
                self.host_fail(lanes[L]["host"], f"review dispatch PR#{p['number']}: {e}", host_started, 120)
                idle.remove(L)
                continue
            idle.remove(L)
            inflight += 1
            if self.dry_run:
                continue
            state[L] = {"issue": f"PR{p['number']}", "run": run, "host": lanes[L]["host"], "base": base,
                        "at": time.strftime("%F %T"), "verify": ver, "review_pr": p["number"], "review_sha": p["headRefOid"]}
            self.save_state(state)
            self.lru_picker().touch(L)
            self.log(f"review dispatched PR #{p['number']} (head {p['headRefOid'][:8]}, author {self.author_lane(p['headRefName'])}) "
                     f"→ {L} ({lanes[L]['host']}/{lanes[L]['backend']}) run {run}")
            self.heartbeat()

    # ------------------------------------------------------------ tick
    def probe(self, lanes, state):
        probe_started = self.clock()
        live = [L for L in lanes if self.host_ok(lanes[L]["host"]) and not self.paused_lane(L, lanes[L])]
        res = {}
        ex = cf.ThreadPoolExecutor(max_workers=min(24, max(1, len(live))))
        try:
            futs, byhost = {}, {}
            for L in live:
                h = lanes[L]["host"]
                if h in self.local_hosts:
                    futs[ex.submit(lambda L=L: {L: self.remote_local(L, (state.get(L) or {}).get("run", ""))})] = [L]
                else:
                    byhost.setdefault(h, []).append(L)
            for h, Ls in byhost.items():
                futs[ex.submit(self.remote_multi, h, [(L, (state.get(L) or {}).get("run", "")) for L in Ls])] = Ls
            try:
                for fut in cf.as_completed(futs, timeout=int(self.cfg["status_tick_cap_s"])):
                    Ls = futs[fut]
                    try:
                        r = fut.result(timeout=0)
                        for L in Ls:
                            res[L] = r.get(L, {"error": "missing"})
                    except Exception as e:
                        for L in Ls:
                            res[L] = {"error": f"status exc: {e}"[:200]}
            except cf.TimeoutError:
                for fut, Ls in futs.items():
                    for L in Ls:
                        if L not in res:
                            fut.cancel()
                            res[L] = {"error": "ssh status timeout (tick cap)"}
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        for L in lanes:
            res.setdefault(L, {"error": "host in backoff", "skip": True})
        seen = set()
        for L in live:
            h = lanes[L]["host"]
            if h in seen:
                continue
            seen.add(h)
            errs = [res[x] for x in live if lanes[x]["host"] == h and "error" in res[x]]
            if errs:
                self.host_fail(h, errs[0]["error"], probe_started, int(self.cfg["status_tick_cap_s"]))
            else:
                self.host_good(h)
        return res

    def harvest(self, lanes, res, state):
        todo = []
        for L, r in res.items():
            st = state.get(L)
            if "error" in r or not st:
                continue
            todo.append((L, "finish", r["run"]) if isinstance(r.get("run"), dict) else (L, "reconcile", r))
        if not todo:
            return

        def one(item):
            L, kind, payload = item
            st = state.get(L)
            if not st:
                return L, False
            try:
                if kind == "finish":
                    return L, bool(self.finish(L, lanes[L], st, payload, state))
                if self.dry_run:
                    return L, False
                return L, bool(self.reconcile(L, lanes[L], st, payload, state))
            except Exception as e:
                self.log(f"{L} harvest exc: {e!r}", brain=False)
                return L, False

        ex = cf.ThreadPoolExecutor(max_workers=min(6, max(1, len(todo))))
        try:
            futs = {ex.submit(one, it): it[0] for it in todo}
            try:
                for fut in cf.as_completed(futs, timeout=int(self.cfg["harvest_cap_s"])):
                    self.heartbeat()
                    try:
                        L, ok = fut.result(timeout=0)
                    except Exception as e:
                        self.log(f"harvest fut exc: {e!r}", brain=False)
                        continue
                    if ok:
                        state.pop(L, None)
                        self.save_state(state)
            except cf.TimeoutError:
                for fut, L in futs.items():
                    if not fut.done():
                        fut.cancel()
                        self.log(f"{L} harvest tick-cap; leave for next tick", brain=False)
        finally:
            ex.shutdown(wait=False)

    def lane_order(self, lanes):
        backend, factor = self.burn_state()
        groups = (False, True) if factor > 1 else (False,)
        ordered = []
        picker = self.lru_picker()
        for other_backend in groups:
            remaining = [name for name, lane in lanes.items()
                         if (factor > 1 and lane.get("backend") != backend) == other_backend]
            while remaining:
                lane = picker.pick(remaining)
                ordered.append((lane, lanes[lane]))
                remaining.remove(lane)
        return ordered

    def assign_issues(self, lanes, res, state, q):
        base = None
        held = {}
        for L in lanes:
            for f in set((state.get(L) or {}).get("hot") or []) - self.hot_union:
                held[f] = held.get(f, 0) + 1
        for L, lc in self.lane_order(lanes):
            r = res[L]
            if ("error" in r or not self.host_ok(lc["host"]) or state.get(L) or r.get("lane") != "idle"
                    or self.paused_lane(L, lc) or lc.get("drain")):
                continue
            pick = None
            for c in sorted((i for i in q if i.get("deps_ok", True) and self.can_take(lc["backend"], i)),
                            key=lambda i: self.rank(lc["backend"], i)):
                if self.hot_blocked(self.hot_of(c["body"]), held):
                    continue
                ls = self.label_state(c["n"])
                if ls == "fresh":
                    pick = c
                    break
                q.remove(c)
                if ls == "unknown":       # read failed / budget spent: not evidence of staleness
                    continue
                self.log(f"skip #{c['n']}: stale search index (already claimed/done)", brain=False)
                self.note_stale_skip(c["n"], state)
            if not pick:
                continue
            if base is None:
                try:
                    base = self.main_sha()
                except Exception as e:
                    self.log(f"{e}; skip further dispatch this tick", brain=False)
                    break
            self.heartbeat()
            self.gh_write(["issue", "edit", str(pick["n"]), "--add-label", self.L["in_progress"]])
            host_started = self.clock()
            try:
                run, ver = self.dispatch(L, lc, pick, base)
            except Exception as e:
                self.gh_write(["issue", "edit", str(pick["n"]), "--remove-label", self.L["in_progress"]])
                self.host_fail(lc["host"], f"dispatch #{pick['n']}: {e}", host_started, 120)
                continue
            q.remove(pick)
            hot = self.hot_of(pick["body"])
            for f in hot:
                held[f] = held.get(f, 0) + 1
            if self.dry_run:
                continue
            state[L] = {"issue": pick["n"], "run": run, "host": lc["host"], "base": base, "at": time.strftime("%F %T"), "verify": ver}
            self.lru_picker().touch(L)
            rb = self.rebase_of(pick["body"])
            if hot:
                state[L]["hot"] = sorted(hot)
            if rb:
                state[L]["rebase_pr"], state[L]["rebase_branch"] = rb
                # PR head the lane starts from: harvest force-pushes with this lease so a fix pushed meanwhile survives
                try:
                    ls = self.sh(["git", "ls-remote", f"https://github.com/{self.cfg['repo']}.git", f"refs/heads/{rb[1]}"], timeout=30)[1]
                    if ls.split():
                        state[L]["rebase_head"] = ls.split()[0]
                except (subprocess.TimeoutExpired, OSError):
                    pass
            self.save_state(state)
            self.log(f"dispatched #{pick['n']} → {L} ({lc['host']}) run {run} base {base[:8]}")
            self.heartbeat()

    def replenish(self, need, lanes, q, blocked):
        rp = self.cfg.get("replenish") or {}
        if not rp.get("cmd"):
            return
        if any(os.path.exists(self.path(f)) for f in rp.get("skip_files") or []):
            return
        if self.dry_run:
            self.plan.append({"would": "replenish", "need": need})
            return
        try:
            rc, out, err = self.sh(list(rp["cmd"]) + [str(need)], timeout=int(rp.get("timeout_s", 240)))
            if out.strip():
                self.log(f"replenish: {out.strip().splitlines()[-1][:200]}")
        except Exception as e:
            self.log(f"replenish hook exc: {e!r}", brain=False)

    def tick(self, lanes, state):
        if os.path.exists(self.path("PAUSE")):
            return
        self.label_cache, self.label_reads = {}, 0
        os.makedirs(os.path.expanduser(self.cfg["ssh"]["control_dir"]), exist_ok=True)
        self.heartbeat()
        try:
            paused = sorted(p for p in os.listdir(self.D) if p.startswith("PAUSE"))
            if paused and self.clock() - self.last_warn > 300:
                self.log(f"pauses active: {', '.join(paused)}", brain=False)
                self.last_warn = self.clock()
        except Exception:
            pass
        state.setdefault("_done", [])
        cm = os.path.expanduser(self.cfg["ssh"]["control_dir"])
        for name in os.listdir(cm):
            try:
                if self.clock() - os.stat(os.path.join(cm, name)).st_mtime > 600:
                    os.unlink(os.path.join(cm, name))
            except Exception:
                pass
        res = self.probe(lanes, state)
        self.harvest(lanes, res, state)
        try:
            self.sweep_leaked_inprogress(state)
        except Exception as e:
            self.log(f"leak-sweep exc: {e!r}", brain=False)
        try:
            q = self.queue()
        except Exception as e:
            self.log(f"queue error: {e}", brain=False)
            return res
        for i in q:
            i["deps_ok"] = self.deps_met(i)
        blocked = sum(not i["deps_ok"] for i in q)
        try:
            self.dispatch_reviews(lanes, res, state, q)
        except Exception as e:
            self.log(f"review dispatch exc: {e!r}", brain=False)
        self.assign_issues(lanes, res, state, q)
        ready = len(q) - blocked
        rp = self.cfg.get("replenish") or {}
        short = {b: sum(c["backend"] == b for c in lanes.values()) - sum(1 for i in q if i.get("deps_ok", True) and self.can_take(b, i))
                 for b in {c["backend"] for c in lanes.values()}}
        starve = max(short.values(), default=0)
        low, target = int(rp.get("low", 20)), int(rp.get("target", 24))
        if ready < low or starve > 0:
            self.replenish(max(target - ready, starve, 1 if ready < low else 0), lanes, q, blocked)
        st = {L: ("busy #%s" % state[L]["issue"]) if state.get(L) else (res[L].get("error") or res[L].get("lane") or "?") for L in lanes}
        st.update({"_busy": sum(1 for L in lanes if state.get(L)), "_ready": ready, "_blocked": blocked,
                   "_at": time.strftime("%F %T"), "_engine": "openswarm-dispatch"})
        if not self.dry_run:
            write_json(self.path("lanes-status.json"), st)
        return res

    def serve(self):
        while True:
            try:
                self.tick(load_json(self.cfg["lanes_file"], {}), load_json(self.path("state.json"), {}))
            except Exception as e:
                self.log(f"tick error: {e!r}", brain=False)
            left = int(self.cfg["tick_s"])
            while left > 0:      # chunked sleep keeps the heartbeat fresh across host suspend
                self.heartbeat()
                step = min(5, left)
                time.sleep(step)
                left -= step


def classify_numstat(numstat, min_eff, doc_ext, code_ext, code_dirs):
    """Return a reason when a diff is mostly docs or has < min_eff effective code lines, else ''."""
    eff = docs = other = 0
    for line in (numstat or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d, p = parts[0], parts[1], parts[2]
        try:
            n = (0 if a == "-" else int(a)) + (0 if d == "-" else int(d))
        except ValueError:
            continue
        pl = p.lower()
        if pl.endswith(doc_ext) or "/docs/" in pl or pl.startswith("docs/"):
            docs += n
        elif pl.endswith(code_ext) or any(("/" + cd) in pl or pl.startswith(cd) for cd in code_dirs):
            eff += n
        else:
            other += n
    if docs + eff + other == 0:
        return "empty-diff"
    if eff < min_eff:
        return f"eff<{min_eff}:{eff} docs={docs} other={other}"
    if docs > 0 and eff < docs and eff < min_eff * 2:
        return f"mostly-docs:docs={docs} eff={eff}"
    return ""


# ---------------------------------------------------------------- CLI
def add_parser(sub):
    sp = sub.add_parser("dispatch", help="push-mode GitHub label dispatcher for resident lanes (reviews, rebase/CI-fix queue)")
    dsub = sp.add_subparsers(dest="dispatch_cmd", required=True)
    for name, hlp in (("serve", "run the resident dispatch loop"), ("tick", "run one tick"),
                      ("check-config", "print the merged config")):
        p = dsub.add_parser(name, help=hlp)
        p.add_argument("--config", required=True)
        if name == "tick":
            p.add_argument("--dry-run", action="store_true", help="decide only: no labels, assigns, pushes or state writes")
            p.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_dispatch)


def cmd_dispatch(args):
    cfg = load_config(args.config)
    if args.dispatch_cmd == "check-config":
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return 0
    if args.dispatch_cmd == "serve":
        Dispatcher(cfg).serve()
        return 0
    d = Dispatcher(cfg, dry_run=args.dry_run, out=sys.stderr if args.json else sys.stdout)
    d.tick(load_json(cfg["lanes_file"], {}), load_json(d.path("state.json"), {}))
    if args.json:
        print(json.dumps(d.plan, indent=1, ensure_ascii=False))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="openswarm-dispatch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add_parser(sub)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
