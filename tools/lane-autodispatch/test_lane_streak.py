#!/usr/bin/env python3
"""Regression reproduction for the lane streak fuse (Refs #1825)."""
import importlib.util
import tempfile
import unittest
from pr_guard_fixtures import no_open_prs
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

    def test_terminal_classification_table(self):
        # One case per ordered rule, including precedence over conflicting fields.
        cases = [
            ({"status": "failed", "error_class": "exec_error"}, True, "reset"),
            ({"status": "failed", "error_class": "thin_gate"}, False, "reset"),
            ({"status": "budget_exceeded", "error_class": "exec_error"}, False, "reset"),
            ({"status": "stopped", "error_class": "exec_error"}, False, "failure"),
            ({"status": "failed"}, False, "failure"),
            ({"status": "running"}, False, "ignore"),
            ({"status": "finished"}, False, "reset"),
            ({"status": None}, False, "failure"),
            ({"status": "error", "error_class": "exec_error"}, False, "failure"),
            ({"status": "failed", "error_class": "thin-gate"}, False, "reset"),
        ]
        for info, committed, expected in cases:
            with self.subTest(info=info, committed=committed):
                self.assertEqual(AUTODISPATCH.lane_terminal_kind(info, committed), expected)

    def tick_terminals(self, infos):
        lane = "M19"
        lanes = {lane: {"host": "executor", "backend": "pi"}}
        holds = []
        with mock.patch.object(AUTODISPATCH, "audit_switches"), \
             mock.patch.object(AUTODISPATCH, "paused_scopes", return_value=set()), \
             mock.patch.object(AUTODISPATCH, "host_ok", return_value=True), \
             mock.patch.object(AUTODISPATCH, "remote") as remote, \
             mock.patch.object(AUTODISPATCH, "cleanup_lane_stuck", return_value={"cleaned": False}), \
             mock.patch.object(AUTODISPATCH, "hold_budget_failure", wraps=AUTODISPATCH.hold_budget_failure) as hold, \
             mock.patch.object(AUTODISPATCH, "label_lane_failed"), \
             mock.patch.object(AUTODISPATCH, "queue", return_value=[]), \
             mock.patch.object(AUTODISPATCH, "replenish", return_value=[]), \
             mock.patch.object(AUTODISPATCH, "status_snapshot", return_value={}), \
             mock.patch.object(AUTODISPATCH, "save_state"), \
             mock.patch.object(AUTODISPATCH, "log"):
            for index, info in enumerate(infos):
                run_id = f"run-{index}"
                remote.return_value = {"lane": "idle", "run": info}
                state = {lane: {"issue": 1825 + index, "run": run_id, "host": "executor"}}
                AUTODISPATCH.tick(lanes, state, 0)
                holds.append(state.get("_budget_requeue"))
            return hold.call_count, holds

    def test_tick_budget_hold_clears_two_failures_before_next_failure(self):
        def run(directory):
            failed = {"status": "failed", "error_class": "exec_error"}
            budget = {"status": "budget_exceeded", "error_class": "budget_exceeded"}
            hold_count, holds = self.tick_terminals([failed, failed, budget, failed])
            self.assertEqual(hold_count, 1, "the real budget hold must still run")
            self.assertEqual(holds[2]["1827"]["run"], "run-2")
            self.assertEqual(holds[2]["1827"]["tries"], 1)
            self.assertEqual(AUTODISPATCH.read_lane_streaks()["M19"],
                             {"n": 1, "runs": ["run-3"]})
            self.assertFalse((directory / "PAUSE-M19").exists())
        self.with_state_dir(run)

    def test_tick_thin_gate_resets_and_exec_error_class_counts(self):
        def run(directory):
            failed = {"status": "stopped", "error_class": "exec_error"}
            self.tick_terminals([failed, failed, {"status": "failed", "error_class": "thin_gate"}, failed])
            self.assertEqual(AUTODISPATCH.read_lane_streaks()["M19"]["n"], 1)
            self.assertFalse((directory / "PAUSE-M19").exists())
            self.tick_terminals([failed, failed])
            self.assertTrue((directory / "PAUSE-M19").exists())
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

    def test_replays_recorded_m19_autodispatch_terminals_of_2026_11_03(self):
        # Terminal outcomes (status/error_class) of lane M19 as recorded in its autodispatch.log
        # on 2026-11-03, replayed through the real tick path.  The recorded order is:
        #   03:35 finished #1713 budget_exceeded (committed -> PR)  [not replayed: committed]
        #   04:23 ended #1361  budget_exceeded/budget_exceeded
        #   04:25 ended #1793  failed/exec_error
        #   04:36 ended #1794  failed/exec_error
        #   04:50 ended #1771  failed/exec_error   -> lane M19 was paused by the failure streak
        def run(directory):
            recorded = [("budget_exceeded", "budget_exceeded"), ("failed", "exec_error"),
                        ("failed", "exec_error"), ("failed", "exec_error")]
            self.tick_terminals([{"status": status, "error_class": error_class}
                                 for status, error_class in recorded[:3]])
            self.assertEqual(AUTODISPATCH.read_lane_streaks()["M19"]["n"], 2,
                             "the budget_exceeded ending clears; two failures follow")
            self.assertFalse((directory / "PAUSE-M19").exists())
            self.tick_terminals([{"status": "failed", "error_class": "exec_error"}])
            self.assertTrue((directory / "PAUSE-M19").exists(),
                            "the third consecutive recorded failure pauses the lane")
        self.with_state_dir(run)

    def test_m19_interleaved_terminal_sequence_does_not_pause(self):
        # Representative sequence: the original log is not present in this checkout.
        def run(directory):
            sequence = [("exec_error", False), ("failed", False),
                        ("budget_exceeded", False), ("exec_error", False),
                        ("finished", True), ("exec_error", False),
                        ("done", False), ("exec_error", False)]
            for index, (status, committed) in enumerate(sequence):
                AUTODISPATCH.record_lane_terminal("M19", f"run-{index}",
                                                  {"status": status}, committed=committed)
                self.assertFalse((directory / "PAUSE-M19").exists())
            self.assertEqual(AUTODISPATCH.read_lane_streaks()["M19"],
                             {"n": 1, "runs": ["run-7"]})
        self.with_state_dir(run)

    def test_representative_m19_sequence_triggers_on_third_consecutive_failure(self):
        """Replay the relevant failure/reset ordering, not unavailable original log entries.

        Its equivalence is limited to per-lane terminal ordering: two failures,
        a resetting terminal, then three consecutive failures on M19, with an
        unrelated lane interleaved. The original log is absent from this checkout.
        """
        def run(directory):
            sequence = [("M19", "exec_error"), ("M19", "failed"),
                        ("M19", "budget_exceeded"), ("M19", "exec_error"),
                        ("M20", "exec_error"), ("M19", "failed"),
                        ("M19", "exec_error")]
            for index, (lane, status) in enumerate(sequence):
                AUTODISPATCH.record_lane_terminal(lane, f"run-{index}", {"status": status})
                self.assertEqual((directory / "PAUSE-M19").exists(), index == 6)
            self.assertIn("run-3,run-5,run-6", (directory / "PAUSE-M19").read_text())
            self.assertFalse((directory / "PAUSE-M20").exists())
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
                 mock.patch.object(AUTODISPATCH, "gh", new=no_open_prs(lambda *args, **kwargs: (0, "", ""))), \
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
