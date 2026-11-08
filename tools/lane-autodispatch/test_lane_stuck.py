#!/usr/bin/env python3
"""Regression coverage for fail-closed lane stuck cleanup (Refs #1825)."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)


class LaneStuckTickTest(unittest.TestCase):
    def make_run_data(self, home, queue_reason=None):
        runs = home / "runs"
        rows = []
        for index in range(3):
            run_id = f"run-{index + 1}"
            run_dir = runs / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "events.1.pi.jsonl").write_text("")
            status = {"state": "finished", "attempt_started": 1}
            if queue_reason:
                status["queue_reason"] = queue_reason
            (run_dir / "status.json").write_text(json.dumps(status))
            rows.append({"id": run_id, "lane": "M19", "status": "budget_exceeded"})
        (home / "ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def run_probe(self, home):
        env = dict(os.environ, OPENSWARM_HOME=str(home))
        result = subprocess.run([sys.executable, "-", "M19"], input=AUTODISPATCH.REMOTE_STUCK_CLEANUP,
                                text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    def make_fake_process_tree(self, root):
        for pid, cmd, wchan, children in (
                (43210, b"pi\0--mode\0rpc", "do_wait", "43211"),
                (43211, b"bwrap\0--unshare-all", "poll_schedule_timeout", "")):
            proc = root / str(pid)
            (proc / "task" / str(pid)).mkdir(parents=True)
            rest = "S " + " ".join(str(value) for value in range(20))
            (proc / "stat").write_text(f"{pid} (fake-process) {rest}")
            (proc / "wchan").write_text(wchan)
            (proc / "syscall").write_text("1 2 3")
            (proc / "cmdline").write_bytes(cmd)
            (proc / "task" / str(pid) / "children").write_text(children)

    def test_real_ledger_and_events_do_not_clear_queued_budget_runs(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            home = Path(directory)
            self.make_run_data(home, queue_reason="provider concurrency limit")
            result = self.run_probe(home)
            self.assertFalse(result["cleaned"])
            self.assertEqual(result["reason"], "queued-or-status-unknown")
            self.assertFalse((home / "lane-stuck-audit.jsonl").exists())

    def test_unknown_process_evidence_never_marks_cleanup_success(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            home = Path(directory)
            self.make_run_data(home)
            result = self.run_probe(home)
            self.assertFalse(result["cleaned"])
            self.assertEqual(result["reason"], "rpc-pid-unknown")
            self.assertFalse((home / "lane-stuck-audit.jsonl").exists())

    def test_tick_routes_budget_exceeded_terminal_through_cleanup_probe(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            lane = "M19"
            lanes = {lane: {"host": "executor", "backend": "pi"}}
            logs = []
            old_d, old_brain_log = AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG
            AUTODISPATCH.D = directory
            AUTODISPATCH.BRAIN_LOG = str(Path(directory) / "brain.log")
            with mock.patch.object(AUTODISPATCH, "audit_switches"), \
                 mock.patch.object(AUTODISPATCH, "paused_scopes", return_value=set()), \
                 mock.patch.object(AUTODISPATCH, "host_ok", return_value=True), \
                 mock.patch.object(AUTODISPATCH, "remote", return_value={
                     "run": {"status": "budget_exceeded", "branch": None, "commit": None,
                             "error_class": "budget_exceeded", "repo": "repo"},
                     "lane": "idle", "run_id": "run-3"}), \
                 mock.patch.object(AUTODISPATCH, "cleanup_lane_stuck",
                                   return_value={"cleaned": False, "reason": "queued-or-status-unknown"}) as cleanup, \
                 mock.patch.object(AUTODISPATCH, "label_lane_failed"), \
                 mock.patch.object(AUTODISPATCH, "queue", return_value=[]), \
                 mock.patch.object(AUTODISPATCH, "replenish", return_value=[]), \
                 mock.patch.object(AUTODISPATCH, "status_snapshot", return_value={}), \
                 mock.patch.object(AUTODISPATCH, "save_state"), \
                 mock.patch.object(AUTODISPATCH, "log", side_effect=lambda message, **kwargs: logs.append(message)):
                state = {lane: {"issue": 1825, "run": "run-3", "host": "executor"}}
                AUTODISPATCH.tick(lanes, state, 0)
            AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG = old_d, old_brain_log
            cleanup.assert_called_once_with(lane, lanes[lane])
            self.assertTrue(any("stuck cleanup not performed" in message for message in logs))
            self.assertFalse((Path(directory) / "health.alert").exists())

    def test_tick_alerts_only_after_probe_confirms_term(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            lane = "M19"
            lanes = {lane: {"host": "executor", "backend": "pi"}}
            old_d, old_brain_log = AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG
            AUTODISPATCH.D = directory
            AUTODISPATCH.BRAIN_LOG = str(Path(directory) / "brain.log")
            try:
                with mock.patch.object(AUTODISPATCH, "audit_switches"), \
                     mock.patch.object(AUTODISPATCH, "paused_scopes", return_value=set()), \
                     mock.patch.object(AUTODISPATCH, "host_ok", return_value=True), \
                     mock.patch.object(AUTODISPATCH, "remote", return_value={
                         "run": {"status": "budget_exceeded", "branch": None, "commit": None,
                                 "error_class": "budget_exceeded", "repo": "repo"},
                         "lane": "idle", "run_id": "run-3"}), \
                     mock.patch.object(AUTODISPATCH, "cleanup_lane_stuck", return_value={
                         "cleaned": True, "runs": ["run-1", "run-2", "run-3"], "pids": [1234]}), \
                     mock.patch.object(AUTODISPATCH, "label_lane_failed"), \
                     mock.patch.object(AUTODISPATCH, "queue", return_value=[]), \
                     mock.patch.object(AUTODISPATCH, "replenish", return_value=[]), \
                     mock.patch.object(AUTODISPATCH, "status_snapshot", return_value={}), \
                     mock.patch.object(AUTODISPATCH, "save_state"), \
                     mock.patch.object(AUTODISPATCH, "log"):
                    state = {lane: {"issue": 1825, "run": "run-3", "host": "executor"}}
                    AUTODISPATCH.tick(lanes, state, 0)
            finally:
                AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG = old_d, old_brain_log
            alert = (Path(directory) / "health.alert").read_text()
            self.assertIn("auto:stuck", alert)
            self.assertIn("run-1,run-2,run-3", alert)

    def test_tick_cleans_only_three_zero_event_runs_with_stuck_process_evidence(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            home = Path(directory) / "home"
            home.mkdir()
            self.make_run_data(home)
            proc_root = Path(directory) / "proc"
            self.make_fake_process_tree(proc_root)
            (home / "lanes" / "M19").mkdir(parents=True)
            (home / "lanes" / "M19" / "state.json").write_text(json.dumps({"child_pid": 43210}))

            def run_remote_probe(argv, timeout=60, inp=None):
                # The fake /proc snapshot gives the probe deterministic evidence; replace
                # kill and post-signal exit observation so this test never signals a real PID.
                source = inp.replace("try: os.kill(target,signal.SIGTERM); sent.append(target)",
                                     "try: sent.append(target)")
                source = source.replace("  state=proc(pid)\n  if state is None",
                                        "  state=None  # test observes the simulated TERM exit\n  if state is None")
                env = dict(os.environ)
                result = subprocess.run([sys.executable, "-", "M19"], input=source, text=True,
                                        capture_output=True, env=env, timeout=10)
                return result.returncode, result.stdout, result.stderr

            lane = "M19"
            lanes = {lane: {"host": "executor", "backend": "pi"}}
            old_d, old_brain_log = AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG
            AUTODISPATCH.D = str(Path(directory) / "control")
            Path(AUTODISPATCH.D).mkdir()
            AUTODISPATCH.BRAIN_LOG = str(Path(AUTODISPATCH.D) / "brain.log")
            try:
                with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(home), "AD_PROC_ROOT": str(proc_root)}), \
                     mock.patch.object(AUTODISPATCH, "audit_switches"), \
                     mock.patch.object(AUTODISPATCH, "paused_scopes", return_value=set()), \
                     mock.patch.object(AUTODISPATCH, "host_ok", return_value=True), \
                     mock.patch.object(AUTODISPATCH, "remote", return_value={
                         "run": {"status": "budget_exceeded", "branch": None, "commit": None,
                                 "error_class": "budget_exceeded", "repo": "repo"},
                         "lane": "idle", "run_id": "run-3"}), \
                     mock.patch.object(AUTODISPATCH, "sh", side_effect=run_remote_probe), \
                     mock.patch.object(AUTODISPATCH, "label_lane_failed"), \
                     mock.patch.object(AUTODISPATCH, "queue", return_value=[]), \
                     mock.patch.object(AUTODISPATCH, "replenish", return_value=[]), \
                     mock.patch.object(AUTODISPATCH, "status_snapshot", return_value={}), \
                     mock.patch.object(AUTODISPATCH, "save_state"), \
                     mock.patch.object(AUTODISPATCH, "log"):
                    state = {lane: {"issue": 1825, "run": "run-3", "host": "executor"}}
                    AUTODISPATCH.tick(lanes, state, 0)
            finally:
                AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG = old_d, old_brain_log
            audit = (home / "lane-stuck-audit.jsonl").read_text()
            self.assertIn('"confirmed": true', audit)
            self.assertIn('"runs": ["run-1", "run-2", "run-3"]', audit)
            alert = (Path(directory) / "control" / "health.alert").read_text()
            self.assertIn("auto:stuck", alert)
            recovery = home / "runs" / "run-4"
            recovery.mkdir()
            (recovery / "events.1.pi.jsonl").write_text('{"type":"message","text":"progress"}\n')
            with (home / "ledger.jsonl").open("a") as ledger:
                ledger.write(json.dumps({"id": "run-4", "lane": lane, "status": "finished"}) + "\n")
            resumed = self.run_probe(home)
            self.assertFalse(resumed["cleaned"])
            self.assertEqual(resumed["reason"], "not-three-budget-runs")


if __name__ == "__main__":
    unittest.main()
