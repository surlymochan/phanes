#!/usr/bin/env python3
"""Tick-level contract check for the AD base-commit validation path (Refs #1810).

The autodispatcher validates the base commit in the target repo before it assigns a lane
(`git -C <repo> cat-file -e <base>^{commit}`). When the target repo lags behind upstream the
object is missing, and the dispatch used to fail with `fatal: Not a valid object name <base>^{commit}`
plus a 30s host backoff on every base switch.

The check drives the real `tick()` call chain (queue -> authoritative label read -> dispatch ->
state file -> host backoff) against a throw-away git sandbox and covers the two acceptance paths:

  * base object missing -> the AD fetches the target repo -> the base arrives -> the lane is
    dispatched, the `in-progress` label is kept and the host is NOT backed off;
  * base object still missing after the fetch -> the dispatch fails exactly as before, the
    `in-progress` label is dropped and the host is backed off (30s) like any transient failure.

It also pins the boundaries: a failing fetch aborts the dispatch (a later successful check must
never mask it), the target repo path comes from the lane configuration (not hardcoded) and
concurrent ticks do not deadlock on the fetch.

The whole path runs offline: the remote command is executed locally with `bash` against throw-away
git repositories (no network, no ssh, no fleet).
"""
from __future__ import annotations

import concurrent.futures as cf
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"
# Scratch space stays inside the worktree (the executor's TMPDIR is .swrm-tmp/).
SCRATCH = Path(os.environ.get("TMPDIR") or (ROOT / ".swrm-tmp"))
HOST = "ad-base-fetch-test"
LANE = "CX1"
ISSUE = 1810


def load_module(name="autodispatch_base_fetch"):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- git sandbox
def git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode:
        raise AssertionError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()[-200:]}")
    return proc


def init_repo(path, files):
    """Create a repo with one commit on `main`; returns the commit sha."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    git(path, "config", "user.email", "ad-check@example.invalid")
    git(path, "config", "user.name", "ad check")
    for name, content in files.items():
        (path / name).write_text(content)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "c1")
    return git(path, "rev-parse", "HEAD").stdout.strip()


def commit_file(repo, name, content):
    (repo / name).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c2")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def is_fetch(command):
    """True when the remote command is the git fetch of the base branch."""
    return " fetch -q " in command


class Sandbox:
    """Throw-away upstream + target repo, a fake `gh` and a fake remote shell."""

    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin"
        self.fleet = root / "fleet"
        self.assign_log = root / "assign.log"
        self.commands = []  # every remote command the AD ran (the fetch-on-miss contract)
        self.gh_calls = []
        self.fetch_mode = "ok"  # "ok" | "fail" | "fail_but_object_arrives"
        self.ls_remote_base = None  # pin the advertised base (a base the fetch cannot deliver)
        self.fetch_lock = threading.Lock()  # models git's own short-lived ref locks
        root.mkdir(parents=True, exist_ok=True)
        init_repo(self.origin, {"README.md": "c1\n"})
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.fleet)], check=True)
        # upstream moves on: the new base commit is not in the target repo yet
        self.base = commit_file(self.origin, "README.md", "c2\n")
        self.fleet_bin = root / "swrm-fleet"
        self.fleet_bin.write_text(f"#!/bin/sh\necho \"assigned $*\" >> {self.assign_log}\necho '{{\"id\": \"run-1\"}}'\n")
        self.fleet_bin.chmod(0o755)
        self.issues = {ISSUE: {"title": f"#{ISSUE} base fetch check", "body": "",
                                "labels": ["lane-ready"], "state": "OPEN"}}

    # --- the AD's fixtures ---------------------------------------------------------------
    def lanes(self, repo=None):
        """The lane configuration the AD reads; the target repo comes from it."""
        return {LANE: {"host": HOST, "backend": "pi", "repo": str(repo or self.fleet),
                       "fleet": str(self.fleet_bin)}}

    def item(self, n=ISSUE):
        return {"n": n, "title": f"base fetch check {n}", "body": "", "prio": 2, "backend": None}

    def assigned(self):
        return self.assign_log.exists() and self.assign_log.read_text().strip()

    def labels(self, n=ISSUE):
        return list(self.issues[n]["labels"])

    # --- the AD's `gh` -------------------------------------------------------------------
    def gh(self, args, timeout=60):
        """The AD's `gh`: a local issue table, never the network."""
        self.gh_calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            wanted = args[args.index("--label") + 1] if "--label" in args else None
            rows = []
            for number, issue in sorted(self.issues.items()):
                if wanted and wanted not in issue["labels"]:
                    continue
                rows.append({"number": number, "title": issue["title"], "body": issue["body"],
                             "labels": [{"name": name} for name in issue["labels"]]})
            return 0, json.dumps(rows), ""
        if args[:2] == ["issue", "view"]:
            issue = self.issues.get(int(args[2]))
            if issue is None:
                return 1, "", "gh: could not read the issue"
            fields = args[args.index("--json") + 1].split(",") if "--json" in args else []
            payload = {"state": issue["state"], "title": issue["title"], "body": issue["body"]}
            if "labels" in fields:
                payload["labels"] = [{"name": name} for name in issue["labels"]]
            return 0, json.dumps(payload), ""
        if args[:2] == ["issue", "edit"]:
            issue = self.issues[int(args[2])]
            for flag in ("--add-label", "--remove-label"):
                if flag not in args:
                    continue
                label = args[args.index(flag) + 1]
                if flag == "--add-label" and label not in issue["labels"]:
                    issue["labels"].append(label)
                if flag == "--remove-label" and label in issue["labels"]:
                    issue["labels"].remove(label)
            return 0, "", ""
        raise AssertionError(f"unexpected gh call: {args}")

    # --- the AD's `sh` -------------------------------------------------------------------
    def sh(self, cmd, timeout=120, inp=None):
        """The AD's `sh`: the ssh argv is executed locally with bash instead of over ssh."""
        if isinstance(cmd, str):
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, input=inp)
            return proc.returncode, proc.stdout, proc.stderr
        argv = list(cmd)
        if argv[:2] == ["git", "ls-remote"]:
            if self.ls_remote_base:
                return 0, f"{self.ls_remote_base}\trefs/heads/main\n", ""
        if argv[:1] == ["ssh"]:
            remote = argv[-1].replace("/tmp/", f"{self.root}/")  # keep every scratch file in the sandbox
            self.commands.append(remote)
            if is_fetch(remote):
                return self.fetch(remote, timeout)
            proc = subprocess.run(["bash", "-c", remote], capture_output=True, text=True, timeout=timeout, input=inp)
            return proc.returncode, proc.stdout, proc.stderr
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=inp)
        return proc.returncode, proc.stdout, proc.stderr

    def fetch(self, remote, timeout):
        """Run the base fetch; `fetch_mode` drives the failure simulations.

        "fail" reports a network failure without touching the repo; "fail_but_object_arrives"
        reports the same failure although the object did arrive (a concurrent tick fetched it),
        which is exactly the case a later successful check must not be allowed to mask."""
        with self.fetch_lock:
            if self.fetch_mode == "fail":
                return 1, "", "fatal: unable to access 'https://github.com/surlymochan/phanes.git/': Could not resolve host"
            proc = subprocess.run(["bash", "-c", remote], capture_output=True, text=True, timeout=timeout)
            if self.fetch_mode == "fail_but_object_arrives":
                return 1, "", "fatal: unable to access 'https://github.com/surlymochan/phanes.git/': Could not resolve host"
            return proc.returncode, proc.stdout, proc.stderr


def prepare(module, sandbox: Sandbox, state_dir="ad-state"):
    """Point the AD at the sandbox: no real state dir, no real log, no gh, no fleet."""
    module.D = str(sandbox.root / state_dir)
    os.makedirs(module.D, exist_ok=True)
    module.BRAIN_LOG = str(sandbox.root / f"{state_dir}-brain-log.md")
    module.sh = sandbox.sh
    module.gh = sandbox.gh
    module.remote = lambda L, cfg, run="": {"lane": "idle"}
    module.replenish = lambda k, state: []
    module.LOW = 0
    module.DISPATCH_BASE_FETCH_DELAY = 0.2
    module.BASE_REPO_URL = str(sandbox.origin)
    module.BASE_BRANCH = "main"
    module.BACKOFF.clear()


def tick(module, sandbox: Sandbox, lanes=None, state=None):
    """Run one real tick and return (state, log text)."""
    state = {} if state is None else state
    module.tick(lanes if lanes is not None else sandbox.lanes(), state, 0)
    log = (Path(module.D) / "autodispatch.log").read_text() if (Path(module.D) / "autodispatch.log").exists() else ""
    return state, log


def saved_state(module):
    path = Path(module.D) / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def saved_status(module):
    path = Path(module.D) / "lanes-status.json"
    return json.loads(path.read_text()) if path.exists() else {}


# --------------------------------------------------------------------------- the two paths
def check_tick_miss_then_fetch(module, sandbox: Sandbox):
    """base object missing -> fetch -> the base arrives -> dispatched, and no backoff."""
    prepare(module, sandbox)
    state, log = tick(module, sandbox)
    assert sandbox.assigned(), "the lane must be assigned once the base arrived"
    assert not module.BACKOFF, f"a recovered base must not back the host off: {module.BACKOFF}"
    assert module.host_ok(HOST)
    labels = sandbox.labels()
    assert "in-progress" in labels, labels
    assert "lane-ready" in labels, labels
    saved = saved_state(module)
    assert saved.get(LANE, {}).get("issue") == ISSUE, saved
    assert saved.get(LANE, {}).get("base") == sandbox.base, saved
    status = saved_status(module)
    assert status.get("_busy") == 1, status
    assert status.get("_ready") == 0, status  # the dispatched issue left the queue
    assert "Not a valid object name" not in log, log
    assert "backing off" not in log, log
    assert f"dispatched #{ISSUE}" in log, log
    have = git(sandbox.fleet, "cat-file", "-e", f"{sandbox.base}^{{commit}}", check=False)
    assert have.returncode == 0, "the fetched base must be in the target repo"
    # the fetch is triggered by the missing base, not run before every check
    first_check = next(command for command in sandbox.commands if "cat-file -e" in command)
    assert not is_fetch(first_check), f"the check must run before the fetch: {first_check}"
    assert any(is_fetch(command) for command in sandbox.commands), sandbox.commands
    print("  tick: base missing -> fetch -> base present: dispatched, in-progress kept, no backoff: OK")


def check_tick_miss_after_fetch(module, sandbox: Sandbox):
    """the fetch ran but the base is still missing -> the old error plus the old 30s backoff."""
    prepare(module, sandbox)
    # the upstream advertises a base the target repo cannot obtain (rewritten main, shallow clone)
    sandbox.ls_remote_base = init_repo(sandbox.root / "stray", {"README.md": "stray\n"})
    state, log = tick(module, sandbox)
    assert not sandbox.assigned(), "the lane must not be assigned without the base commit"
    assert not module.host_ok(HOST), "a missing base must back the host off"
    wait = module.BACKOFF[HOST]["until"] - time.time()
    assert 25 <= wait <= 31, f"the backoff must stay 30s, got {wait:.1f}s"
    labels = sandbox.labels()
    assert "in-progress" not in labels, labels
    assert "lane-ready" in labels, labels
    assert LANE not in saved_state(module), saved_state(module)
    status = saved_status(module)
    assert status.get("_busy") == 0, status
    assert status.get("_ready") == 1, status  # the issue stays dispatchable for the next tick
    assert "Not a valid object name" in log, log
    assert "backing off 30s" in log, log
    print("  tick: base missing after the fetch: dispatch failed, in-progress dropped, host backed off 30s: OK")


def check_tick_fetch_failure(module, sandbox: Sandbox):
    """a failing fetch aborts the dispatch: the lane is not assigned and the host backs off."""
    prepare(module, sandbox)
    sandbox.fetch_mode = "fail"
    state, log = tick(module, sandbox)
    assert not sandbox.assigned(), "the lane must not be assigned when the fetch failed"
    assert not module.host_ok(HOST), "a failing fetch must back the host off"
    assert "in-progress" not in sandbox.labels(), sandbox.labels()
    assert "base fetch failed" in log, log
    print("  tick: fetch failed (network): reported, no assign, host backed off: OK")


def check_tick_fetch_failure_not_masked(module, sandbox: Sandbox):
    """a failing fetch is not masked by a later successful check (concurrent fetch in between)."""
    prepare(module, sandbox)
    # this fetch fails, but the object arrives anyway (a concurrent tick fetched it)
    sandbox.fetch_mode = "fail_but_object_arrives"
    state, log = tick(module, sandbox)
    have = git(sandbox.fleet, "cat-file", "-e", f"{sandbox.base}^{{commit}}", check=False)
    assert have.returncode == 0, "the object must have arrived (the concurrent fetch worked)"
    assert not sandbox.assigned(), "a failing fetch must not let the dispatch pass"
    assert not module.host_ok(HOST), "a failing fetch must back the host off"
    assert "in-progress" not in sandbox.labels(), sandbox.labels()
    assert "base fetch failed" in log, log
    print("  tick: fetch failed while the base arrived: dispatch aborted, host backed off: OK")


def check_tick_base_present(module, sandbox: Sandbox):
    """the base is already in the target repo: the check passes without any fetch."""
    prepare(module, sandbox)
    subprocess.run(["git", "-C", str(sandbox.fleet), "fetch", "-q", str(sandbox.origin), "main"], check=True)
    state, log = tick(module, sandbox)
    assert sandbox.assigned(), f"the lane must be assigned, log: {log}"
    assert not any(is_fetch(command) for command in sandbox.commands), \
        f"a present base must not trigger a fetch: {sandbox.commands}"
    assert not module.BACKOFF, module.BACKOFF
    print("  tick: base already present: dispatched without a fetch, no backoff: OK")


def check_tick_configured_repo(module, sandbox: Sandbox):
    """the target repo comes from the lane configuration, not from a hardcoded path."""
    prepare(module, sandbox)
    other = sandbox.root / "other-fleet"
    subprocess.run(["git", "clone", "-q", str(sandbox.origin), str(other)], check=True)
    state, log = tick(module, sandbox, lanes=sandbox.lanes(repo=other))
    have = git(other, "cat-file", "-e", f"{sandbox.base}^{{commit}}", check=False)
    assert have.returncode == 0, "the configured repo must be the one that is fetched"
    untouched = git(sandbox.fleet, "cat-file", "-e", f"{sandbox.base}^{{commit}}", check=False)
    assert untouched.returncode != 0, "the default repo must not be touched"
    print("  tick: target repo taken from the lane config (not hardcoded): OK")


def check_concurrent_dispatches(module, sandbox: Sandbox):
    """concurrent dispatches must not deadlock on the fetch."""
    prepare(module, sandbox)
    results = []

    def one(index):
        try:
            results.append(("ok", module.dispatch(f"lane-e{index}", sandbox.lanes()[LANE], sandbox.item(index), sandbox.base)))
        except RuntimeError as error:
            results.append(("error", str(error)))

    with cf.ThreadPoolExecutor(4) as pool:
        futures = [pool.submit(one, index) for index in range(4)]
        done, pending = concurrent_wait(futures, timeout=120)
    assert not pending, "the concurrent dispatches deadlocked on the fetch"
    assert len(done) == 4, results
    assert all(kind == "ok" for kind, _ in results), results
    print(f"  4 concurrent dispatches finished without a deadlock ({len(results)} assigned): OK")


def check_concurrent_ticks(module, sandbox: Sandbox):
    """two AD processes ticking at the same time must not deadlock on the fetch."""
    prepare(module, sandbox)
    peer = load_module("autodispatch_base_fetch_peer")
    prepare(peer, sandbox, state_dir="ad-state-peer")  # its own state dir, the same target repo
    results = []

    def one(index, runtime):
        try:
            runtime.tick(sandbox.lanes(), {}, 0)
            results.append(("ok", index))
        except Exception as error:
            results.append(("error", repr(error)))

    with cf.ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(one, index, runtime) for index, runtime in enumerate((module, peer))]
        done, pending = concurrent_wait(futures, timeout=120)
    assert not pending, "the concurrent ticks deadlocked on the fetch"
    assert len(done) == 2, results
    assert all(kind == "ok" for kind, _ in results), results
    print("  2 concurrent ticks finished without a deadlock: OK")


def concurrent_wait(futures, timeout):
    """Wait for the futures without blocking forever (a deadlock must fail the check)."""
    done, pending = set(), set(futures)
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        finished, pending = cf.wait(pending, timeout=1, return_when=cf.FIRST_COMPLETED)
        done |= finished
    return done, pending


def main():
    module = load_module()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="ad-base-fetch-", dir=str(SCRATCH)))
    try:
        assert " " not in str(root), "the sandbox path must not contain spaces"
        print(f"sandbox: {root}")
        for name, case in (("miss-then-fetch tick", check_tick_miss_then_fetch),
                           ("miss-after-fetch tick", check_tick_miss_after_fetch),
                           ("fetch-failure tick", check_tick_fetch_failure),
                           ("fetch-failure-not-masked tick", check_tick_fetch_failure_not_masked),
                           ("base-present tick", check_tick_base_present),
                           ("configured-repo tick", check_tick_configured_repo),
                           ("concurrent dispatches", check_concurrent_dispatches),
                           ("concurrent ticks", check_concurrent_ticks)):
            print(f"{name}:")
            case(module, Sandbox(root / name.replace(" ", "-")))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("ad base-commit fetch-on-miss: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
