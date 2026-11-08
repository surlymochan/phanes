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
    def with_state_dir(self, callback):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            old_d, old_brain_log = AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG
            AUTODISPATCH.D = directory
            AUTODISPATCH.BRAIN_LOG = str(Path(directory) / "brain.log")
            try:
                callback(Path(directory))
            finally:
                AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG = old_d, old_brain_log

    def test_three_exec_error_terminations_pause_lane_from_tick(self):
        def run(directory):
            lane = "M19"
            lanes = {lane: {"host": "executor", "backend": "pi"}}
            run_ids = iter(("run-1", "run-2", "run-3"))
            logs = []

            def terminal_run(*args):
                run_id = next(run_ids)
                return {"run": {"status": "exec_error", "branch": None, "commit": None,
                                "error_class": "exec_error", "repo": "repo"},
                        "lane": "idle", "run_id": run_id}

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

            pause_marker = directory / f"PAUSE-{lane}"
            self.assertTrue(pause_marker.exists(), "three consecutive exec_error runs must pause their lane")
            self.assertIn("auto:streak", pause_marker.read_text())
            self.assertIn("run-1", pause_marker.read_text())
            self.assertIn("run-2", pause_marker.read_text())
            self.assertIn("run-3", pause_marker.read_text())
            self.assertTrue((directory / "health.alert").exists())
            self.assertTrue(any("auto:streak" in message and lane in message for message in logs))

        self.with_state_dir(run)

    def test_commit_clears_streak_and_budget_exceeded_is_not_counted(self):
        def run(directory):
            AUTODISPATCH.record_lane_terminal("M19", "run-0", {"status": "exec_error"})
            AUTODISPATCH.record_lane_terminal("M19", "run-1", {"status": "exec_error"})
            AUTODISPATCH.record_lane_terminal("M19", "run-2", {"status": "finished"}, committed=True)
            AUTODISPATCH.record_lane_terminal("M19", "run-3", {"status": "exec_error"})
            AUTODISPATCH.record_lane_terminal("M19", "run-4", {"status": "exec_error"})
            AUTODISPATCH.record_lane_terminal("M19", "run-5", {"status": "budget_exceeded"})
            streaks = AUTODISPATCH.read_lane_streaks()
            self.assertNotIn("M19", streaks)
            self.assertFalse((directory / "PAUSE-M19").exists())
        self.with_state_dir(run)

    def test_third_failure_blocks_same_tick_dispatch_only_on_paused_lane(self):
        def run(directory):
            lanes = {name: {"host": "executor", "backend": "pi"} for name in ("M19", "M20")}
            for run_id in ("run-1", "run-2"):
                AUTODISPATCH.record_lane_terminal("M19", run_id, {"status": "exec_error"})
            state = {"M19": {"issue": 1825, "run": "run-3", "host": "executor"}}
            task = {"n": 2000, "backend": "pi", "prio": 1, "strict": False, "verify": False}

            def terminal_run(lane, *args):
                return {"lane": "idle", "run": {"status": "exec_error"} if lane == "M19" else None}

            with mock.patch.object(AUTODISPATCH, "audit_switches"), \
                 mock.patch.object(AUTODISPATCH, "pause_store_dir", return_value=str(directory / "pauses")), \
                 mock.patch.object(AUTODISPATCH, "host_ok", return_value=True), \
                 mock.patch.object(AUTODISPATCH, "remote", side_effect=terminal_run), \
                 mock.patch.object(AUTODISPATCH, "label_lane_failed"), \
                 mock.patch.object(AUTODISPATCH, "queue", return_value=[task]), \
                 mock.patch.object(AUTODISPATCH, "deps_met", return_value=True), \
                 mock.patch.object(AUTODISPATCH, "read_issue", return_value=("fresh", {})), \
                 mock.patch.object(AUTODISPATCH, "sh", return_value=(0, "base refs/heads/main", "")), \
                 mock.patch.object(AUTODISPATCH, "gh", return_value=(0, "", "")), \
                 mock.patch.object(AUTODISPATCH, "dispatch", return_value=("new-run", None)) as dispatch, \
                 mock.patch.object(AUTODISPATCH, "replenish", return_value=[]), \
                 mock.patch.object(AUTODISPATCH, "status_snapshot", return_value={}):
                AUTODISPATCH.tick(lanes, state, 0)

            self.assertTrue((directory / "PAUSE-M19").exists())
            dispatch.assert_called_once_with("M20", lanes["M20"], task, "base")
            self.assertNotIn("M19", state)
            self.assertEqual(state["M20"]["run"], "new-run")

        self.with_state_dir(run)

    def test_corrupt_streak_entry_is_empty_and_does_not_pause_early(self):
        def run(directory):
            (directory / "lane-streak.json").write_text('{"M19":{"n":2,"runs":123}}')
            AUTODISPATCH.record_lane_terminal("M19", "run-3", {"status": "exec_error"})
            self.assertFalse((directory / "PAUSE-M19").exists())
            self.assertEqual(AUTODISPATCH.read_lane_streaks()["M19"], {"n": 1, "runs": ["run-3"]})
        self.with_state_dir(run)

    def test_missing_or_malformed_streak_file_starts_clean(self):
        def run(directory):
            path = directory / "lane-streak.json"
            for damaged in (None, "{not json", '{"M19":{"n":99,"runs":["old"]}}'):
                if damaged is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(damaged)
                AUTODISPATCH.record_lane_terminal("M19", "run-1", {"status": "exec_error"})
                self.assertFalse((directory / "PAUSE-M19").exists())
                self.assertEqual(AUTODISPATCH.read_lane_streaks().get("M19"),
                                 {"n": 1, "runs": ["run-1"]})
        self.with_state_dir(run)

    def test_existing_pause_marker_is_not_rewritten_or_realerted(self):
        def run(directory):
            marker = directory / "PAUSE-M19"
            marker.write_text("manual pause\n")
            with mock.patch.object(AUTODISPATCH, "log") as logged:
                for run_id in ("run-1", "run-2", "run-3", "run-4"):
                    AUTODISPATCH.record_lane_terminal("M19", run_id, {"status": "exec_error"})
            self.assertEqual(marker.read_text(), "manual pause\n")
            self.assertFalse((directory / "health.alert").exists())
            logged.assert_not_called()
        self.with_state_dir(run)


if __name__ == "__main__":
    unittest.main()
