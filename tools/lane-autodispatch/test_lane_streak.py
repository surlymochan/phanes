#!/usr/bin/env python3
"""Regression reproduction for the lane streak fuse (Refs #1825)."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)


class LaneStreakTickTest(unittest.TestCase):
    def test_three_exec_error_terminations_pause_lane_from_tick(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            old_d, old_brain_log = AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG
            AUTODISPATCH.D = directory
            AUTODISPATCH.BRAIN_LOG = str(Path(directory) / "brain.log")
            lane = "M19"
            lanes = {lane: {"host": "executor", "backend": "pi"}}
            run_ids = iter(("run-1", "run-2", "run-3"))
            logs = []

            def terminal_run(*args):
                run_id = next(run_ids)
                return {"run": {"status": "exec_error", "branch": None, "commit": None,
                                "error_class": "exec_error", "repo": "repo"},
                        "lane": "idle", "run_id": run_id}

            try:
                with mock.patch.object(AUTODISPATCH, "audit_switches"), \
                     mock.patch.object(AUTODISPATCH, "paused_scopes", return_value=set()), \
                     mock.patch.object(AUTODISPATCH, "host_ok", return_value=True), \
                     mock.patch.object(AUTODISPATCH, "remote", side_effect=terminal_run), \
                     mock.patch.object(AUTODISPATCH, "label_lane_failed"), \
                     mock.patch.object(AUTODISPATCH, "queue", return_value=[]), \
                     mock.patch.object(AUTODISPATCH, "replenish", return_value=[]), \
                     mock.patch.object(AUTODISPATCH, "status_snapshot", return_value={}), \
                     mock.patch.object(AUTODISPATCH, "save_state"), \
                     mock.patch.object(AUTODISPATCH, "log", side_effect=lambda message, **kwargs: logs.append(message)):
                    state = {}
                    for run_id in ("run-1", "run-2", "run-3"):
                        state[lane] = {"issue": 1825, "run": run_id, "host": "executor"}
                        AUTODISPATCH.tick(lanes, state, 0)

                pause_marker = Path(directory) / f"PAUSE-{lane}"
                self.assertTrue(pause_marker.exists(), "three consecutive exec_error runs must pause their lane")
                self.assertIn("auto:streak", pause_marker.read_text())
                self.assertIn("run-1", pause_marker.read_text())
                self.assertIn("run-2", pause_marker.read_text())
                self.assertIn("run-3", pause_marker.read_text())
                self.assertTrue((Path(directory) / "health.alert").exists())
                self.assertTrue(any("auto:streak" in message and lane in message for message in logs))
            finally:
                AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG = old_d, old_brain_log


if __name__ == "__main__":
    unittest.main()
