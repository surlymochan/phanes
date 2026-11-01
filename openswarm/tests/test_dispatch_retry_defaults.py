"""Default retry table for the failures seen on the dispatch lanes (Refs #1510).

Loaded by file path, like ``test_dispatch_retry.py``: ``openswarm/lib/dispatch``
is a namespace package and cannot be imported from the repository root.
"""
import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "retry.py"
SPEC = importlib.util.spec_from_file_location("dispatch_retry_defaults", MODULE_PATH)
retry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retry)


class DefaultTableTests(unittest.TestCase):
    def test_risk_blocked_requeues_once_then_reroutes(self):
        table = retry.DEFAULT_TABLE
        self.assertEqual(retry.decide("risk_blocked", 0, table, "stopped"), ("requeue", True))
        self.assertEqual(retry.decide("risk_blocked", 1, table, "stopped"), ("reroute", True))

    def test_exec_error_fails_the_lane_on_the_third_attempt(self):
        table = retry.DEFAULT_TABLE
        self.assertEqual(retry.decide("exec_error", 0, table, "partial"), ("requeue", True))
        self.assertEqual(retry.decide("exec_error", 1, table, "partial"), ("requeue", True))
        self.assertEqual(retry.decide("exec_error", 2, table, "partial"), ("lane_failed", True))

    def test_missing_error_class_with_partial_or_stopped_status_is_exec_error(self):
        table = retry.DEFAULT_TABLE
        for status in ("stopped", "partial"):
            with self.subTest(status=status):
                self.assertEqual(retry.decide(None, 2, table, status), ("lane_failed", True))
                self.assertEqual(retry.decide("", 0, table, status), ("requeue", True))

    def test_missing_error_class_with_other_status_raises(self):
        with self.assertRaises(ValueError):
            retry.decide(None, 2, retry.DEFAULT_TABLE, "done")

    def test_vm_paused_requeues_without_counting(self):
        table = retry.DEFAULT_TABLE
        for attempts in (0, 1, 5):
            with self.subTest(attempts=attempts):
                self.assertEqual(retry.decide("vm_paused", attempts, table), ("requeue", False))

    def test_stalled_quota_and_cli_policies(self):
        table = retry.DEFAULT_TABLE
        self.assertEqual(retry.decide("stalled", 9, table), ("requeue", True))
        self.assertEqual(retry.decide("cli_too_old", 0, table), ("pause_lane", True))
        self.assertEqual(retry.decide("quota", 0, table), ("pause_lane", True))

    def test_unknown_class_uses_default_lane_failed(self):
        self.assertEqual(retry.decide("new_error", 0, retry.DEFAULT_TABLE), ("lane_failed", True))

    def test_default_table_has_reroute_in_actions(self):
        self.assertIn("reroute", retry.ACTIONS)


class MergeTableTests(unittest.TestCase):
    def test_override_changes_one_key_and_keeps_the_rest(self):
        merged = retry.merge_table(retry.DEFAULT_TABLE, {"quota": "lane_failed"})
        self.assertEqual(merged["quota"], "lane_failed")
        for key in retry.DEFAULT_TABLE:
            if key != "quota":
                self.assertEqual(merged[key], retry.DEFAULT_TABLE[key])
        self.assertEqual(retry.DEFAULT_TABLE["quota"], "pause_lane")

    def test_override_can_add_a_new_class(self):
        merged = retry.merge_table(retry.DEFAULT_TABLE, {"disk_full": {"max": 1, "then": "close"}})
        self.assertEqual(retry.decide("disk_full", 0, merged), ("requeue", True))
        self.assertEqual(retry.decide("disk_full", 1, merged), ("close", True))

    def test_invalid_override_raises_value_error(self):
        with self.assertRaises(ValueError):
            retry.merge_table(retry.DEFAULT_TABLE, {"quota": "retry"})

    def test_invalid_action_error_lists_every_action(self):
        with self.assertRaises(ValueError) as ctx:
            retry.merge_table(retry.DEFAULT_TABLE, {"quota": "retry"})
        for action in retry.ACTIONS:
            self.assertIn(action, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
