"""Retry policy per error class for dispatch failures (Refs #1401).

The module is loaded by file path: ``openswarm/lib/dispatch`` is a namespace package
(no ``__init__.py``), so it cannot be imported as a package from the repository root.
"""
import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "retry.py"
SPEC = importlib.util.spec_from_file_location("dispatch_retry", MODULE_PATH)
retry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retry)


class DispatchRetryTests(unittest.TestCase):
    def test_each_action_is_returned_with_a_reason(self):
        table = {
            "stalled": "requeue",
            "cli_too_old": "pause_lane",
            "exec_error": "lane_failed",
            "bad_request": "close",
        }
        for error_class, action in table.items():
            with self.subTest(error_class=error_class):
                actual, reason = retry.decide(error_class, 0, table)
                self.assertEqual(actual, action)
                self.assertIn(error_class, reason)

    def test_card_table_decides_every_documented_shape(self):
        table = {
            "stalled": "requeue",
            "cli_too_old": "pause_lane",
            "exec_error": {"max": 2, "then": "lane_failed"},
        }
        self.assertEqual(retry.decide("stalled", 0, table)[0], "requeue")
        self.assertEqual(retry.decide("cli_too_old", 0, table)[0], "pause_lane")
        self.assertEqual(retry.decide("exec_error", 0, table)[0], "requeue")
        self.assertEqual(retry.decide("exec_error", 2, table)[0], "lane_failed")

    def test_bounded_policy_requeues_below_the_limit(self):
        table = {"exec_error": {"max": 2, "then": "lane_failed"}}
        action, reason = retry.decide("exec_error", 0, table)
        self.assertEqual(action, "requeue")
        self.assertIn("attempt 1 of 2", reason)
        self.assertEqual(retry.decide("exec_error", 1, table)[0], "requeue")

    def test_bounded_policy_switches_to_then_at_and_after_the_limit(self):
        table = {"exec_error": {"max": 2, "then": "lane_failed"}}
        action, reason = retry.decide("exec_error", 2, table)
        self.assertEqual(action, "lane_failed")
        self.assertIn("limit 2", reason)
        self.assertEqual(retry.decide("exec_error", 3, table)[0], "lane_failed")

    def test_unknown_error_class_uses_the_default_policy(self):
        table = {"default": "pause_lane", "stalled": "requeue"}
        action, reason = retry.decide("new_error", 0, table)
        self.assertEqual(action, "pause_lane")
        self.assertIn("new_error", reason)

    def test_unknown_error_class_uses_a_bounded_default_policy(self):
        table = {"default": {"max": 1, "then": "close"}}
        self.assertEqual(retry.decide("new_error", 0, table)[0], "requeue")
        self.assertEqual(retry.decide("new_error", 1, table)[0], "close")

    def test_unknown_error_class_without_default_is_closed(self):
        action, reason = retry.decide("new_error", 0, {"stalled": "requeue"})
        self.assertEqual(action, "close")
        self.assertIn("new_error", reason)

    def test_invalid_tables_raise_value_error(self):
        invalid = (
            None,
            {},
            {"stalled": "retry"},
            {"exec_error": {"max": 2}},
            {"exec_error": {"max": 0, "then": "close"}},
            {"exec_error": {"max": True, "then": "close"}},
            {"exec_error": {"max": 2, "then": "unknown"}},
            {"exec_error": {"max": 2, "then": "close", "extra": 1}},
            {"": "close"},
        )
        for table in invalid:
            with self.subTest(table=table):
                with self.assertRaises(ValueError):
                    retry.decide("exec_error", 0, table)

    def test_invalid_attempt_counts_raise_value_error(self):
        for attempts in (-1, 1.5, True):
            with self.subTest(attempts=attempts):
                with self.assertRaises(ValueError):
                    retry.decide("stalled", attempts, {"stalled": "requeue"})

    def test_non_string_error_class_raises_value_error(self):
        with self.assertRaises(ValueError):
            retry.decide(["stalled"], 0, {"stalled": "requeue"})


if __name__ == "__main__":
    unittest.main()
