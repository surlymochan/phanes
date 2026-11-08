#!/usr/bin/env python3
"""Reproduction of the host-recovery gap of the lane autodispatcher (Refs #1673).

The box lost its connection twice on 2026-11-02: the control plane wrote
``PAUSE-HOST-box`` and nobody noticed when the host came back.  The automatic pause
survived the outage and the run that was in flight was lost with it (M6's #1361 run
was unreachable for 44 minutes).  The issue asks for two things:

  * when a host comes back, the automatic pause that the outage itself caused is
    re-checked and released - but only that one.  A pause of another cause (the hard
    disk limit, a manual hold) survives the recovery: a reachable host does not prove
    that the disk problem is gone;
  * a run that the host does not know any more is re-queued - but only after the
    dispatcher has verified that the runner of that run is really dead.  A runner whose
    liveness is *unknown* (the run has no status file, the host cannot tell) may still
    be running: re-queueing its card then dispatches the same work twice.

The tests below drive the real ``reconcile()`` and the real ``tick()`` of the
autodispatcher against a fake fleet (no network, no ssh, no GitHub), and the real
offline health contract of the pause store (``tools/check-health-pause.py``).

On the baseline the tests of this file fail:

  * ``reconcile()`` re-queues a run whose runner liveness it does not know, and the
    tick drops the lane state of that run;
  * the memory health path overwrites the automatic pause of the hard disk limit with
    the one of the memory limit and deletes it when the memory recovers.

Run them offline:

    python3 -m unittest discover -s tools/tests -p 'test_autodispatch_host_recovery.py' -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUTODISPATCH_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
HEALTH_PAUSE_PATH = ROOT / "tools" / "check-health-pause.py"
SCRATCH = ROOT / ".swrm-tmp"

LANE = "GH2"
HOST = "lane-host"
ISSUE = 42
RUN = "run-42"
LANE_CFG = {"host": HOST, "backend": "pi", "repo": "/tmp/fleet", "fleet": "/tmp/swrm-fleet"}
# The run of the lane is older than the 600 s the dispatcher waits before it calls a
# run without a runner "old enough": the age is not what protects the run here, the
# unknown liveness of its runner is.
OLD_RUN_S = 900


def load_module(name, path):
    """Load a fresh copy of a script (every test patches its own copy)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lane_state(age_seconds=OLD_RUN_S):
    """The state of a lane that is running a card for `age_seconds` seconds."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - age_seconds))
    return {"issue": ISSUE, "run": RUN, "host": HOST, "base": "a" * 40, "at": stamp, "verify": None}


def remote_status(**overrides):
    """The status of a lane whose run the host does not know any more.

    The defaults are the status of a run whose status file is missing: the run is
    still marked as running, but the dispatcher cannot tell whether its runner is
    alive (``runner_alive`` is None) and it does not even know the state of the run
    (``run_state`` is "missing")."""
    status = {"lane": "idle", "current_run": None, "run": "running",
              "runner_alive": None, "run_state": "missing"}
    status.update(overrides)
    return status


class FakeGitHub:
    """The `gh` calls of the dispatcher: the issue table and the label edits."""

    def __init__(self, issues):
        self.issues = issues          # issue number -> {"state": ..., "labels": [...]}
        self.calls = []               # every call, in order
        self.edits = []               # the `gh issue edit` calls only
        self.fail_edits = False

    def __call__(self, args, timeout=60):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            rows = [{"number": number, "title": "#%d" % number, "body": "",
                     "labels": list(issue["labels"]), "state": issue["state"]}
                    for number, issue in sorted(self.issues.items())]
            return 0, json.dumps(rows), ""
        if args[:2] == ["issue", "view"]:
            number = int(args[2])
            issue = self.issues.get(number)
            if issue is None:
                return 1, "", "gh: no such issue"
            return 0, json.dumps({"number": number, "state": issue["state"],
                                   "labels": [{"name": name} for name in issue["labels"]]}), ""
        if args[:2] == ["issue", "edit"]:
            number = int(args[2])
            self.edits.append(args)
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


class OrphanRunTests(unittest.TestCase):
    """The recovery of a run the host does not know any more (`reconcile()`)."""

    def setUp(self):
        self.module = load_module("autodispatch_host_recovery", AUTODISPATCH_PATH)
        self.fleet = FakeGitHub({ISSUE: {"state": "OPEN", "labels": ["lane-ready", "in-progress"]}})
        self.module.gh = self.fleet
        self.module.log = lambda *args, **kwargs: None

    def dispatchable(self):
        """Whether the dispatcher would hand the card of the issue out again."""
        issue = dict(self.fleet.issues[ISSUE])
        issue["labels"] = list(issue["labels"])
        return self.module.dispatchable_issue(issue)

    def test_an_unknown_runner_liveness_is_not_requeued(self):
        """A run whose runner liveness is unknown may still be running.

        Re-queueing its card would dispatch the same work twice, so the dispatcher
        must keep the run (and its card) until the liveness is known."""
        state = lane_state()
        status = remote_status()
        self.assertFalse(self.module.reconcile(LANE, LANE_CFG, state, status, {}),
                         "a run whose runner liveness is unknown must not be re-queued: "
                         "the runner may still be alive")
        self.assertEqual(self.fleet.edits, [],
                         "no label may be changed while the runner liveness is unknown")
        self.assertIn("in-progress", self.fleet.issues[ISSUE]["labels"],
                      "the card must keep its in-progress label while the runner liveness is unknown")
        self.assertFalse(self.dispatchable(),
                         "the card must not be dispatchable again while the runner liveness is unknown")

    def test_a_dead_runner_is_requeued(self):
        """A run whose runner is dead is cleaned up and its card is re-queued."""
        state = lane_state()
        status = remote_status(runner_alive=False)
        self.assertTrue(self.module.reconcile(LANE, LANE_CFG, state, status, {}),
                        "a run whose runner is dead must be re-queued")
        self.assertNotIn("in-progress", self.fleet.issues[ISSUE]["labels"],
                         "the re-queued card loses its in-progress label")
        self.assertTrue(self.dispatchable(), "the re-queued card is dispatchable again")

    def test_a_young_run_with_an_unknown_liveness_is_not_requeued(self):
        """A run that just started is never re-queued, whatever the liveness says."""
        state = lane_state(age_seconds=60)
        status = remote_status()
        self.assertFalse(self.module.reconcile(LANE, LANE_CFG, state, status, {}),
                         "a young run must not be re-queued")
        self.assertEqual(self.fleet.edits, [], "no label may be changed for a young run")

    def test_failed_label_update_does_not_release_the_run(self):
        """Failed relabeling leaves state in place so the next tick can retry."""
        self.fleet.fail_edits = True
        state = lane_state()
        self.assertFalse(self.module.reconcile(LANE, LANE_CFG, state,
                                               remote_status(runner_alive=False), {}))
        self.assertIn("in-progress", self.fleet.issues[ISSUE]["labels"])


class RecoveryTickTests(unittest.TestCase):
    """The recovery of a run inside the tick of the dispatcher."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="ad-recovery-", dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.module = load_module("autodispatch_host_recovery_tick", AUTODISPATCH_PATH)
        self.fleet = FakeGitHub({ISSUE: {"state": "OPEN", "labels": ["lane-ready", "in-progress"]}})
        self.status = remote_status()
        # the dispatcher runs against the sandbox: no real state dir, no real fleet
        self.module.D = str(self.state_dir)
        self.module.BRAIN_LOG = str(self.root / "brain-log.md")
        self.module.gh = self.fleet
        self.module.log = lambda *args, **kwargs: None
        self.module.sh = self.fake_sh
        self.module.remote = lambda lane, cfg, run="": self.status
        self.module.replenish = lambda count, state: []
        self.module.BACKOFF.clear()
        # the pause store of the operator must not leak into the test
        old_home = os.environ.get("OPENSWARM_HOME")
        os.environ["OPENSWARM_HOME"] = str(self.root / "swrm-home")
        self.addCleanup(self.restore_home, old_home)

    def restore_home(self, old_home):
        if old_home is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = old_home

    def fake_sh(self, cmd, timeout=120, inp=None):
        """The `sh` calls of the dispatcher: only the base commit of a dispatch."""
        command = cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd)
        if "ls-remote" in command:
            return 0, "%s\trefs/heads/main\n" % ("a" * 40), ""
        return 0, "", ""

    def tick(self, state):
        self.module.tick({LANE: LANE_CFG}, state, 0)
        return state

    def test_the_tick_keeps_the_lane_of_a_run_with_an_unknown_liveness(self):
        """The tick must not drop the lane of a run whose runner liveness is unknown."""
        state = self.tick({LANE: lane_state()})
        self.assertIn(LANE, state,
                      "the lane state must survive a tick that cannot prove the runner dead")
        self.assertEqual(state[LANE]["issue"], ISSUE)
        self.assertEqual(self.fleet.edits, [],
                         "the issue must not be relabelled while the runner liveness is unknown")
        self.assertIn("in-progress", self.fleet.issues[ISSUE]["labels"],
                      "the card must keep its in-progress label while the runner liveness is unknown")

    def test_the_tick_releases_the_lane_of_a_run_with_a_dead_runner(self):
        """The lane of a run whose runner is dead is released and its card re-queued."""
        self.status = remote_status(runner_alive=False)
        state = self.tick({LANE: lane_state()})
        self.assertNotIn(LANE, state,
                         "the lane of a run whose runner is dead is released")
        self.assertNotIn("in-progress", self.fleet.issues[ISSUE]["labels"],
                         "the re-queued card loses its in-progress label")
        self.assertIn("lane-ready", self.fleet.issues[ISSUE]["labels"],
                      "the re-queued card carries the lane-ready label again")

    def test_the_tick_keeps_the_lane_when_requeue_label_update_fails(self):
        self.status = remote_status(runner_alive=False)
        self.fleet.fail_edits = True
        state = self.tick({LANE: lane_state()})
        self.assertIn(LANE, state,
                      "the lane must remain recorded until issue relabeling succeeds")
        self.assertIn("in-progress", self.fleet.issues[ISSUE]["labels"])


class HealthPauseContractTests(unittest.TestCase):
    """The offline health contract of the pause store (`tools/check-health-pause.py`).

    The health loop holds a host with one automatic pause per scope.  A host can be
    held by more than one cause (the hard disk limit and the memory limit at the same
    time): the cause that is still true must survive the self-heal of the other one."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.module = load_module("check_health_pause", HEALTH_PAUSE_PATH)
        self.state_dir = Path(tempfile.mkdtemp(prefix="health-pause-", dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, self.state_dir, ignore_errors=True)
        self.host = "worker-b.example.invalid"
        self.host_pause = self.state_dir / ("PAUSE-HOST-%s" % self.host)

    def test_the_memory_health_does_not_overwrite_the_disk_pause(self):
        """A low memory snapshot must not take the pause of the hard disk limit over."""
        self.host_pause.write_text("auto: disk_hard\n")
        low = {self.host: {"mem_avail_mb": self.module.HEALTH_MEM_HARD_MB - 1}}
        self.module.reconcile_mem_health(self.state_dir, {}, low, {self.host: ["CX2"]})
        self.assertTrue(self.host_pause.exists(),
                        "the automatic pause of the host must survive a low memory snapshot")
        self.assertIn("disk_hard", self.host_pause.read_text(),
                      "the pause must keep the cause that holds the host (the disk limit)")

    def test_the_memory_recovery_does_not_release_the_disk_pause(self):
        """The memory self-heal must not release a pause another cause still holds."""
        self.host_pause.write_text("auto: disk_hard\n")
        low = {self.host: {"mem_avail_mb": self.module.HEALTH_MEM_HARD_MB - 1}}
        recovered = {self.host: {"mem_avail_mb": self.module.HEALTH_MEM_SOFT_MB}}
        self.module.reconcile_mem_health(self.state_dir, {}, low, {self.host: ["CX2"]})
        self.module.reconcile_mem_health(self.state_dir, low, recovered, {self.host: ["CX2"]})
        self.assertTrue(self.host_pause.exists(),
                        "the memory self-heal must not release the pause of the disk limit")
        self.assertIn("disk_hard", self.host_pause.read_text(),
                      "the pause must keep the cause that holds the host (the disk limit)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
