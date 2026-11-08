#!/usr/bin/env python3
"""Offline contract of the host recovery of the lane autodispatcher (Refs #1673).

The check drives the runtime functions of the dispatcher against a fake fleet (no
ssh, no network, no GitHub) and a temporary pause directory: the real
``~/phanes-ctl/autodispatch`` is never read, written or removed.

The contract has two halves:

  * the pause of a host that came back is released only when its cause is the
    unreachability of that host.  A pause of another cause (the hard disk limit,
    the memory limit, a machine cooldown, an operator hold, an unknown reason such
    as ``api failure streak 3``) survives: a reachable host does not prove that
    cause is gone;
  * a run of a host that came back is re-queued only when the runner of that run is
    *positively* dead.  An unknown liveness keeps the lane and its card, and a
    failed label update keeps the lane state so that the next tick retries.

Run it offline:

    python3 tools/check-host-recovery.py
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"

HOST = "node-9.example.invalid"
LANE = "M4"
ISSUE = 1673
RUN = "20261102-203554-0c6915"


def load_runtime():
    """Load a fresh copy of the dispatcher (the check patches its own copy)."""
    spec = importlib.util.spec_from_file_location("autodispatch_runtime", RUNTIME)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {RUNTIME}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeFleet:
    """The `gh` calls of the dispatcher: the issue table and the label edits."""

    def __init__(self):
        self.issues = {ISSUE: {"state": "OPEN", "labels": ["lane-ready", "in-progress"]}}
        self.fail_edits = False

    def __call__(self, args, timeout=60):
        args = list(args)
        if args[:2] == ["issue", "view"]:
            number = int(args[2])
            issue = self.issues.get(number)
            if issue is None:
                return 1, "", "gh: no such issue"
            return 0, json.dumps({"number": number, "state": issue["state"],
                                   "labels": [{"name": name} for name in issue["labels"]]}), ""
        if args[:2] == ["issue", "edit"]:
            number = int(args[2])
            if self.fail_edits:
                return 1, "", "synthetic label update failure"
            issue = self.issues.get(number)
            if issue is not None:
                for flag, add in (("--add-label", True), ("--remove-label", False)):
                    if flag not in args:
                        continue
                    label = args[args.index(flag) + 1]
                    if add and label not in issue["labels"]:
                        issue["labels"].append(label)
                    elif not add and label in issue["labels"]:
                        issue["labels"].remove(label)
            return 0, "", ""
        return 0, "", ""


def lane_state(age_seconds=900):
    """The state of a lane that runs a card since `age_seconds` seconds."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - age_seconds))
    return {"issue": ISSUE, "run": RUN, "host": HOST, "base": "a" * 40, "at": stamp,
            "verify": None, "class": "work"}


def main() -> None:
    runtime = load_runtime()
    runtime.log = lambda *args, **kwargs: None
    fleet = FakeFleet()
    runtime.gh = fleet

    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as fixture:
        runtime.D = fixture
        state_dir = Path(fixture)
        host_pause = state_dir / f"PAUSE-HOST-{HOST}"
        lane_pause = state_dir / f"PAUSE-{LANE}"

        # 1) the outage holds the host with the marker of its unreachability
        state = {}
        runtime.note_host_unreachable(state, HOST)
        assert host_pause.exists(), "the outage must hold the host"
        assert host_pause.read_text().startswith(runtime.HOST_UNREACHABLE_MARKER)
        assert "host=" + HOST in host_pause.read_text()

        # 2) the recovery releases the pause of the outage
        assert runtime.host_came_back(state, HOST), "the recovery of the host must be detected"
        released = runtime.release_unreachable_pauses(HOST, runtime.host_pause_paths(HOST, [LANE]))
        assert released == [f"PAUSE-HOST-{HOST}"], released
        assert not host_pause.exists(), "the pause of the outage must be released"

        # 3) a pause of another cause survives the recovery of the host
        for body in ("auto: disk_hard\n", "auto:host_hard mem_avail_mb=255\n",
                     "auto:cooldown:until=2035-01-01T00:00:00Z\n", "auto: quota 429\n",
                     "auto: api failure streak 3\n", "operator hold\n"):
            host_pause.write_text(body)
            released = runtime.release_unreachable_pauses(HOST, runtime.host_pause_paths(HOST, [LANE]))
            assert released == [], f"{body!r} must not be released"
            assert host_pause.exists(), f"{body!r} must survive the recovery"
            assert host_pause.read_text() == body
        host_pause.unlink()

        # 4) the marker never takes the pause of another cause over
        host_pause.write_text("auto: disk_hard\n")
        assert not runtime.write_unreachable_pause(HOST, int(time.time()))
        assert "disk_hard" in host_pause.read_text()
        host_pause.unlink()

        # A health write between the cause check and removal must survive too.
        host_pause.write_text("auto: ssh unreachable\n")
        read_pause = runtime.read_pause

        def health_write_during_read(path):
            body = read_pause(path)
            if str(path) == str(host_pause):
                host_pause.write_text("auto: disk_hard\n")
            return body

        with patch.object(runtime, "read_pause", health_write_during_read):
            assert runtime.release_unreachable_pauses(HOST, [str(host_pause)]) == []
        assert host_pause.read_text() == "auto: disk_hard\n"
        host_pause.unlink()

        # 5) the lane pause of the outage is released, an independent one is kept
        lane_pause.write_text("auto: ssh streak 3\n")
        released = runtime.release_unreachable_pauses(HOST, runtime.host_pause_paths(HOST, [LANE]))
        assert released == [f"PAUSE-{LANE}"], released
        lane_pause.write_text("auto:mem_hard host=x mem_avail_mb=1\n")
        released = runtime.release_unreachable_pauses(HOST, runtime.host_pause_paths(HOST, [LANE]))
        assert released == [], "the memory pause of a lane must survive the recovery"
        assert lane_pause.exists()
        lane_pause.unlink()

        # 6) the liveness of the runner decides whether a run is lost
        assert runtime.runner_liveness({"runner_alive": True}) == "alive"
        assert runtime.runner_liveness({"runner_alive": False}) == "dead"
        assert runtime.runner_liveness({"run_state": "finished"}) == "dead"
        assert runtime.runner_liveness({"runner_alive": None, "run_state": "missing"}) == "unknown"
        assert runtime.runner_liveness({}) == "unknown"

        # 7) a lost run is re-queued, an unknown liveness keeps the lane and the card
        st = lane_state()
        pending = {"_recovery": {LANE: {"host": HOST, "run": RUN, "issue": ISSUE,
                                         "since": time.time()}}}
        assert not runtime.verify_recovered_run(LANE, {"host": HOST}, st,
                                                {"runner_alive": None, "run_state": "missing"}, pending)
        assert LANE in pending["_recovery"], "the verification of an unknown liveness must be retried"
        assert "in-progress" in fleet.issues[ISSUE]["labels"]

        assert runtime.verify_recovered_run(LANE, {"host": HOST}, st,
                                            {"runner_alive": False, "run_state": "running"}, pending)
        assert LANE not in pending["_recovery"], "a lost run must not be verified again"
        assert "in-progress" not in fleet.issues[ISSUE]["labels"], "the card must be re-queued"
        assert "lane-ready" in fleet.issues[ISSUE]["labels"]

        # 8) a failed label update keeps the lane state, so the next tick retries
        fleet.issues[ISSUE]["labels"] = ["lane-ready", "in-progress"]
        pending["_recovery"] = {LANE: {"host": HOST, "run": RUN, "issue": ISSUE,
                                        "since": time.time()}}
        fleet.fail_edits = True
        assert not runtime.verify_recovered_run(LANE, {"host": HOST}, st,
                                                {"runner_alive": False, "run_state": "running"}, pending)
        assert LANE in pending["_recovery"], "the re-queue must be retried after a failed label update"
        assert "in-progress" in fleet.issues[ISSUE]["labels"]
        fleet.fail_edits = False
        assert runtime.verify_recovered_run(LANE, {"host": HOST}, st,
                                            {"runner_alive": False, "run_state": "running"}, pending)
        assert "in-progress" not in fleet.issues[ISSUE]["labels"]

    print("OK: the outage holds the host and the recovery releases that pause")
    print("OK: a pause of another cause (disk, memory, cooldown, quota, unknown) survives")
    print("OK: the marker of the dispatcher never overwrites the pause of another cause")
    print("OK: a concurrent disk pause survives the recovery cause check")
    print("OK: the lane pause of the outage is released, an independent one is kept")
    print("OK: only a positively dead runner re-queues a card, an unknown liveness keeps it")
    print("OK: a failed label update keeps the lane state, so the next tick retries")


if __name__ == "__main__":
    main()
