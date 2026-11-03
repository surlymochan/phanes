"""#1710: the canonical run-ending reason enumeration (lib/dispatch/failure_reason.py)."""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "failure_reason_under_test", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "failure_reason.py"))
FR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FR)


class FailureReasonTests(unittest.TestCase):
    """Every ended run carries one reason of the enumeration; an unknown cause keeps its raw error."""

    def test_the_enumeration_is_fixed(self):
        self.assertEqual(set(FR.RUN_FAILURE_REASONS),
                         {"spawn-failed", "fetch-failed", "timeout", "oom-killed",
                          "exec-error", "budget-exceeded", "rate-limit", "unknown"})

    def test_error_classes_map_onto_the_enumeration(self):
        cases = {"spawn_failed": "spawn-failed", "prepare_failed": "spawn-failed",
                 "worktree_unavailable": "spawn-failed", "fetch_failed": "fetch-failed",
                 "timeout": "timeout", "timed_out": "timeout", "stalled": "timeout",
                 "oom": "oom-killed", "oom_killed": "oom-killed",
                 "exec_error": "exec-error", "executor_io_lost": "exec-error",
                 "budget_exceeded": "budget-exceeded", "budget_exceeded_daily": "budget-exceeded",
                 "rate_limit": "rate-limit", "quota": "rate-limit"}
        for source, expected in cases.items():
            with self.subTest(source=source):
                reason = FR.classify(source, "raw detail")
                self.assertEqual(reason, expected)
                self.assertIn(reason, FR.RUN_FAILURE_REASONS)

    def test_session_derivation_failure_is_a_spawn_failure(self):
        for text in ("session derivation failed: denied", "could not spawn the executor session",
                     "failed to derive session id", "会话派生失败：拒绝"):
            with self.subTest(text=text):
                self.assertEqual(FR.classify("exec_error", text), "spawn-failed")

    def test_the_raw_error_selects_the_reason(self):
        self.assertEqual(FR.classify(None, "connection timed out"), "timeout")
        self.assertEqual(FR.classify(None, "JavaScript heap out of memory"), "oom-killed")
        self.assertEqual(FR.classify(None, "upstream HTTP 429 rate limit"), "rate-limit")
        self.assertEqual(FR.classify(None, "git fetch failed: could not resolve host"), "fetch-failed")

    def test_an_unknown_cause_keeps_the_raw_error(self):
        self.assertEqual(FR.classify("brand_new_class", "something odd"), "unknown")
        self.assertEqual(FR.tail("something odd"), "something odd")
        self.assertEqual(FR.classify(None, None), "unknown")
        self.assertEqual(FR.tail(None), "原因缺失")
        self.assertEqual(FR.tail(None, 7), "exit code 7")

    def test_canonical_rejects_empty_and_foreign_reasons(self):
        for reason in (None, "", "   ", "not-a-reason"):
            with self.subTest(reason=reason):
                self.assertIn(FR.canonical(reason), FR.RUN_FAILURE_REASONS)
                self.assertNotEqual(FR.canonical(reason), "")
        self.assertEqual(FR.canonical("fetch_failed"), "fetch-failed")
        self.assertEqual(FR.canonical("unknown: some tail"), "unknown")
        self.assertEqual(FR.canonical(""), "unknown")
