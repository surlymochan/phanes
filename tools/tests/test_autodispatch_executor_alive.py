#!/usr/bin/env python3
"""The liveness proof of an executor session (Refs #1663).

The control plane may only release an in-flight run when the whole session of that
run is provably gone.  A runner pid, an executor pid and a scan of the workspace of
the run do not prove that: a child can leave the process group of its executor,
close the output files of the run and keep working outside the workspace.  The proof
the dispatcher accepts is the process group of the executor: when the remote state
records it and that group has no member left, the session is dead.  Every other
outcome - no recorded group, a group with a member, a check that fails - is unknown:
the run stays in flight, its lane keeps the claim and its card is never dispatched
a second time.

The tests below drive the real remote probe (``REMOTE_STATUS``), the real remote
cleanup (``REMOTE_CLEAR_ORPHAN``) and the real ``reconcile()`` of the dispatcher
against a fake lane home: no network, no ssh, no GitHub.

Run them offline:

    python3 -m unittest tools.tests.test_autodispatch_executor_alive -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import importlib.machinery
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
AUTODISPATCH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
LANE = "D1"
RUN = "old-run"
ISSUE = 42
DEAD_PID = 2147483647          # no process owns this pid


def load_module(name="autodispatch_executor_alive"):
    """A fresh copy of the dispatcher (every test patches its own copy)."""
    spec = importlib.util.spec_from_file_location(name, AUTODISPATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AD = load_module()


def stop(child):
    """Kill a helper process and reap it."""
    try:
        child.kill()
    except OSError:
        pass
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def empty_group():
    """The id of a process group without any member: a session leader that exited."""
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    child.wait(timeout=30)
    return child.pid


def live_group():
    """The id of a process group with a living member, and the member itself."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             start_new_session=True)
    return child.pid, child


class FakeHost:
    """A lane home on a fake host: a busy lane and a run without a result."""

    def __init__(self, root, real_proc=False):
        self.root = Path(root)
        self.lane_dir = self.root / "lanes" / LANE
        self.run_dir = self.root / "runs" / RUN
        self.lane_dir.mkdir(parents=True)
        self.run_dir.mkdir(parents=True)
        self.events = self.run_dir / "events.jsonl"
        self.events.write_text("")
        # An empty process table: the session is decided by the process group of
        # the executor, not by a scan of the host.  A host that has to show its
        # own processes (a child that writes the output of the run) uses the
        # real process table of the machine.
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.real_proc = real_proc
        self.write_lane_state("busy", RUN)

    def write_lane_state(self, state, current_run):
        (self.lane_dir / "state.json").write_text(
            json.dumps({"state": state, "current_run": current_run}))

    def lane_state(self):
        return json.loads((self.lane_dir / "state.json").read_text())

    def write_status(self, **fields):
        fields.setdefault("events", str(self.events))
        fields.setdefault("state", "running")
        (self.run_dir / "status.json").write_text(json.dumps(fields))

    def hold_output(self, pid=4242):
        """A process that holds the output file of the run open."""
        fd_dir = self.proc / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        os.symlink(str(self.events.resolve()), str(fd_dir / "1"))

    def _environment(self):
        return {"OPENSWARM_HOME": str(self.root),
                "OPEN_SWARM_PROC_ROOT": "/proc" if self.real_proc else str(self.proc)}

    def probe(self):
        """The verdict of the remote probe (REMOTE_STATUS) on this host."""
        output = io.StringIO()
        with patch.dict(os.environ, self._environment()), \
                patch.object(sys, "argv", ["status", LANE, RUN]), \
                contextlib.redirect_stdout(output):
            exec(AD.REMOTE_STATUS, {})
        return json.loads(output.getvalue())

    def cleanup(self):
        """The result of the remote cleanup (REMOTE_CLEAR_ORPHAN) on this host."""
        output = io.StringIO()
        with patch.dict(os.environ, self._environment()), \
                patch.object(sys, "argv", ["cleanup", LANE, RUN]), \
                contextlib.redirect_stdout(output):
            exec(AD.REMOTE_CLEAR_ORPHAN, {})
        return json.loads(output.getvalue())


class SessionVerdictTests(unittest.TestCase):
    """The verdict of the remote probe: is the executor session provably gone?"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ad-executor-alive-")
        self.addCleanup(self.tmp.cleanup)
        self.host = FakeHost(self.tmp.name)

    def test_an_empty_process_group_and_a_dead_runner_prove_the_session_dead(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        status = self.host.probe()
        self.assertIs(status["runner_alive"], False)
        self.assertIs(status["executor_alive"], False,
                      "an empty process group is the proof that the session is gone")
        self.assertEqual(status["executor_pgid"], pgid)

    def test_swrm_status_writer_supplies_group_for_remote_probe(self):
        loader = importlib.machinery.SourceFileLoader("swrm_status_test", str(ROOT / "swrm/bin/swrm"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        swrm = importlib.util.module_from_spec(spec)
        loader.exec_module(swrm)
        pgid, child = live_group()
        self.addCleanup(stop, child)
        swrm.set_status(str(self.host.run_dir), runner_pid=DEAD_PID,
                        executor_pid=child.pid, events=str(self.host.events), state="running")
        recorded = json.loads((self.host.run_dir / "status.json").read_text())
        self.assertEqual(recorded["executor_pgid"], pgid)
        self.assertIsNone(self.host.probe()["executor_alive"])
        stop(child)
        verdict = self.host.probe()
        self.assertIs(verdict["runner_alive"], False)
        self.assertIs(verdict["executor_alive"], False)
        with patch.object(swrm.os, "getpgid", side_effect=ProcessLookupError):
            swrm.set_status(str(self.host.run_dir), executor_pid=child.pid)
        recorded = json.loads((self.host.run_dir / "status.json").read_text())
        self.assertNotIn("executor_pgid", recorded)
        self.assertIsNone(self.host.probe()["executor_alive"])

    def test_a_process_group_with_a_living_member_is_unknown(self):
        pgid, child = live_group()
        self.addCleanup(stop, child)
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        status = self.host.probe()
        self.assertIs(status["runner_alive"], False)
        self.assertIsNone(status["executor_alive"],
                          "a group with a member is not proof that the session died")

    def test_a_group_with_a_member_stays_unknown_even_with_a_holder(self):
        """A group with a member is unknown - never alive, never dead.

        The group is the gate of the proof: while it has a member the session
        cannot be called dead, whatever else the host shows (Refs #1663)."""
        pgid, child = live_group()
        self.addCleanup(stop, child)
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        self.host.hold_output()
        status = self.host.probe()
        self.assertIsNone(status["executor_alive"],
                          "a group with a member is unknown, whatever else the host shows")

    def test_a_status_without_the_process_group_is_unknown(self):
        """An old status file that never recorded the group cannot prove a death."""
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID)
        status = self.host.probe()
        self.assertIsNone(status["executor_alive"],
                          "without a recorded process group the session stays unknown")

    def test_a_status_without_the_executor_pid_is_unknown(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=DEAD_PID, executor_pgid=pgid)
        status = self.host.probe()
        self.assertIsNone(status["executor_alive"],
                          "without an executor pid the session stays unknown")

    def test_a_live_runner_is_alive(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=os.getpid(), executor_pid=DEAD_PID, executor_pgid=pgid)
        status = self.host.probe()
        self.assertIs(status["runner_alive"], True,
                      "a living runner keeps the run in flight")

    def test_invalid_or_unverifiable_runner_never_releases_the_lane(self):
        pgid = empty_group()
        for pid in (None, False, 0, -1, "bad"):
            with self.subTest(pid=pid):
                self.host.write_status(runner_pid=pid, executor_pid=DEAD_PID,
                                       executor_pgid=pgid)
                self.assertIsNone(self.host.probe()["runner_alive"])
                self.assertIs(self.host.cleanup(), False)
                self.assertEqual(self.host.lane_state()["current_run"], RUN)
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID,
                               executor_pgid=pgid)
        with patch("os.kill", side_effect=OSError("cannot inspect runner")):
            self.assertIsNone(self.host.probe()["runner_alive"])
            self.assertIs(self.host.cleanup(), False)
        self.assertEqual(self.host.lane_state()["current_run"], RUN)

    def test_a_failed_group_check_is_unknown(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        with patch("os.killpg", side_effect=OSError("the process group cannot be checked")):
            status = self.host.probe()
        self.assertIsNone(status["executor_alive"],
                          "a group that cannot be checked is not proof that the session died")

    def test_invalid_groups_and_permission_failures_are_unknown(self):
        for pgid in (None, False, 0, -1, "bad"):
            with self.subTest(pgid=pgid):
                self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID,
                                       executor_pgid=pgid)
                with patch("os.killpg") as signal:
                    self.assertIsNone(self.host.probe()["executor_alive"])
                    self.assertIs(self.host.cleanup(), False)
                    signal.assert_not_called()
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID,
                               executor_pgid=empty_group())
        with patch("os.killpg", side_effect=PermissionError("not permitted")):
            self.assertIsNone(self.host.probe()["executor_alive"])
            self.assertIs(self.host.cleanup(), False)
        self.assertEqual(self.host.lane_state()["current_run"], RUN)

    def test_an_unreadable_process_table_is_unknown(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        self.host.proc = self.host.root / "missing-proc"
        status = self.host.probe()
        self.assertIsNone(status["executor_alive"],
                          "a host that cannot be inspected does not prove a death")

    def test_a_process_holding_the_output_of_the_run_keeps_the_session_alive(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        self.host.hold_output()
        status = self.host.probe()
        self.assertIs(status["executor_alive"], True,
                      "a process that still writes the output of the run is alive")


class DetachedChildTests(unittest.TestCase):
    """A child that left the process group of its executor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ad-detached-child-")
        self.addCleanup(self.tmp.cleanup)
        self.host = FakeHost(self.tmp.name)

    def detached_child(self, keep_output=False):
        """A child that calls setsid(), closes the output files and keeps working.

        With `keep_output` the child keeps the output file of the run open: it
        left the process group of its executor, but it still writes the output
        of the run."""
        marker = Path(self.tmp.name) / "child.pid"
        child_code = ("import os,sys,time; open(sys.argv[1], 'w').write(str(os.getpid())); "
                      "time.sleep(30)")
        if keep_output:
            executor_code = (
                "import subprocess,sys; "
                "out=open(sys.argv[3],'a'); "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
                "stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)")
            argv = [sys.executable, "-c", executor_code, child_code, str(marker),
                    str(self.host.events)]
        else:
            executor_code = (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL, start_new_session=True)")
            argv = [sys.executable, "-c", executor_code, child_code, str(marker)]
        executor = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, cwd=self.tmp.name)
        executor.wait(timeout=30)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "the detached child should start")
        child_pid = int(marker.read_text())

        def kill_child():
            try:
                os.kill(child_pid, 15)
            except OSError:
                pass

        self.addCleanup(kill_child)
        return executor.pid, child_pid

    def test_a_detached_child_without_a_recorded_process_group_is_unknown(self):
        executor_pid, child_pid = self.detached_child()
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            self.fail("the detached child has to outlive its executor for this test")
        # The state of an old run records no process group: nothing can rule out
        # the detached child, so the session must not be called dead.
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=executor_pid,
                               workspace=self.tmp.name)
        status = self.host.probe()
        self.assertIsNone(status["executor_alive"],
                          "a detached child without a recorded process group stays unknown")

    def test_the_workspace_of_the_run_never_proves_that_the_session_is_dead(self):
        """The workspace scan cannot see a child that works outside of it."""
        executor_pid, child_pid = self.detached_child()
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            self.fail("the detached child has to outlive its executor for this test")
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=executor_pid,
                               workspace=self.tmp.name)
        status = self.host.probe()
        self.assertIsNot(status["executor_alive"], False,
                         "the workspace of the run is not a proof that the session died")

    def test_the_workspace_scan_is_gone(self):
        """The workspace of the run is not a liveness source anymore (Refs #1663)."""
        namespace = {}
        exec(AD.REMOTE_STATE_HOME + AD.REMOTE_LIVENESS, namespace)
        self.assertNotIn("_workspace_process_alive", namespace)

    def test_a_detached_child_that_writes_the_output_of_the_run_is_not_dead(self):
        """A child that left the process group but writes the output of the run
        keeps the session alive.

        The group of the executor is empty - the executor itself is gone - but
        the child still holds the output file of the run open.  The run must not
        be released: it would be dispatched a second time (Refs #1663)."""
        host = self.host
        # the host has to show its own processes: the child of the test holds
        # the output file of the run open
        host.real_proc = True
        executor_pid, child_pid = self.detached_child(keep_output=True)
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            self.fail("the detached child has to outlive its executor for this test")
        # the executor was the leader of its own group: the group is empty now
        host.write_status(runner_pid=DEAD_PID, executor_pid=executor_pid,
                          executor_pgid=executor_pid)
        status = host.probe()
        self.assertIsNot(status["executor_alive"], False,
                         "a child that still writes the output of the run is not a dead session")
        namespace = {}
        exec(AD.REMOTE_STATE_HOME + AD.REMOTE_LIVENESS, namespace)
        if namespace["_output_holders"](str(host.events)) is not None:
            self.assertIs(status["executor_alive"], True,
                          "the child that writes the output of the run keeps the session alive")
        self.assertIs(host.cleanup(), False)
        self.assertEqual(host.lane_state()["current_run"], RUN,
                         "the lane of a run that is still written keeps its claim")


class RemoteCleanupTests(unittest.TestCase):
    """The remote cleanup releases a lane only after the proof."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ad-cleanup-")
        self.addCleanup(self.tmp.cleanup)
        self.host = FakeHost(self.tmp.name)

    def test_the_cleanup_releases_a_lane_whose_process_group_is_empty(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        self.assertIs(self.host.cleanup(), True)
        state = self.host.lane_state()
        self.assertEqual(state["state"], "idle")
        self.assertIsNone(state["current_run"])

    def test_the_cleanup_keeps_a_lane_whose_process_group_has_a_member(self):
        pgid, child = live_group()
        self.addCleanup(stop, child)
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        self.assertIs(self.host.cleanup(), False)
        self.assertEqual(self.host.lane_state()["current_run"], RUN,
                         "a lane with a living session keeps its run")

    def test_the_cleanup_keeps_a_lane_whose_output_is_still_written(self):
        """A process that still writes the output of the run keeps the lane.

        The process group of the executor is empty, but a child that left the
        group and kept the output file open is still working on the run."""
        pgid = empty_group()
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        self.host.hold_output()
        self.assertIs(self.host.cleanup(), False)
        self.assertEqual(self.host.lane_state()["current_run"], RUN,
                         "a run whose output is still written keeps its lane")

    def test_the_cleanup_keeps_a_lane_without_a_recorded_process_group(self):
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID)
        self.assertIs(self.host.cleanup(), False)
        self.assertEqual(self.host.lane_state()["current_run"], RUN)

    def test_the_cleanup_keeps_a_lane_when_the_group_check_fails(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        with patch("os.killpg", side_effect=OSError("the process group cannot be checked")):
            self.assertIs(self.host.cleanup(), False)
        self.assertEqual(self.host.lane_state()["current_run"], RUN)

    def test_the_cleanup_keeps_a_lane_of_a_living_runner(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=os.getpid(), executor_pid=DEAD_PID, executor_pgid=pgid)
        self.assertIs(self.host.cleanup(), False)
        self.assertEqual(self.host.lane_state()["current_run"], RUN)


class ReconcileTests(unittest.TestCase):
    """The control plane releases a run only after the remote proof."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ad-reconcile-")
        self.addCleanup(self.tmp.cleanup)
        self.host = FakeHost(self.tmp.name)
        self.module = load_module("autodispatch_executor_alive_reconcile")
        self.module.log = lambda *args, **kwargs: None
        self.edits = []
        self.module.gh = self.github
        self.module.sh = self.remote
        self.st = {"run": RUN, "issue": ISSUE, "at": "2000-01-01 00:00:00"}

    def github(self, args, **kwargs):
        if args[1] == "view":
            return 0, json.dumps({"state": "OPEN", "labels": [{"name": "in-progress"}]}), ""
        self.edits.append(args)
        return 0, "", ""

    def remote(self, cmd, timeout=120, inp=None):
        """The ssh call of the dispatcher: the cleanup runs on the fake host."""
        if inp == self.module.REMOTE_CLEAR_ORPHAN:
            return 0, json.dumps(self.host.cleanup()) + "\n", ""
        return 0, "", ""

    def reconcile(self):
        return self.module.reconcile(LANE, {"host": "example.com"}, self.st,
                                     self.host.probe(), {})

    def test_an_empty_group_and_a_dead_runner_release_the_lane_and_requeue_the_card(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        self.assertTrue(self.reconcile(),
                        "a dead session with an empty process group must be released")
        state = self.host.lane_state()
        self.assertEqual(state["state"], "idle")
        self.assertIsNone(state["current_run"])
        self.assertTrue(self.edits, "the card of the lost run must be re-queued")

    def test_a_living_member_of_the_group_keeps_the_run_and_the_card(self):
        pgid, child = live_group()
        self.addCleanup(stop, child)
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        self.assertFalse(self.reconcile(),
                         "a session that may still run must never be released")
        self.assertEqual(self.host.lane_state()["current_run"], RUN)
        self.assertEqual(self.edits, [], "the card of a living session must not be re-queued")

    def test_a_status_without_the_process_group_keeps_the_run_and_the_card(self):
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID)
        self.assertFalse(self.reconcile(),
                         "a session that cannot be verified must never be released")
        self.assertEqual(self.host.lane_state()["current_run"], RUN)
        self.assertEqual(self.edits, [])

    def test_a_failed_group_check_keeps_the_run_and_the_card(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=pgid)
        with patch("os.killpg", side_effect=OSError("the process group cannot be checked")):
            self.assertFalse(self.reconcile(),
                             "a session that cannot be verified must never be released")
        self.assertEqual(self.host.lane_state()["current_run"], RUN)
        self.assertEqual(self.edits, [])

    def test_a_living_runner_keeps_the_run_and_the_card(self):
        pgid = empty_group()
        self.host.write_status(runner_pid=os.getpid(), executor_pid=DEAD_PID, executor_pgid=pgid)
        self.assertFalse(self.reconcile(),
                         "a run with a living runner must never be released")
        self.assertEqual(self.host.lane_state()["current_run"], RUN)
        self.assertEqual(self.edits, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
