"""Pi auto-retry must stop when the provider reports an exhausted usage quota."""

import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()


class PiRetryQuotaTests(unittest.TestCase):
    def test_minimax_token_plan_exhaustion_stops_with_rate_limit_summary(self):
        error = '{"type":"rate_limit_error","message":"已达到 Token Plan 用量上限…(2056)"}'
        retry, error_class, summary = xs.pi_retry_action(error)
        self.assertFalse(retry)
        self.assertEqual(error_class, "rate_limit")
        self.assertEqual(summary, error)

    def test_rpm_429_remains_retryable(self):
        self.assertEqual(xs.pi_retry_action("429 too many requests"), (True, None, None))

    def test_overloaded_remains_retryable(self):
        self.assertEqual(xs.pi_retry_action("overloaded_error"), (True, None, None))

    def test_english_usage_limit_stops_retry(self):
        retry, error_class, summary = xs.pi_retry_action("usage limit exceeded")
        self.assertFalse(retry)
        self.assertEqual(error_class, "rate_limit")
        self.assertEqual(summary, "usage limit exceeded")


if __name__ == "__main__":
    unittest.main()
