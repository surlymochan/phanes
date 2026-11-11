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

The contract they cover:

  * when a host comes back, the automatic pause that the outage itself caused is
    re-checked and released - but only that one.  A pause of another cause (the hard
    disk limit, a manual hold) survives the recovery: a reachable host does not prove
    that the disk problem is gone;
  * a run that the host does not know any more is re-queued - but only after the
    dispatcher has verified that the runner of that run is really dead.  A runner whose
    liveness is *unknown* (the run has no status file, the host cannot tell) may still
    be running: re-queueing its card then dispatches the same work twice;
  * a failed label update keeps the lane state, so the next tick retries the
    re-queue instead of losing the card.

On the baseline the tests of this file fail:

  * ``reconcile()`` re-queues a run whose runner liveness it does not know, and the
    tick drops the lane state of that run;
  * the memory health path overwrites the automatic pause of the hard disk limit with
    the one of the memory limit and deletes it when the memory recovers.

Run them offline:

    python3 -m unittest discover -s tools/tests -p 'test_autodispatch_host_recovery.py' -v
"""
from __future__ import annotations

from _test_tmp import short_tmp_root

import importlib.util
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
AUTODISPATCH_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
HEALTH_PAUSE_PATH = ROOT / "tools" / "check-health-pause.py"
SCRATCH = Path(short_tmp_root())

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
              "runner_alive": None, "executor_alive": None, "run_state": "missing"}
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
        self.module.card_quality = lambda text: None  # these scenarios judge the lane recovery, not the card quality
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
        status = remote_status(runner_alive=False, executor_alive=False)
        self.assertTrue(self.module.reconcile(LANE, LANE_CFG, state, status, {}),
                        "a run whose runner is dead must be re-queued")
        self.assertNotIn("in-progress", self.fleet.issues[ISSUE]["labels"],
                         "the re-queued card loses its in-progress label")
        self.assertTrue(self.dispatchable(), "the re-queued card is dispatchable again")

    def test_an_unknown_executor_session_is_not_requeued(self):
        """A run whose executor session cannot be verified keeps its card.

        The remote host proves that a session is gone only with the process group
        of the executor; without that proof the run may still be executing, and
        re-queueing its card would dispatch the same work twice (Refs #1663)."""
        state = lane_state()
        status = remote_status(runner_alive=False, executor_alive=None)
        self.assertFalse(self.module.reconcile(LANE, LANE_CFG, state, status, {}),
                         "a run whose executor session is unknown must not be re-queued")
        self.assertEqual(self.fleet.edits, [],
                         "no label may be changed while the executor session is unknown")
        self.assertIn("in-progress", self.fleet.issues[ISSUE]["labels"],
                      "the card must keep its in-progress label while the session is unknown")

    def test_a_live_executor_session_is_not_requeued(self):
        """A run whose executor session is alive keeps its card."""
        state = lane_state()
        status = remote_status(runner_alive=False, executor_alive=True)
        self.assertFalse(self.module.reconcile(LANE, LANE_CFG, state, status, {}),
                         "a run whose executor session is alive must not be re-queued")
        self.assertEqual(self.fleet.edits, [])
        self.assertIn("in-progress", self.fleet.issues[ISSUE]["labels"])

    def test_an_unknown_executor_session_is_never_verified_as_lost(self):
        """The recovery of a host keeps a run whose executor session is unknown.

        The remote host proves that a session is gone only with the process
        group of the executor; without that proof the run may still be
        executing, so neither the lane nor the card may be released."""
        state = lane_state()
        status = remote_status(lane="busy", current_run=RUN, runner_alive=False,
                               executor_alive=None)
        control = {LANE: state, "_recovery": {LANE: {"run": RUN}}}
        self.assertFalse(self.module.verify_recovered_run(LANE, LANE_CFG, state, status, control),
                         "a run whose executor session is unknown must not be released")
        self.assertIn(LANE, control["_recovery"])
        self.assertEqual(self.fleet.edits, [],
                         "no label may be changed while the executor session is unknown")
        self.assertIn("in-progress", self.fleet.issues[ISSUE]["labels"],
                      "the card must keep its in-progress label while the session is unknown")

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
                                               remote_status(runner_alive=False, executor_alive=False), {}))
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
        self.module.card_quality = lambda text: None  # these scenarios judge the lane recovery, not the card quality
        self.fleet = FakeGitHub({ISSUE: {"state": "OPEN", "labels": ["lane-ready", "in-progress"]}})
        self.status = remote_status()
        # the dispatcher runs against the sandbox: no real state dir, no real fleet
        self.module.D = str(self.state_dir)
        self.module.BRAIN_LOG = str(self.root / "brain-log.md")
        self.module.gh = self.fleet
        self.module.log = lambda *args, **kwargs: None
        self.module.sh = self.fake_sh
        self.module.remote = lambda lane, cfg, run="": self.status
        self.module.clear_orphan_lane = lambda lane, cfg, run: True
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

    def outage(self):
        """The host is unreachable: the ssh probe of the lane fails."""
        self.status = {"error": "ssh: connect to host lane-host port 22: No route to host"}

    def host_back(self):
        """The host is reachable again (the backoff of the outage has passed)."""
        self.module.BACKOFF.clear()

    def host_pause(self):
        """The path of the automatic pause of the host."""
        return Path(self.module.host_pause_path(HOST))

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
        self.status = remote_status(runner_alive=False, executor_alive=False)
        state = self.tick({LANE: lane_state()})
        self.assertNotIn(LANE, state,
                         "the lane of a run whose runner is dead is released")
        self.assertNotIn("in-progress", self.fleet.issues[ISSUE]["labels"],
                         "the re-queued card loses its in-progress label")
        self.assertIn("lane-ready", self.fleet.issues[ISSUE]["labels"],
                      "the re-queued card carries the lane-ready label again")

    def test_the_tick_keeps_the_lane_when_requeue_label_update_fails(self):
        self.status = remote_status(runner_alive=False, executor_alive=False)
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


class HostPauseAttributionTests(unittest.TestCase):
    """The attribution of an automatic pause to the unreachability of the host.

    A pause is only released when its cause is the unreachability itself: a
    reachable host does not prove that the disk limit, the memory limit, a machine
    cooldown or an unknown failure of the host is gone."""

    def setUp(self):
        self.module = load_module("autodispatch_host_pause", AUTODISPATCH_PATH)
        self.module.card_quality = lambda text: None  # these scenarios judge the lane recovery, not the card quality
        self.module.log = lambda *args, **kwargs: None

    def test_the_marker_of_the_dispatcher_is_attributed(self):
        self.assertTrue(self.module.pause_cause_is_unreachable(
            f"{self.module.HOST_UNREACHABLE_MARKER} host={HOST} since=1700000000"))

    def test_an_explicit_ssh_reason_is_attributed(self):
        for body in ("auto: ssh streak 3\n", "auto: ssh unreachable\n",
                     "auto: host_unreachable\n", "auto: ssh down\n"):
            self.assertTrue(self.module.pause_cause_is_unreachable(body), body)

    def test_another_cause_is_not_attributed(self):
        for body in ("auto: disk_hard\n", "auto:host_hard mem_avail_mb=255\n",
                     "auto:cooldown:until=2035-01-01T00:00:00Z\n", "auto: quota 429\n",
                     "auto: api failure streak 3\n", "auto: lane hold\n", "operator hold\n"):
            self.assertFalse(self.module.pause_cause_is_unreachable(body), body)


class RunnerLivenessTests(unittest.TestCase):
    """The liveness of the runner of a run: the proof that a run is really lost."""

    def setUp(self):
        self.module = load_module("autodispatch_runner_liveness", AUTODISPATCH_PATH)
        self.module.card_quality = lambda text: None  # these scenarios judge the lane recovery, not the card quality

    def test_a_live_runner(self):
        self.assertEqual(self.module.runner_liveness({"runner_alive": True}), "alive")

    def test_a_dead_runner(self):
        self.assertEqual(self.module.runner_liveness({"runner_alive": False}), "dead")

    def test_a_finished_run_has_no_runner(self):
        self.assertEqual(self.module.runner_liveness({"run_state": "finished"}), "dead")
        self.assertEqual(self.module.runner_liveness({"run_state": "unroutable"}), "dead")

    def test_an_unknown_liveness(self):
        self.assertEqual(self.module.runner_liveness({"runner_alive": None}), "unknown")
        self.assertEqual(self.module.runner_liveness({"run_state": "missing"}), "unknown")
        self.assertEqual(self.module.runner_liveness({}), "unknown")


class HostRecoveryPauseTests(unittest.TestCase):
    """The re-check of the pauses of a host that came back."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="ad-host-pause-", dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.module = load_module("autodispatch_host_recovery_pause", AUTODISPATCH_PATH)
        self.module.card_quality = lambda text: None  # these scenarios judge the lane recovery, not the card quality
        self.module.D = str(self.root)
        self.module.log = lambda *args, **kwargs: None

    def pause_paths(self):
        return self.module.host_pause_paths(HOST, [LANE])

    def test_the_pause_of_the_outage_is_released(self):
        pause = Path(self.module.host_pause_path(HOST))
        pause.write_text(f"{self.module.HOST_UNREACHABLE_MARKER} host={HOST} since=1700000000\n")
        released = self.module.release_unreachable_pauses(HOST, self.pause_paths())
        self.assertEqual(released, [f"PAUSE-HOST-{HOST}"])
        self.assertFalse(pause.exists(), "the pause of the outage must be released")

    def test_the_lane_pause_of_the_outage_is_released(self):
        lane_pause = Path(self.module.lane_pause_path(LANE))
        lane_pause.write_text("auto: ssh streak 3\n")
        released = self.module.release_unreachable_pauses(HOST, self.pause_paths())
        self.assertEqual(released, [f"PAUSE-{LANE}"])
        self.assertFalse(lane_pause.exists(), "the lane pause of the outage must be released")

    def test_an_independent_pause_survives_the_recovery(self):
        pause = Path(self.module.host_pause_path(HOST))
        pause.write_text("auto: disk_hard\n")
        released = self.module.release_unreachable_pauses(HOST, self.pause_paths())
        self.assertEqual(released, [], "the pause of the disk limit must not be released")
        self.assertTrue(pause.exists())
        self.assertIn("disk_hard", pause.read_text())

    def test_a_manual_pause_survives_the_recovery(self):
        pause = Path(self.module.host_pause_path(HOST))
        pause.write_text("operator hold\n")
        released = self.module.release_unreachable_pauses(HOST, self.pause_paths())
        self.assertEqual(released, [])
        self.assertTrue(pause.exists(), "a manual hold must never be released")

    def test_the_marker_never_overwrites_another_cause(self):
        pause = Path(self.module.host_pause_path(HOST))
        pause.write_text("auto: disk_hard\n")
        self.assertFalse(self.module.write_unreachable_pause(HOST, 1700000000))
        self.assertIn("disk_hard", pause.read_text(),
                      "the pause of the disk limit must not be overwritten by the marker")

    def test_a_disk_pause_written_during_marker_creation_is_kept(self):
        pause = Path(self.module.host_pause_path(HOST))
        real_open = open

        def competing_open(path, mode="r", *args, **kwargs):
            if str(path) == str(pause) and mode in ("w", "x"):
                pause.write_text("auto: disk_hard\n")
            return real_open(path, mode, *args, **kwargs)

        with patch.object(self.module, "open", competing_open, create=True):
            self.assertFalse(self.module.write_unreachable_pause(HOST, 1700000000))
        self.assertEqual(pause.read_text(), "auto: disk_hard\n")

    def test_a_disk_pause_written_after_the_read_is_restored(self):
        pause = Path(self.module.host_pause_path(HOST))
        pause.write_text("auto: ssh unreachable\n")
        read_pause = self.module.read_pause

        def competing_read(path):
            body = read_pause(path)
            if str(path) == str(pause):
                pause.write_text("auto: disk_hard\n")
            return body

        with patch.object(self.module, "read_pause", competing_read):
            self.assertEqual(self.module.release_unreachable_pauses(HOST, [str(pause)]), [])
        self.assertEqual(pause.read_text(), "auto: disk_hard\n")
        self.assertEqual(list(self.root.glob(".pause-recovery-*")), [])

    def test_a_new_pause_after_the_claim_is_never_unlinked(self):
        pause = Path(self.module.host_pause_path(HOST))
        pause.write_text("auto: ssh unreachable\n")
        read_pause = self.module.read_pause

        def competing_read(path):
            body = read_pause(path)
            if str(path) != str(pause):
                pause.write_text("auto: disk_hard\n")
            return body

        with patch.object(self.module, "read_pause", competing_read):
            self.assertEqual(self.module.release_unreachable_pauses(HOST, [str(pause)]), [])
        self.assertEqual(pause.read_text(), "auto: disk_hard\n")
        self.assertEqual(list(self.root.glob(".pause-recovery-*")), [])

    def test_a_restore_collision_preserves_both_pause_causes(self):
        pause = Path(self.module.host_pause_path(HOST))
        pause.write_text("auto: ssh unreachable\n")
        read_pause = self.module.read_pause

        def competing_read(path):
            body = read_pause(path)
            if str(path) == str(pause):
                pause.write_text("auto: disk_hard\n")
            else:
                pause.write_text("operator hold\n")
            return body

        with patch.object(self.module, "read_pause", competing_read):
            self.assertEqual(self.module.release_unreachable_pauses(HOST, [str(pause)]), [])
        self.assertEqual(pause.read_text(), "operator hold\n")
        claims = list(self.root.glob(".pause-recovery-*/PAUSE-HOST-*"))
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].read_text(), "auto: disk_hard\n")
        pause.unlink()  # Even if the newer hold clears, disk protection remains.
        self.assertTrue(self.module.lane_paused(LANE, LANE_CFG))

    def test_a_failed_claim_cleanup_keeps_the_pause_gate(self):
        pause = Path(self.module.host_pause_path(HOST))
        pause.write_text("auto: ssh unreachable\n")
        with patch.object(self.module.os, "unlink", side_effect=PermissionError("held")):
            self.assertEqual(self.module.release_unreachable_pauses(HOST, [str(pause)]), [])
        self.assertTrue(self.module.lane_paused(LANE, LANE_CFG))


class HostRecoveryTickTests(RecoveryTickTests):
    """The recovery of a host inside the tick of the dispatcher.

    The tick of the outage holds the host with the marker of its unreachability;
    the tick of the recovery re-checks that pause and verifies the runs that were
    in flight."""

    def test_the_outage_holds_the_host(self):
        self.outage()
        state = self.tick({LANE: lane_state()})
        self.assertTrue(self.host_pause().exists(),
                        "the outage must hold the host with the marker of its unreachability")
        self.assertIn(self.module.HOST_UNREACHABLE_MARKER, self.host_pause().read_text())

    def test_the_recovery_keeps_a_disk_pause_written_during_release(self):
        self.outage()
        state = self.tick({LANE: lane_state()})
        self.host_back()
        self.status = remote_status(runner_alive=True)
        pause = self.host_pause()
        read_pause = self.module.read_pause

        def competing_read(path):
            body = read_pause(path)
            if str(path) == str(pause):
                pause.write_text("auto: disk_hard\n")
            return body

        with patch.object(self.module, "read_pause", competing_read):
            state = self.tick(state)
        self.assertEqual(pause.read_text(), "auto: disk_hard\n")
        self.assertIn(LANE, state)
        self.assertTrue(self.module.lane_paused(LANE, LANE_CFG))
        self.assertEqual(self.fleet.edits, [])

    def test_the_recovery_releases_the_pause_and_requeues_the_lost_run(self):
        self.outage()
        state = self.tick({LANE: lane_state()})
        self.host_back()
        self.status = remote_status(lane="busy", current_run=RUN, runner_alive=False,
                                    executor_alive=False,
                                    run_state="running")
        state = self.tick(state)
        self.assertFalse(self.host_pause().exists(),
                         "the pause of the outage must be released when the host is back")
        self.assertNotIn(LANE, state, "the lane of the lost run must be released")
        self.assertNotIn("in-progress", self.fleet.issues[ISSUE]["labels"])
        self.assertIn("lane-ready", self.fleet.issues[ISSUE]["labels"],
                      "the card of the lost run must be re-queued")

    def test_the_recovery_keeps_a_run_with_an_unknown_liveness(self):
        self.outage()
        state = self.tick({LANE: lane_state()})
        self.host_back()
        self.status = remote_status(lane="busy", current_run=RUN)
        state = self.tick(state)
        self.assertFalse(self.host_pause().exists(),
                         "the pause of the outage must be released when the host is back")
        self.assertIn(LANE, state, "the lane of a run with an unknown liveness must be kept")
        self.assertIn("in-progress", self.fleet.issues[ISSUE]["labels"],
                      "the card must keep its in-progress label while the liveness is unknown")
        self.assertIn(LANE, state.get("_recovery", {}),
                      "the verification of the run must be retried in the next tick")

    def test_the_recovery_retries_the_requeue_after_a_failed_label_update(self):
        self.outage()
        state = self.tick({LANE: lane_state()})
        self.host_back()
        self.status = remote_status(lane="busy", current_run=RUN, runner_alive=False,
                                    executor_alive=False,
                                    run_state="running")
        self.fleet.fail_edits = True
        state = self.tick(state)
        self.assertIn(LANE, state, "the lane must stay recorded while the label update fails")
        self.assertIn("in-progress", self.fleet.issues[ISSUE]["labels"])
        self.assertIn(LANE, state.get("_recovery", {}),
                      "the re-queue must be retried in the next tick")
        # the label update works again: the next tick re-queues the card
        self.fleet.fail_edits = False
        state = self.tick(state)
        self.assertNotIn(LANE, state, "the lane of the lost run must be released")
        self.assertNotIn("in-progress", self.fleet.issues[ISSUE]["labels"])
        self.assertIn("lane-ready", self.fleet.issues[ISSUE]["labels"])

    def test_the_recovery_keeps_an_independent_pause(self):
        self.outage()
        pause = self.host_pause()
        pause.write_text("auto: disk_hard\n")
        state = self.tick({LANE: lane_state()})
        self.host_back()
        self.status = remote_status(lane="busy", current_run=RUN, runner_alive=False,
                                    executor_alive=False,
                                    run_state="running")
        state = self.tick(state)
        self.assertTrue(pause.exists(),
                        "the pause of the disk limit must survive the recovery of the host")
        self.assertIn("disk_hard", pause.read_text())

    def test_the_recovery_keeps_a_live_run(self):
        self.outage()
        state = self.tick({LANE: lane_state()})
        self.host_back()
        self.status = remote_status(lane="busy", current_run=RUN, runner_alive=True,
                                    run_state="running")
        state = self.tick(state)
        self.assertIn(LANE, state, "the lane of a live run must be kept")
        self.assertIn("in-progress", self.fleet.issues[ISSUE]["labels"],
                      "the card of a live run must not be re-queued")
        self.assertNotIn(LANE, state.get("_recovery", {}),
                         "the verification of a live run is finished")


if __name__ == "__main__":
    unittest.main(verbosity=2)
