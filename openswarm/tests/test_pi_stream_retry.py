import os
import pathlib
import sys
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()
xs.__path__ = [str(ROOT)]
sys.modules["openswarm"] = xs


class PiStreamRetryTests(unittest.TestCase):
    def test_today_incident_samples_are_retryable_stream_errors(self):
        samples = [
            "Anthropic stream ended before message_stop",
            "502 provider unavailable",
            "stream ended before message_stop",
            "ECONNRESET",
            "premature close",
        ]
        for msg in samples:
            self.assertTrue(xs.PI_RETRYABLE_RE.search(msg), f"PI_RETRYABLE_RE should match {msg}")
            self.assertTrue(xs.PI_STREAM_RETRY_RE.search(msg), f"PI_STREAM_RETRY_RE should match {msg}")
            self.assertEqual(xs.pi_retry_kind(msg), "stream", f"pi_retry_kind should return 'stream' for {msg}")
            self.assertFalse(xs.quota_exhausted_error(msg), f"quota_exhausted_error should be False for {msg}")

            action, err_cls, detail = xs.pi_stream_retry_decision("pi", "exec_error", msg, 0)
            self.assertEqual(action, "retry", f"Decision should retry for {msg} at retry 0")
            self.assertEqual(err_cls, "exec_error")
            self.assertEqual(detail, msg)

    def test_token_plan_2056_samples_are_quota_and_not_retryable(self):
        samples = [
            "已达到 Token Plan 用量上限 (2056)",
            'HTTP 429 {"type":"rate_limit_error","message":"已达到 Token Plan 用量上限…(2056)"}',
            "Token Plan exhausted",
            "insufficient quota",
            "quota exhausted",
        ]
        for msg in samples:
            self.assertTrue(xs.quota_exhausted_error(msg), f"quota_exhausted_error should be True for {msg}")
            self.assertEqual(xs.pi_retry_kind(msg), "quota", f"pi_retry_kind should return 'quota' for {msg}")

            # Stream retry decision stops on quota errors
            action, err_cls, detail = xs.pi_stream_retry_decision("pi", "exec_error", msg, 0)
            self.assertEqual(action, "stop", f"Decision should stop for quota {msg}")
            self.assertEqual(err_cls, "exec_error")
            self.assertEqual(detail, msg)

            # Rate limit branch excludes quota errors from retry
            can_retry_in_rate_limit = not xs.quota_exhausted_error(msg)
            self.assertFalse(can_retry_in_rate_limit, f"Rate-limit branch must not retry quota error: {msg}")

    def test_quota_precedence_over_stream_when_both_present(self):
        # #1535 takes precedence over #1594: non-transient quota must not be retried as a stream error
        mixed_error = "已达到 Token Plan 用量上限 (2056): stream ended before message_stop premature close"
        self.assertTrue(xs.quota_exhausted_error(mixed_error))
        self.assertEqual(xs.pi_retry_kind(mixed_error), "quota")
        action, err_cls, detail = xs.pi_stream_retry_decision("pi", "exec_error", mixed_error, 0)
        self.assertEqual(action, "stop")
        self.assertEqual(err_cls, "exec_error")
        self.assertEqual(detail, mixed_error)

    def test_stream_retry_limit_default_three_and_detail_formatting(self):
        msg = "Anthropic stream ended before message_stop"
        self.assertEqual(xs.pi_stream_retry_max(), 3)

        for retry_count in (0, 1, 2):
            action, err_cls, detail = xs.pi_stream_retry_decision("pi", "exec_error", msg, retry_count)
            self.assertEqual(action, "retry", f"Should retry on attempt {retry_count}")
            self.assertEqual(err_cls, "exec_error")
            self.assertEqual(detail, msg)

        # At max (3), it must stop with exec_error and detail containing stream_retries=3
        action, err_cls, detail = xs.pi_stream_retry_decision("pi", "exec_error", msg, 3)
        self.assertEqual(action, "stop")
        self.assertEqual(err_cls, "exec_error")
        self.assertEqual(detail, f"{msg} (stream_retries=3)")

        # Beyond max
        action, err_cls, detail = xs.pi_stream_retry_decision("pi", "exec_error", msg, 4)
        self.assertEqual(action, "stop")
        self.assertEqual(err_cls, "exec_error")
        self.assertEqual(detail, f"{msg} (stream_retries=4)")

    def test_stream_retry_custom_max_and_env_var(self):
        msg = "502 provider unavailable"
        # Explicit max_retries argument
        action, err_cls, detail = xs.pi_stream_retry_decision("pi", "exec_error", msg, 1, max_retries=1)
        self.assertEqual(action, "stop")
        self.assertEqual(err_cls, "exec_error")
        self.assertEqual(detail, f"{msg} (stream_retries=1)")

        # Via PI_STREAM_RETRY_MAX env var
        with mock.patch.dict(os.environ, {"PI_STREAM_RETRY_MAX": "5"}):
            self.assertEqual(xs.pi_stream_retry_max(), 5)
            action, _, _ = xs.pi_stream_retry_decision("pi", "exec_error", msg, 4)
            self.assertEqual(action, "retry")
            action, err_cls, detail = xs.pi_stream_retry_decision("pi", "exec_error", msg, 5)
            self.assertEqual(action, "stop")
            self.assertEqual(detail, f"{msg} (stream_retries=5)")

    def test_cannot_retry_stops_immediately_with_retries_noted(self):
        msg = "Anthropic stream ended before message_stop"
        action, err_cls, detail = xs.pi_stream_retry_decision("pi", "exec_error", msg, 1, can_retry=False)
        self.assertEqual(action, "stop")
        self.assertEqual(err_cls, "exec_error")
        self.assertEqual(detail, f"{msg} (stream_retries=1)")

    def test_non_pi_backend_and_non_exec_error_not_affected(self):
        msg = "Anthropic stream ended before message_stop"
        # Non-pi backend
        action, err_cls, detail = xs.pi_stream_retry_decision("codex", "exec_error", msg, 0)
        self.assertEqual(action, "stop")
        self.assertEqual(err_cls, "exec_error")
        self.assertEqual(detail, msg)

        # Non-exec_error
        action, err_cls, detail = xs.pi_stream_retry_decision("pi", "timeout", msg, 0)
        self.assertEqual(action, "stop")
        self.assertEqual(err_cls, "timeout")
        self.assertEqual(detail, msg)

    def test_pi_stream_retry_delays(self):
        self.assertEqual(xs.pi_stream_retry_delay(1), 5.0)
        self.assertEqual(xs.pi_stream_retry_delay(2), 10.0)
        self.assertEqual(xs.pi_stream_retry_delay(3), 15.0)
        self.assertEqual(xs.pi_stream_retry_delay(6), 30.0)
        self.assertEqual(xs.pi_stream_retry_delay(10), 30.0)

    def test_lane_wait_stream_retry_aborts_when_child_not_alive(self):
        child = mock.Mock()
        child.alive.return_value = False
        spec = {"started": 100.0, "budget_minutes": 10}
        with mock.patch.object(xs, "NOW", return_value=105.0), \
             mock.patch.object(xs, "lane_control_messages"):
            res = xs.lane_wait_stream_retry(10, {}, spec, child, mock.Mock(), 1, [])
            self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
