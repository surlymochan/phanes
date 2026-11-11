"""Busy orphan recovery without real network or GitHub operations."""
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
# A process group that no process belongs to: the proof that the executor of a
# run left no member behind (Refs #1663).
DEAD_PGID = 2147483646
# A pid that no process owns.
DEAD_PID = 2147483647


class BusyOrphanTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "autodispatch_orphan_busy", ROOT / "tools/lane-autodispatch/autodispatch.py")
        self.ad = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ad)
        self.st = {"run": "old-run", "issue": 42, "at": "2000-01-01 00:00:00"}
        self.state = {"D1": self.st}
        self.status = {"run": "running", "lane": "busy", "current_run": "old-run",
                       "runner_alive": False, "executor_alive": False, "executor_pid": 54321}
        self.order = []
        self.ad.log = lambda *args: None
        self.ad.sh = lambda *args, **kwargs: self.order.append("cleanup") or (0, "true\n", "")
        self.ad.gh = self.github

    def github(self, args, **kwargs):
        self.order.append(args[1])
        if args[1] == "view":
            return 0, json.dumps({"state": "OPEN", "labels": [{"name": "in-progress"}]}), ""
        return 0, "", ""

    def reconcile(self):
        return self.ad.reconcile("D1", {"host": "example.com"}, self.st, self.status, self.state)

    def test_dead_busy_cleanup_precedes_requeue(self):
        self.assertTrue(self.reconcile())
        self.assertEqual(self.order, ["cleanup", "view", "edit"])

    def test_alive_unknown_and_young_do_not_touch_remote_or_issue(self):
        for alive in (True, None, "false"):
            with self.subTest(alive=alive):
                self.status["runner_alive"] = alive
                self.assertFalse(self.reconcile())
                self.assertEqual(self.order, [])
        self.status["runner_alive"] = False
        with patch.object(self.ad, "age_s", return_value=1):
            self.assertFalse(self.reconcile())
        self.assertEqual(self.order, [])

    def test_failed_cleanup_keeps_local_record_and_retries(self):
        for reply in ((1, "", "failure"), (0, "false", ""), (0, "invalid", "")):
            with self.subTest(reply=reply):
                self.ad.sh = lambda *args, **kwargs: reply
                self.assertFalse(self.reconcile())
                self.assertEqual(self.order, [])
                self.assertIs(self.state["D1"], self.st)
        self.ad.sh = lambda *args, **kwargs: (0, "true", "")
        self.assertTrue(self.reconcile())

    def test_requeue_failure_keeps_record_and_idle_retry_succeeds(self):
        self.ad.gh = lambda *args, **kwargs: (1, "", "unavailable")
        self.assertFalse(self.reconcile())
        self.assertEqual(self.order, ["cleanup"])
        self.assertIs(self.state["D1"], self.st)
        self.status.update(lane="idle", current_run=None)
        self.ad.gh = self.github
        self.assertTrue(self.reconcile())
        self.assertEqual(self.order, ["cleanup", "view", "edit"])

    def test_label_update_failure_preserves_record(self):
        def gh(args, **kwargs):
            if args[1] == "edit":
                self.order.append("edit-failed")
                return 1, "", "unavailable"
            return self.github(args)
        self.ad.gh = gh
        self.assertFalse(self.reconcile())
        self.assertEqual(self.order, ["cleanup", "view", "edit-failed"])
        self.assertIs(self.state["D1"], self.st)

    def test_different_busy_run_is_never_cleared(self):
        self.status["current_run"] = "new-run"
        self.assertFalse(self.reconcile())
        self.assertEqual(self.order, [])

    def run_cleanup(self, lane_state, pid_effect=ProcessLookupError, before_lock=None,
                    pid=12345, completed=False, fail_write=False, executor_alive=False,
                    executor_pid=DEAD_PID, omit_executor_alive=False, attempt_started=False,
                    pgid=DEAD_PGID, group="empty", holder=False):
        scratch = ROOT / ".swrm-tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            home = Path(tmp)
            lane = home / "lanes/D1"
            run = home / "runs/old-run"
            lane.mkdir(parents=True)
            run.mkdir(parents=True)
            state_path = lane / "state.json"
            state_path.write_text(json.dumps(lane_state))
            events = run / "events.jsonl"
            events.write_text("")
            status = {"runner_pid": pid, "executor_pid": executor_pid,
                      "executor_pgid": pgid, "events": str(events)}
            if attempt_started: status["attempt_started"] = 1
            if not omit_executor_alive:
                status["executor_alive"] = executor_alive
            (run / "status.json").write_text(json.dumps(status))
            if completed:
                (run / "result.json").write_text("{}")
            real_flock = fcntl.flock
            real_replace = os.replace
            def flock(fd, operation):
                self.assertEqual(Path(fd.name), lane / "state.json.lock")
                if before_lock:
                    # A concurrent normal lane writer owns the stable sidecar.
                    waiting = threading.Event()
                    with open(lane / "state.json.lock", "a") as writer:
                        real_flock(writer, fcntl.LOCK_EX)
                        def acquire():
                            waiting.set()
                            real_flock(fd, operation)
                        contender = threading.Thread(target=acquire)
                        contender.start()
                        self.assertTrue(waiting.wait(5))
                        state_path.write_text(json.dumps(before_lock))
                        real_flock(writer, fcntl.LOCK_UN)
                    contender.join(5)
                    self.assertFalse(contender.is_alive())
                else:
                    real_flock(fd, operation)
            def replace(source, target):
                if fail_write:
                    raise OSError("synthetic write failure")
                return real_replace(source, target)
            out = io.StringIO()
            proc = home / "proc"
            proc.mkdir()
            environment = {"OPENSWARM_HOME": tmp, "OPEN_SWARM_PROC_ROOT": str(proc)}
            if holder:
                # A process that still writes the output of the run: it left the
                # process group of the executor, but it holds the file open.
                proc = home / "proc"
                (proc / "4242" / "fd").mkdir(parents=True)
                os.symlink(str(events.resolve()), str(proc / "4242" / "fd" / "1"))
                environment["OPEN_SWARM_PROC_ROOT"] = str(proc)
            with patch.dict(os.environ, environment), \
                    patch("sys.argv", ["cleanup", "D1", "old-run"]), \
                    patch("os.kill", side_effect=pid_effect), \
                    patch("os.killpg", side_effect=self.group_effect(group)), \
                    patch("fcntl.flock", side_effect=flock), \
                    patch("os.replace", side_effect=replace), \
                    contextlib.redirect_stdout(out):
                exec(self.ad.REMOTE_CLEAR_ORPHAN, {})
            return json.loads(out.getvalue()), json.loads(state_path.read_text())

    @staticmethod
    def group_effect(group):
        """The result of the `kill -0 -pgid` check of the executor process group."""
        if group == "empty": return ProcessLookupError()
        if group == "live": return None
        if group == "fail": return OSError("the process group cannot be checked")
        raise AssertionError(group)

    def run_remote_status(self, runner_pid, executor_pid, hold_events=False,
                          execution_started=None, attempt_started=None,
                          pgid=DEAD_PGID, group="empty"):
        scratch = ROOT / ".swrm-tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            home = Path(tmp)
            (home / "lanes/D1").mkdir(parents=True)
            run = home / "runs/old-run"
            run.mkdir(parents=True)
            event_path = run / "events.jsonl"
            event_path.write_text("")
            status = {
                "runner_pid": runner_pid, "executor_pid": executor_pid,
                "executor_pgid": pgid, "events": str(event_path), "state": "running",
                "executor_alive": False}
            if execution_started is not None:
                status["execution_started"] = execution_started
            if attempt_started is not None:
                status["attempt_started"] = attempt_started
            (run / "status.json").write_text(json.dumps(status))
            proc = home / "proc"
            proc.mkdir()
            if hold_events:
                (proc / "4242" / "fd").mkdir(parents=True)
                os.symlink(str(event_path), proc / "4242" / "fd" / "1")
            out = io.StringIO()
            event_handle = open(event_path) if hold_events else None
            try:
                with patch.dict(os.environ, {"OPENSWARM_HOME": tmp, "OPEN_SWARM_PROC_ROOT": str(proc)}), \
                        patch("sys.argv", ["status", "D1", "old-run"]), \
                        patch("os.killpg", side_effect=self.group_effect(group)), \
                        contextlib.redirect_stdout(out):
                    exec(self.ad.REMOTE_STATUS, {})
            finally:
                if event_handle:
                    event_handle.close()
            return json.loads(out.getvalue())

    def test_remote_status_reports_dead_session_and_detects_unrecorded_executor(self):
        dead_pid = 2147483647
        dead = self.run_remote_status(dead_pid, dead_pid)
        self.assertIs(dead.get("runner_alive"), False)
        self.assertIs(dead.get("executor_alive"), False,
                      "the remote probe must expose the executor proof used by reconciliation")

        # Actual legacy swrm status records PIDs but no PGID. Even with both
        # PIDs gone and no output holder, that format must stay unknown.
        legacy = self.run_remote_status(dead_pid, dead_pid, pgid=None)
        self.assertIs(legacy.get("runner_alive"), False)
        self.assertIsNone(legacy.get("executor_alive"))
        held = self.run_remote_status(dead_pid, dead_pid, hold_events=True)
        self.assertIs(held.get("executor_alive"), True,
                      "an unrecorded process holding the run output keeps the session alive")

    def test_remote_status_proves_reaped_runner_and_executor_dead_after_execution(self):
        runner = subprocess.Popen([sys.executable, "-c", "pass"])
        executor = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        runner.wait(timeout=5)
        executor.wait(timeout=5)
        fields = {"execution_started": "2026-11-09T00:00:00Z",
                  "attempt_started": "2026-11-09T00:00:01Z"}
        legacy = self.run_remote_status(runner.pid, executor.pid, pgid=None, **fields)
        self.assertIsNone(legacy.get("executor_alive"),
                          "reaped PIDs and timestamps cannot replace a recorded group")
        # Probe a real group, not a mocked ESRCH result.
        with patch.object(self, "group_effect", return_value=os.killpg):
            status = self.run_remote_status(runner.pid, executor.pid,
                                            pgid=executor.pid, **fields)
        self.assertIs(status.get("runner_alive"), False)
        self.assertIs(status.get("executor_alive"), False,
                      "timestamps describe execution history; they must not hide a verified dead session")

    def test_remote_status_keeps_an_invalid_runner_pid_unknown(self):
        status = self.run_remote_status(None, 2147483647)
        self.assertIsNone(status.get("runner_alive"))
        self.assertEqual(self.ad.runner_liveness(status), "unknown")

    def test_executor_session_needs_a_recorded_process_group(self):
        namespace = {}
        exec(self.ad.REMOTE_STATE_HOME + self.ad.REMOTE_LIVENESS, namespace)
        status = {"events": "/unused/events.jsonl", "executor_pid": 100}
        with patch.dict(namespace, {"_proc_table": lambda: {},
                                    "_output_holders": lambda _: False,
                                    "_pid_alive": lambda _: False,
                                    "_group_empty": lambda pgid: True}):
            self.assertIsNone(namespace["_executor_alive"](status),
                              "without a recorded process group the session cannot be called dead")
            self.assertIs(namespace["_executor_alive"](dict(status, executor_pgid=200)), False,
                          "an empty process group and a dead executor prove that the session is gone")

        class ProcPermission:
            path = os.path

            @staticmethod
            def listdir(path):
                if path == "/proc": return ["123"]
                if path == "/proc/123/fd": return ["4"]
                raise FileNotFoundError(path)

            @staticmethod
            def readlink(path):
                raise PermissionError(path)

            @staticmethod
            def killpg(pgid, signal):
                raise ProcessLookupError(pgid)

        namespace["os"] = ProcPermission
        namespace["_proc_table"] = lambda: {}
        self.assertIsNone(namespace["_executor_alive"](
            {"events": "/unused/events.jsonl", "executor_pid": 100, "executor_pgid": 200}),
            "an unreadable fd must make liveness unknown")

    def test_executor_exit_does_not_prove_detached_descendants_are_dead(self):
        namespace = {}
        exec(self.ad.REMOTE_STATE_HOME + self.ad.REMOTE_LIVENESS, namespace)
        status = {"events": "/unused/events.jsonl", "executor_pid": 100,
                  "attempt_started": "2026-11-09T00:00:00Z"}
        # A child can setsid(), close both output files, and be reparented after
        # the executor exits. The ordinary process and fd scans cannot tie it to
        # this run, so absence from those scans is not proof that the session died.
        with patch.dict(namespace, {"_proc_table": lambda: {},
                                    "_output_holders": lambda _: False,
                                    "_pid_alive": lambda _: False}):
            self.assertIsNone(namespace["_executor_alive"](status),
                              "an attempted executor without a recorded process group is unknown")

    def test_missing_executor_pid_after_execution_started_is_unknown(self):
        namespace = {}
        exec(self.ad.REMOTE_STATE_HOME + self.ad.REMOTE_LIVENESS, namespace)
        status = {"events": "/unused/events.jsonl", "runner_pid": 100,
                  "execution_started": "2026-11-09T00:00:00Z"}
        with patch.dict(namespace, {"_proc_table": lambda: {},
                                    "_output_holders": lambda _: False}):
            self.assertIsNone(namespace["_executor_alive"](status),
                              "without an executor pid the runner may have spawned an unrecorded executor")

    def test_missing_executor_pid_stays_unknown_after_execution_started(self):
        status = self.run_remote_status(
            2147483647, None, execution_started="2026-11-09T00:00:00Z")
        self.assertIs(status.get("runner_alive"), False)
        self.assertIsNone(status.get("executor_alive"),
                          "without an executor pid the session cannot be called dead")

    def test_a_setsidded_child_without_a_recorded_process_group_is_unknown(self):
        namespace = {}
        exec(self.ad.REMOTE_STATE_HOME + self.ad.REMOTE_LIVENESS, namespace)
        scratch = ROOT / ".swrm-tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            marker = Path(tmp) / "child.pid"
            events = Path(tmp) / "events.jsonl"
            events.write_text("")
            child_code = ("import os,sys,time; open(sys.argv[1], 'w').write(str(os.getpid())); "
                          "time.sleep(30)")
            executor_code = (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL, start_new_session=True)")
            executor = subprocess.Popen(
                [sys.executable, "-c", executor_code, child_code, str(marker)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, cwd=tmp)
            child_pid = None
            try:
                executor.wait(timeout=5)
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "the detached child should start")
                child_pid = int(marker.read_text())
                status = {"events": str(events), "executor_pid": executor.pid,
                          "attempt_started": "2026-11-09T00:00:00Z",
                          "workspace": tmp}
                self.assertIsNone(namespace["_executor_alive"](status),
                                  "a detached child without a recorded process group keeps the session unknown")
            finally:
                if child_pid is not None:
                    try: os.kill(child_pid, 15)
                    except ProcessLookupError: pass

    def test_remote_cleanup_clears_only_inflight_fields(self):
        original = {"state": "busy", "current_run": "old-run", "stale_run": "old-run",
                    "stale_since": 1, "activity_observed_since": 2, "custom": "keep"}
        for fields in ({"pgid": None}, {"group": "live"}, {"group": "fail"}):
            with self.subTest(fields=fields):
                ok, state = self.run_cleanup(original, **fields)
                self.assertFalse(ok)
                self.assertEqual(state, original)
        ok, state = self.run_cleanup(original, pgid=DEAD_PGID, group="empty")
        self.assertTrue(ok)
        self.assertEqual(state["state"], "idle")
        self.assertIsNone(state["current_run"])
        self.assertEqual(state["custom"], "keep")
        for field in ("stale_run", "stale_since", "activity_observed_since"):
            self.assertNotIn(field, state)

    def test_remote_cleanup_rechecks_liveness_and_locked_lane(self):
        original = {"state": "busy", "current_run": "old-run"}
        for effect in (None, PermissionError, OSError):
            with self.subTest(effect=effect):
                ok, state = self.run_cleanup(original, pid_effect=effect)
                self.assertFalse(ok)
                self.assertEqual(state, original)
        replacement = {"state": "busy", "current_run": "new-run"}
        ok, state = self.run_cleanup(original, before_lock=replacement)
        self.assertFalse(ok)
        self.assertEqual(state, replacement)

    def test_remote_cleanup_rejects_unknown_pid_results_and_write_failure(self):
        original = {"state": "busy", "current_run": "old-run"}
        cases = [{"pid": pid} for pid in (None, False, 0, -1, "bad")]
        cases.extend(({"completed": True}, {"fail_write": True},
                      {"executor_alive": True}, {"executor_alive": None}))
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                ok, state = self.run_cleanup(original, **kwargs)
                self.assertFalse(ok)
                self.assertEqual(state, original)

    def test_remote_cleanup_needs_an_empty_process_group(self):
        original = {"state": "busy", "current_run": "old-run"}
        # A process group with a member, a group that cannot be checked and a
        # status without a recorded group are all unverifiable: the lane keeps
        # its run (Refs #1663).
        for kwargs in ({"group": "live"}, {"group": "fail"}, {"pgid": None}):
            with self.subTest(kwargs=kwargs):
                ok, state = self.run_cleanup(original, **kwargs)
                self.assertFalse(ok)
                self.assertEqual(state, original)

    def test_the_cleanup_is_vetoed_by_a_process_that_writes_the_output(self):
        original = {"state": "busy", "current_run": "old-run"}
        # The process group of the executor is empty, but a process still writes
        # the output of the run: the session is not dead (Refs #1663).
        ok, state = self.run_cleanup(original, holder=True)
        self.assertFalse(ok)
        self.assertEqual(state, original)

    def test_available_executor_evidence_vetoes_cleanup(self):
        for alive in (True, None):
            with self.subTest(alive=alive):
                self.status["executor_alive"] = alive
                self.assertFalse(self.reconcile())
                self.assertEqual(self.order, [])

    def test_missing_executor_evidence_vetoes_cleanup(self):
        self.status.pop("executor_alive")
        self.assertFalse(self.reconcile())
        self.assertEqual(self.order, [])

    def test_remote_cleanup_does_not_trust_missing_executor_evidence(self):
        original = {"state": "busy", "current_run": "old-run"}
        def kill(pid, signal):
            if pid == os.getpid(): return None
            raise ProcessLookupError()
        ok, state = self.run_cleanup(original, omit_executor_alive=True,
                                     executor_pid=os.getpid(), pid_effect=kill)
        self.assertFalse(ok)
        self.assertEqual(state, original)

    def test_remote_cleanup_does_not_trust_unverified_executor_flag(self):
        # A stored boolean cannot prove that an executor did not start between
        # spawning and recording its PID, or that its descendants have exited.
        original = {"state": "busy", "current_run": "old-run"}
        ok, state = self.run_cleanup(original, executor_alive=False, executor_pid=None,
                                     attempt_started=True)
        self.assertFalse(ok)
        self.assertEqual(state, original)

    def test_recovered_busy_run_does_not_bypass_failed_cleanup(self):
        self.state["_recovery"] = {"D1": {"run": "old-run"}}
        with patch.object(self.ad, "clear_orphan_lane", return_value=False):
            self.assertFalse(self.reconcile())
            self.assertFalse(self.ad.verify_recovered_run(
                "D1", {"host": "example.com"}, self.st, self.status, self.state))
        self.assertIs(self.state["D1"], self.st)
        self.assertIn("D1", self.state["_recovery"])
        self.assertEqual(self.order, [])

    def test_recovered_dead_busy_run_clears_before_requeue(self):
        self.state["_recovery"] = {"D1": {"run": "old-run"}}
        self.assertTrue(self.ad.verify_recovered_run(
            "D1", {"host": "example.com"}, self.st, self.status, self.state))
        self.assertEqual(self.order, ["cleanup", "view", "edit"])
        self.assertIs(self.state["D1"], self.st)
        self.assertNotIn("D1", self.state["_recovery"])

    def test_live_executor_pid_vetoes_remote_cleanup(self):
        original = {"state": "busy", "current_run": "old-run"}
        def kill(pid, signal):
            if pid == os.getpid(): return None
            raise ProcessLookupError()
        ok, state = self.run_cleanup(original, pid_effect=kill, executor_pid=os.getpid())
        self.assertFalse(ok)
        self.assertEqual(state, original)

    def test_tick_keeps_recovery_after_cleanup_failure(self):
        spec = importlib.util.spec_from_file_location(
            "host_recovery_fixture", ROOT / "tools/tests/test_autodispatch_host_recovery.py")
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        case = fixture.RecoveryTickTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.outage()
        state = case.tick({fixture.LANE: fixture.lane_state()})
        case.host_back()
        case.status = fixture.remote_status(lane="busy", current_run=fixture.RUN,
                                            runner_alive=False, executor_alive=False)
        with patch.object(case.module, "clear_orphan_lane", return_value=False):
            state = case.tick(state)
        self.assertIn(fixture.LANE, state)
        self.assertIn(fixture.LANE, state["_recovery"])
        self.assertEqual(case.fleet.edits, [])

    def test_shared_liveness_guard_can_veto_cleanup(self):
        for liveness in ("alive", "unknown"):
            with self.subTest(liveness=liveness), patch.object(self.ad, "runner_liveness", return_value=liveness):
                self.assertFalse(self.reconcile())
                self.assertEqual(self.order, [])

    def test_transport_timeout_keeps_record(self):
        with patch.object(self.ad, "sh", side_effect=self.ad.subprocess.TimeoutExpired("ssh", 60)):
            self.assertFalse(self.reconcile())
        self.assertEqual(self.order, [])
        self.assertIs(self.state["D1"], self.st)


if __name__ == "__main__":
    unittest.main()
