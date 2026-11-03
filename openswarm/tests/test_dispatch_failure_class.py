"""Failure classification and budget requeue timing (Refs #1668).

The module is loaded by file path: ``openswarm/lib/dispatch`` is a namespace package
(no ``__init__.py``), so it cannot be imported as a package from the repository root.
"""
import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "failure_class.py"
SPEC = importlib.util.spec_from_file_location("dispatch_failure_class", MODULE_PATH)
failure_class = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(failure_class)


class FailureClassTests(unittest.TestCase):
    def test_budget_classes_are_budget_failures(self):
        for error_class in ("budget_exceeded", "budget_exceeded_daily", "budget_exceeded_weekly",
                            "BUDGET-EXCEEDED", "budget-exhausted"):
            with self.subTest(error_class=error_class):
                self.assertEqual(failure_class.classify(error_class), failure_class.REASON_BUDGET)
                self.assertTrue(failure_class.is_budget(error_class))

    def test_quota_classes_stay_quota_failures(self):
        for error_class in ("rate_limit", "quota", "usage_limit", "insufficient_quota"):
            with self.subTest(error_class=error_class):
                self.assertEqual(failure_class.classify(error_class), failure_class.REASON_QUOTA)
                self.assertFalse(failure_class.is_budget(error_class))

    def test_every_other_class_is_a_real_failure(self):
        for error_class in ("exec_error", "stalled", "verify_failed", "model_unavailable", "risk_blocked"):
            with self.subTest(error_class=error_class):
                self.assertEqual(failure_class.classify(error_class), failure_class.REASON_REAL)

    def test_missing_class_is_a_real_failure(self):
        for error_class in (None, "", 42, []):
            with self.subTest(error_class=error_class):
                self.assertEqual(failure_class.classify(error_class), failure_class.REASON_REAL)

    def test_normalize_folds_case_and_hyphens(self):
        self.assertEqual(failure_class.normalize(" BUDGET-Exceeded "), "budget_exceeded")
        self.assertEqual(failure_class.normalize(None), "")


class RequeueTimingTests(unittest.TestCase):
    def test_reset_time_wins_over_the_fallback_delay(self):
        self.assertEqual(failure_class.requeue_at(1000, 5000), 5000)

    def test_a_reset_time_in_the_past_requeues_at_once(self):
        self.assertEqual(failure_class.requeue_at(1000, 500), 1000)

    def test_unknown_reset_time_uses_the_delay(self):
        self.assertEqual(failure_class.requeue_at(1000, None, delay_s=600), 1600)
        self.assertEqual(failure_class.requeue_at(1000, "not a timestamp", delay_s=600), 1600)

    def test_iso_reset_time_is_understood(self):
        self.assertEqual(failure_class.requeue_at(0, "1970-01-01T01:00:00Z"), 3600)

    def test_the_hold_is_capped(self):
        self.assertEqual(failure_class.requeue_at(1000, 100000, max_hold_s=3600), 4600)
        self.assertEqual(failure_class.requeue_at(1000, None, delay_s=99999, max_hold_s=3600), 4600)

    def test_epoch_reads_epoch_numbers_and_iso_strings(self):
        self.assertEqual(failure_class.epoch(12), 12)
        self.assertEqual(failure_class.epoch("12"), 12)
        self.assertEqual(failure_class.epoch("1970-01-01T00:01:00Z"), 60)
        self.assertIsNone(failure_class.epoch(None))
        self.assertIsNone(failure_class.epoch(""))
        self.assertIsNone(failure_class.epoch("nope"))
        self.assertIsNone(failure_class.epoch(True))
        self.assertIsNone(failure_class.epoch(float("nan")))


class WindowStateTests(unittest.TestCase):
    """The window state decides whether a held card returns to the queue (Refs #1668)."""

    def test_an_explicit_probe_answer_is_a_state(self):
        self.assertEqual(failure_class.probe_state({"exhausted": True}), failure_class.WINDOW_EXHAUSTED)
        self.assertEqual(failure_class.probe_state({"exhausted": False}), failure_class.WINDOW_OPEN)

    def test_a_missing_probe_answer_is_unknown(self):
        for probe in (None, {}, {"exhausted": None}, "exhausted", 42):
            with self.subTest(probe=probe):
                self.assertEqual(failure_class.probe_state(probe), failure_class.WINDOW_UNKNOWN)

    def test_the_probe_answer_wins(self):
        self.assertEqual(failure_class.window_state({"exhausted": True}, reset_passed=True),
                         failure_class.WINDOW_EXHAUSTED)
        self.assertEqual(failure_class.window_state({"exhausted": False}, reset_passed=False),
                         failure_class.WINDOW_OPEN)

    def test_a_passed_reset_time_opens_the_window(self):
        self.assertEqual(failure_class.window_state({}, reset_passed=True), failure_class.WINDOW_OPEN)

    def test_an_unknown_window_never_opens(self):
        self.assertEqual(failure_class.window_state(None), failure_class.WINDOW_UNKNOWN)
        self.assertEqual(failure_class.window_state({}, reset_passed=False), failure_class.WINDOW_UNKNOWN)


class WindowResetTextTests(unittest.TestCase):
    """The provider error may say when the window recovers (Refs #1668)."""

    def test_the_run_text_may_name_the_reset(self):
        self.assertEqual(failure_class.reset_seconds_from_text("resets in 2h13m"), 2 * 3600 + 13 * 60)
        self.assertEqual(failure_class.reset_seconds_from_text("Resets in 30s"), 30)
        self.assertEqual(failure_class.reset_seconds_from_text("resets in 5m"), 300)

    def test_a_plain_reset_time_is_read(self):
        self.assertEqual(failure_class.reset_seconds_from_text("resets in 900"), 900)

    def test_a_retry_after_header_is_read(self):
        self.assertEqual(failure_class.reset_seconds_from_text('Retry-After: 120'), 120)
        self.assertEqual(failure_class.reset_seconds_from_text('"retry_after": 45'), 45)

    def test_a_text_without_a_reset_says_nothing(self):
        self.assertIsNone(failure_class.reset_seconds_from_text("Token Plan 用量上限"))
        self.assertIsNone(failure_class.reset_seconds_from_text(""))
        self.assertIsNone(failure_class.reset_seconds_from_text(None))

