import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()
xs.__path__ = [str(ROOT)]
sys.modules["openswarm"] = xs


class PiRetryQuotaTests(unittest.TestCase):
    def test_token_plan_2056_is_terminal_rate_limit_with_original_summary(self):
        message = ('HTTP 429 {"type":"rate_limit_error","message":"已达到 Token Plan 用量上限…(2056)"}')

        self.assertTrue(xs.PI_QUOTA_EXHAUSTED_RE.search(message))
        self.assertFalse(xs.pi_error_is_retryable(message))
        self.assertTrue(xs.RATE_LIMIT_RE.search(message))
        self.assertIn("Token Plan", xs.short(message, 200))

    def test_rpm_429_remains_retryable(self):
        message = "429 too many requests"
        self.assertFalse(xs.PI_QUOTA_EXHAUSTED_RE.search(message))
        self.assertTrue(xs.pi_error_is_retryable(message))

    def test_overloaded_remains_retryable(self):
        self.assertTrue(xs.pi_error_is_retryable("529 overloaded_error: Overloaded"))

    def test_english_usage_limit_is_terminal(self):
        message = "usage limit exceeded"
        self.assertTrue(xs.PI_QUOTA_EXHAUSTED_RE.search(message))
        self.assertFalse(xs.pi_error_is_retryable(message))
        self.assertTrue(xs.RATE_LIMIT_RE.search(message))


if __name__ == "__main__":
    unittest.main()
