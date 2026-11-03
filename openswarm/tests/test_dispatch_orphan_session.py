"""Orphan session reconciliation: an in-flight run is requeued only after its whole session is proven dead.

Refs #1663: after a host regression the box lost the runner of a dispatched run.  The control plane
must verify the old session (runner, executor leader, its process group, every recorded descendant and
anything still holding the executor output file) before it requeues the issue — a requeue without that
proof would run the same branch twice.
"""

import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import _load

HERE = Path(__file__).resolve().parent
DISPATCH = HERE.parent / "lib" / "dispatch"

_spec = importlib.util.spec_from_file_location("orphan_session_engine_under_test",
                                               str(DISPATCH / "engine.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

DEAD_PID = 99999999          # no process owns this pid


def probe(**env):
    """Exec the remote session probe and return its namespace."""
    scope = {"__name__": "openswarm_session_probe"}
    with mock.patch.dict(os.environ, env):
        exec(E.SESSION_PROBE, scope)
    return scope


class SessionVerdictTests(unittest.TestCase):
    """session_state: alive | dead | unknown for the whole executor session of one run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orphan-session-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "runs" / "old-run"
        self.run_dir.mkdir(parents=True)
        self.events = self.run_dir / "events.1.pi.jsonl"
        self.events.write_text("")

    def fake_proc(self, holders=()):
        """A /proc tree whose pids hold the given files open."""
        root = self.root / "proc"
        root.mkdir(exist_ok=True)
        for index, target in enumerate(holders):
            pid = str(1000 + index)
            (root / pid / "fd").mkdir(parents=True)
            os.symlink(str(target), str(root / pid / "fd" / "1"))
        return str(root)

    def test_live_runner_keeps_the_session_alive(self):
        scope = probe()
        self.assertEqual(scope["session_state"]({"runner_pid": os.getpid()}, str(self.run_dir)), "alive")

    def test_live_executor_leader_keeps_the_session_alive(self):
        scope = probe()
        status = {"runner_pid": DEAD_PID, "executor_pid": os.getpid(), "executor_pgid": os.getpgrp()}
        self.assertEqual(scope["session_state"](status, str(self.run_dir)), "alive")

    def test_surviving_process_group_keeps_the_session_alive(self):
        """The executor leader exited, but its process group still has a member."""
        scope = probe()
        status = {"runner_pid": DEAD_PID, "executor_pid": DEAD_PID, "executor_pgid": os.getpgrp()}
        self.assertEqual(scope["session_state"](status, str(self.run_dir)), "alive")

    def test_recorded_descendant_keeps_the_session_alive(self):
        scope = probe()
        status = {"runner_pid": DEAD_PID, "executor_pid": DEAD_PID, "executor_pgid": DEAD_PID,
                  "executor_descendants": [{"pid": os.getpid(), "pgid": os.getpgrp(), "identity": "x"}]}
        self.assertEqual(scope["session_state"](status, str(self.run_dir)), "alive")

    def test_recorded_dead_session_is_dead(self):
        scope = probe(OPEN_SWARM_PROC_ROOT=self.fake_proc())
        status = {"runner_pid": DEAD_PID, "executor_pid": DEAD_PID, "executor_pgid": DEAD_PID}
        self.assertEqual(scope["session_state"](status, str(self.run_dir)), "dead")

    def test_unrecorded_detached_output_holder_with_recorded_dead_executor(self):
        scope = probe(OPEN_SWARM_PROC_ROOT=self.fake_proc([self.events.resolve()]))
        status = {"runner_pid": DEAD_PID, "executor_pid": DEAD_PID, "executor_pgid": DEAD_PID}
        self.assertEqual(scope["session_state"](status, str(self.run_dir)), "alive")

    def test_invalid_runner_is_unknown(self):
        scope = probe(OPEN_SWARM_PROC_ROOT=self.fake_proc())
        for pid in (None, "invalid", 0, -1):
            with self.subTest(pid=pid):
                self.assertEqual(scope["session_state"]({"runner_pid": pid}, str(self.run_dir)), "unknown")

    def test_unreadable_fd_directory_and_link_are_unknown(self):
        root = Path(self.fake_proc())
        (root / "1234" / "fd").mkdir(parents=True)
        scope = probe(OPEN_SWARM_PROC_ROOT=str(root))
        real_listdir = os.listdir
        def listdir(path):
            if str(path).endswith("/1234/fd"):
                raise OSError("cannot inspect")
            return real_listdir(path)
        with mock.patch.object(os, "listdir", side_effect=listdir):
            self.assertEqual(scope["session_state"]({"runner_pid": DEAD_PID}, str(self.run_dir)), "unknown")
        (root / "1234" / "fd" / "1").touch()
        with mock.patch.object(os, "readlink", side_effect=OSError("cannot inspect")):
            self.assertEqual(scope["session_state"]({"runner_pid": DEAD_PID}, str(self.run_dir)), "unknown")

    def test_run_without_an_executor_output_never_started_an_executor(self):
        """The runner died before it spawned the executor: nothing can be running."""
        scope = probe()
        run_dir = self.root / "runs" / "no-executor"
        run_dir.mkdir()
        self.assertEqual(scope["session_state"]({"runner_pid": DEAD_PID}, str(run_dir)), "dead")

    def test_executor_that_outlived_its_runner_keeps_the_session_alive(self):
        """The runner died between spawning the executor and recording its pid."""
        scope = probe(OPEN_SWARM_PROC_ROOT=self.fake_proc([self.events.resolve()]))
        self.assertEqual(scope["session_state"]({"runner_pid": DEAD_PID}, str(self.run_dir)), "alive")

    def test_missing_executor_pid_without_a_holder_is_dead(self):
        scope = probe(OPEN_SWARM_PROC_ROOT=self.fake_proc())
        self.assertEqual(scope["session_state"]({"runner_pid": DEAD_PID}, str(self.run_dir)), "dead")

    def test_unreadable_process_table_stays_unknown(self):
        """An unverifiable session is never treated as dead."""
        scope = probe(OPEN_SWARM_PROC_ROOT=str(self.root / "missing-proc"))
        self.assertEqual(scope["session_state"]({"runner_pid": DEAD_PID}, str(self.run_dir)), "unknown")


class RemoteStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orphan-status-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.run_dir = self.home / "runs" / "old-run"
        self.run_dir.mkdir(parents=True)

    def status(self, **fields):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["remote", "M7", "old-run", str(self.home)]), \
                contextlib.redirect_stdout(output):
            exec(E.REMOTE_STATUS, {})
        return json.loads(output.getvalue())

    def test_remote_status_reports_the_session_verdict(self):
        (self.run_dir / "status.json").write_text(json.dumps({"runner_pid": DEAD_PID,
                                                                "executor_pid": DEAD_PID,
                                                                "executor_pgid": DEAD_PID}))
        result = self.status()
        self.assertEqual(result["session"], "dead")
        self.assertFalse(result["runner_alive"])
        self.assertFalse(result["executor_alive"])
        self.assertFalse(result["executor_group_alive"])

    def test_remote_status_keeps_a_live_executor_session_visible(self):
        (self.run_dir / "status.json").write_text(json.dumps({"runner_pid": DEAD_PID,
                                                                "executor_pid": os.getpid(),
                                                                "executor_pgid": os.getpgrp()}))
        result = self.status()
        self.assertEqual(result["session"], "alive")
        self.assertTrue(result["executor_alive"])
        self.assertTrue(result["executor_group_alive"])

    def test_remote_status_without_an_executor_record_is_unknown(self):
        (self.run_dir / "status.json").write_text(json.dumps({"runner_pid": DEAD_PID}))
        result = self.status()
        self.assertIsNone(result["executor_alive"])
        self.assertIsNone(result["executor_group_alive"])


class ReconcileTests(unittest.TestCase):
    """reconcile() requeues only a provably dead session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orphan-reconcile-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        raw = {"repo": "acme/project", "local_repo": str(self.root),
               "state_dir": str(self.root / "state"), "local_hosts": ["box"],
               "ssh": {"control_dir": str(self.root / "cm")}}
        cfg_path = self.root / "config.json"
        cfg_path.write_text(json.dumps(raw))
        self.cfg = E.load_config(str(cfg_path))
        self.calls = []
        self.output = io.StringIO()
        self.dispatcher = E.Dispatcher(self.cfg, runner=self.fake_run, out=self.output, clock=time.time)
        self.lane = {"host": "box", "backend": "pi"}
        self.st = {"issue": 1503, "run": "old-run",
                   "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))}

    def fake_run(self, command, **kwargs):
        text = command if isinstance(command, str) else " ".join(map(str, command))
        self.calls.append(text)
        if kwargs.get("inp") == E.REMOTE_CLEAR_ORPHAN:
            return 0, "cleared\n", ""
        if "issue view" in text:
            return 0, json.dumps({"state": "OPEN", "labels": [{"name": "in-progress"}]}), ""
        return 0, "", ""

    def test_live_session_is_never_requeued(self):
        remote = {"run": "running", "lane": "idle", "current_run": None, "session": "alive"}
        self.assertFalse(self.dispatcher.reconcile("M7", self.lane, self.st, remote, {}))
        self.assertEqual(self.calls, [])

    def test_unverifiable_session_is_never_requeued(self):
        remote = {"run": "running", "lane": "idle", "current_run": None, "session": "unknown"}
        self.assertFalse(self.dispatcher.reconcile("M7", self.lane, self.st, remote, {}))
        self.assertEqual(self.calls, [])

    def test_dead_session_on_an_idle_lane_is_requeued(self):
        remote = {"run": "running", "lane": "idle", "current_run": None, "session": "dead"}
        self.assertTrue(self.dispatcher.reconcile("M7", self.lane, self.st, remote, {}))
        self.assertTrue(any("issue edit 1503" in call for call in self.calls))

    def test_busy_lane_is_cleared_before_the_issue_is_requeued(self):
        remote = {"run": "running", "lane": "busy", "current_run": "old-run", "session": "dead"}
        self.assertTrue(self.dispatcher.reconcile("M7", self.lane, self.st, remote, {}))
        self.assertTrue(any("python3" in call and "old-run" in call for call in self.calls))
        self.assertTrue(any("issue edit 1503" in call for call in self.calls))

    def test_a_refused_cleanup_keeps_the_run_in_flight(self):
        def refuse(command, **kwargs):
            text = command if isinstance(command, str) else " ".join(map(str, command))
            self.calls.append(text)
            if kwargs.get("inp") == E.REMOTE_CLEAR_ORPHAN:
                return 0, "unknown\n", ""
            return 0, "", ""

        self.dispatcher.runner = refuse
        remote = {"run": "running", "lane": "busy", "current_run": "old-run", "session": "dead"}
        self.assertFalse(self.dispatcher.reconcile("M7", self.lane, self.st, remote, {}))
        self.assertFalse(any("issue edit" in call for call in self.calls))


class RemoteCleanupTests(unittest.TestCase):
    """The remote cleanup releases a lane that still points at a dead run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orphan-cleanup-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.lane_dir = self.home / "lanes" / "M7"
        self.run_dir = self.home / "runs" / "old-run"
        self.lane_dir.mkdir(parents=True)
        self.run_dir.mkdir(parents=True)
        self.lane_state = self.lane_dir / "state.json"
        self.lane_state.write_text(json.dumps({"state": "busy", "current_run": "old-run"}))

    def write_status(self, **fields):
        (self.run_dir / "status.json").write_text(json.dumps(fields))

    def clear(self, run="old-run", lane="M7"):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["clear", lane, run, str(self.home)]), \
                contextlib.redirect_stdout(output):
            try:
                exec(E.REMOTE_CLEAR_ORPHAN, {})
            except SystemExit:
                pass
        return output.getvalue().strip()

    def test_cleanup_releases_a_lane_whose_run_is_dead(self):
        self.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=DEAD_PID)
        self.assertEqual(self.clear(), "cleared")
        state = json.loads(self.lane_state.read_text())
        self.assertEqual(state["state"], "idle")
        self.assertIsNone(state["current_run"])

    def test_cleanup_uses_the_shared_lane_state_lock(self):
        self.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=DEAD_PID)
        self.assertEqual(self.clear(), "cleared")
        self.assertTrue((self.lane_dir / "state.lock").exists())

    def test_cleanup_refuses_a_live_session(self):
        self.write_status(runner_pid=os.getpid())
        self.assertEqual(self.clear(), "alive")
        self.assertEqual(json.loads(self.lane_state.read_text())["current_run"], "old-run")

    def test_cleanup_refuses_a_session_it_cannot_verify(self):
        """An executor output file exists but the process table cannot be read: stay unknown."""
        self.write_status(runner_pid=DEAD_PID)
        (self.run_dir / "events.1.pi.jsonl").write_text("")
        with mock.patch.dict(os.environ, {"OPEN_SWARM_PROC_ROOT": str(self.root / "missing-proc")}), \
                mock.patch.object(sys, "argv", ["clear", "M7", "old-run", str(self.home)]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    exec(E.REMOTE_CLEAR_ORPHAN, {})
        self.assertEqual(output.getvalue().strip(), "unknown")
        self.assertEqual(json.loads(self.lane_state.read_text())["current_run"], "old-run")

    def test_cleanup_keeps_a_concurrent_reassignment(self):
        self.lane_state.write_text(json.dumps({"state": "busy", "current_run": "new-run"}))
        self.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=DEAD_PID)
        self.assertEqual(self.clear(), "changed")
        self.assertEqual(json.loads(self.lane_state.read_text())["current_run"], "new-run")

    def test_cleanup_does_not_clobber_a_dispatch_that_won_the_lane(self):
        """A dispatch that took the lane while the cleanup waited must keep the lane."""
        self.write_status(runner_pid=DEAD_PID, executor_pid=DEAD_PID, executor_pgid=DEAD_PID)
        lock_path = self.lane_dir / "state.lock"
        lock_path.touch()
        handle = open(lock_path, "a+")
        fcntl.flock(handle, fcntl.LOCK_EX)
        proc = subprocess.Popen([sys.executable, "-", "M7", "old-run", str(self.home)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            proc.stdin.write(E.REMOTE_CLEAR_ORPHAN)
            proc.stdin.close()
            time.sleep(0.5)                       # the cleanup is now waiting for the lock
            self.assertIsNone(proc.poll())        # it did not clear the lane behind our back
            self.lane_state.write_text(json.dumps({"state": "busy", "current_run": "new-run"}))
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        out = proc.stdout.read()
        proc.stderr.read()
        proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(out.strip().splitlines()[-1], "changed")
        self.assertEqual(json.loads(self.lane_state.read_text())["current_run"], "new-run")


class LaneStateLockTests(unittest.TestCase):
    """Every lane state writer shares one stable lock with the orphan cleanup."""

    def setUp(self):
        self.xs = _load.load()

    def test_lane_write_state_uses_the_shared_lock_file(self):
        lane = "M7"
        self.xs.lane_write_state(lane, state="busy", current_run="r1")
        self.assertTrue(os.path.exists(self.xs.lane_state_lock_path(lane)))
        with open(self.xs.lane_state_path(lane)) as stream:
            self.assertEqual(json.load(stream)["current_run"], "r1")

    def test_lane_write_state_waits_for_a_writer_that_holds_the_lock(self):
        lane = "M8"
        lock_path = self.xs.lane_state_lock_path(lane)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        handle = open(lock_path, "a+")
        fcntl.flock(handle, fcntl.LOCK_EX)
        done = []

        def writer():
            self.xs.lane_write_state(lane, state="busy", current_run="r2")
            done.append(True)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            thread.join(timeout=1)
            self.assertEqual(done, [])            # blocked on the shared lock
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        thread.join(timeout=10)
        self.assertEqual(done, [True])


if __name__ == "__main__":
    unittest.main()
