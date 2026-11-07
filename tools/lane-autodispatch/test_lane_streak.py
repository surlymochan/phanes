#!/usr/bin/env python3
"""Unit tests for the lane failure circuit breaker (Refs #1825).

A lane that ends the same bad way three times in a row is paused automatically
(`PAUSE-<lane>`); the pause is never released automatically.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)

SCRATCH = Path(__file__).resolve().parents[2] / ".openswarm-tmp"
EXEC_ERROR = {"status": "error", "error_class": "exec_error"}
COMMITTED = {"status": "done", "error_class": "none", "branch": "fleet/M19-issue1-run3", "commit": "c0ffee"}


class LaneStreakTest(unittest.TestCase):
    """The breaker counts lane failures, clears on benign terminations and pauses at three."""

    def setUp(self):
        SCRATCH.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.orig_d, self.orig_brain, self.orig_gh = AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG, AUTODISPATCH.gh
        AUTODISPATCH.D = str(self.dir)
        AUTODISPATCH.BRAIN_LOG = str(self.dir / "brain.log")
        AUTODISPATCH.gh = lambda args, timeout=60: (0, "", "")
        self.addCleanup(self.restore)

    def restore(self):
        AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG, AUTODISPATCH.gh = self.orig_d, self.orig_brain, self.orig_gh

    def note(self, lane, run, info):
        """Record one finished run of `lane`."""
        return AUTODISPATCH.lane_streak_note(lane, run, info)

    def pause(self, lane="M19"):
        """The pause file of `lane`, if it exists."""
        path = self.dir / f"PAUSE-{lane}"
        return path.read_text() if path.exists() else None

    def brain(self):
        """The brain log written by the breaker."""
        path = self.dir / "brain.log"
        return path.read_text() if path.exists() else ""

    def streaks(self):
        """The persisted streak state."""
        path = self.dir / "lane-streak.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def test_three_exec_errors_pause_the_lane(self):
        self.assertEqual(self.note("M19", "run-1", EXEC_ERROR), 1)
        self.assertEqual(self.note("M19", "run-2", EXEC_ERROR), 2)
        self.assertIsNone(self.pause(), "two failures must not pause the lane")
        self.assertEqual(self.note("M19", "run-3", EXEC_ERROR), 3)
        pause = self.pause()
        self.assertIsNotNone(pause, "the third failure in a row must pause the lane")
        self.assertTrue(pause.startswith("auto:streak "), pause)
        self.assertIn("3次", pause)
        self.assertIn("run-1 run-2 run-3", pause)
        self.assertEqual(len(self.brain().strip().splitlines()), 1, self.brain())
        self.assertIn("M19", self.brain())
        alert = (self.dir / "health.alert").read_text()
        self.assertIn("alert=lane_streak", alert)
        self.assertIn("lane=M19", alert)
        self.assertEqual(self.streaks()["M19"]["n"], 3)

    def test_committed_run_in_between_clears_the_streak(self):
        self.note("M19", "run-1", EXEC_ERROR)
        self.note("M19", "run-2", EXEC_ERROR)
        self.assertEqual(self.note("M19", "run-3", COMMITTED), 0)
        self.assertNotIn("M19", self.streaks())
        self.note("M19", "run-4", EXEC_ERROR)
        self.note("M19", "run-5", EXEC_ERROR)
        self.assertIsNone(self.pause(), "a committed run breaks the streak")
        self.assertEqual(self.streaks()["M19"]["n"], 2)

    def test_budget_exceeded_does_not_count(self):
        self.note("M19", "run-1", EXEC_ERROR)
        self.note("M19", "run-2", {"status": "budget_exceeded", "error_class": "budget_exceeded"})
        self.assertNotIn("M19", self.streaks(), "a budget stop clears the streak")
        self.note("M19", "run-3", EXEC_ERROR)
        self.note("M19", "run-4", EXEC_ERROR)
        self.assertIsNone(self.pause(), "budget_exceeded must not count as a lane failure")
        self.assertEqual(self.streaks()["M19"]["n"], 2)

    def test_thin_gate_does_not_count(self):
        self.note("M19", "run-1", {"status": "failed", "error_class": "thin_gate"})
        self.assertNotIn("M19", self.streaks())
        self.note("M19", "run-2", EXEC_ERROR)
        self.note("M19", "run-3", EXEC_ERROR)
        self.assertIsNone(self.pause())

    def test_existing_pause_is_not_rewritten(self):
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        for i in range(3):
            self.note("M19", f"run-{i}", EXEC_ERROR)
        self.assertEqual(self.pause(), "manual hold\n", "an existing pause must be kept as is")
        self.assertEqual(self.brain(), "", "no trip is announced when the pause already exists")

    def test_manual_clear_keeps_the_breaker_armed(self):
        for i in range(3):
            self.note("M19", f"run-{i}", EXEC_ERROR)
        (self.dir / "PAUSE-M19").unlink()  # the operator clears the pause by hand
        self.note("M19", "run-3", EXEC_ERROR)
        self.assertIsNotNone(self.pause(), "the breaker stays armed after a manual clear")

    def test_missing_state_file_is_treated_as_empty(self):
        self.assertEqual(self.note("M19", "run-1", EXEC_ERROR), 1)
        self.assertEqual(self.streaks()["M19"]["n"], 1)
        self.assertIsNone(self.pause())

    def test_broken_state_file_is_treated_as_empty(self):
        (self.dir / "lane-streak.json").write_text("{not json")
        self.assertEqual(self.note("M19", "run-1", EXEC_ERROR), 1)
        self.assertIsNone(self.pause(), "a broken state file must not look like a streak")
        self.assertEqual(self.streaks()["M19"]["n"], 1)

    def test_broken_streak_entry_counts_as_zero(self):
        (self.dir / "lane-streak.json").write_text(json.dumps({"M19": {"n": "many"}}))
        self.assertEqual(self.note("M19", "run-1", EXEC_ERROR), 1)
        self.assertIsNone(self.pause())

    def test_invalid_run_list_and_count_do_not_trip(self):
        for entry in ({"n": 2, "runs": 123, "at": "now"},
                      {"n": 999, "runs": ["old"], "at": "now"}):
            with self.subTest(entry=entry):
                (self.dir / "lane-streak.json").write_text(json.dumps({"M19": entry}))
                self.assertEqual(self.note("M19", "run-1", EXEC_ERROR), 1)
                self.assertIsNone(self.pause())

    def test_finish_records_the_streak(self):
        """The breaker is wired into the run-end handling of `finish()`."""
        state = {}
        for i in range(3):
            st = {"issue": 1800 + i, "run": f"run-{i}", "host": "h", "base": "b" * 40}
            info = {"status": "error", "error_class": "exec_error", "branch": None, "commit": None}
            AUTODISPATCH.finish("M19", {"host": "h"}, st, info, state)
        self.assertIsNotNone(self.pause(), "three run-end failures must pause the lane")
        self.assertIn("run-0 run-1 run-2", self.pause())


if __name__ == "__main__":
    unittest.main()
