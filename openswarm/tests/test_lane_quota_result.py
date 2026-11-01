"""Lane rate-limit results carry resets_at / provider_code parsed from the last provider error (#1544)."""

import datetime as dt
import pathlib
import sys
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

MINIMAX_TEXT = ('{"type":"rate_limit_error","message":"已达到 Token Plan 用量上限…(2056)"}')
NOW_TS = 1_700_000_000.0


def attempts_with(error_tail):
    return [{"backend": "pi", "rc": 75, "error_class": "rate_limit", "error_tail": error_tail}]


class LaneQuotaResultTests(unittest.TestCase):
    def test_minimax_2056_yields_provider_code_without_resets_at(self):
        fields = xs.lane_quota_fields("rate_limit", attempts_with(MINIMAX_TEXT))
        self.assertEqual(fields, {"provider_code": "2056"})

    def test_agy_resource_exhausted_yields_provider_code(self):
        text = "429 RESOURCE_EXHAUSTED: quota exceeded for model"
        self.assertEqual(xs.lane_quota_fields("rate_limit", attempts_with(text)), {"provider_code": "RESOURCE_EXHAUSTED"})

    def test_resets_in_composite_duration_yields_iso_z(self):
        text = "usage limit reached, resets in 1h20m"
        with mock.patch.object(xs, "NOW", return_value=NOW_TS):
            fields = xs.lane_quota_fields("rate_limit", attempts_with(text))
        expected = dt.datetime.fromtimestamp(NOW_TS + 4800, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(fields, {"resets_at": expected})
        self.assertEqual(fields["resets_at"], "2023-11-14T23:33:20Z")
        self.assertNotIn("provider_code", fields)

    def test_retry_after_header_yields_resets_at(self):
        text = "HTTP 429 Retry-After: 120"
        with mock.patch.object(xs, "NOW", return_value=NOW_TS):
            fields = xs.lane_quota_fields("rate_limit", attempts_with(text))
        self.assertEqual(fields["resets_at"], "2023-11-14T22:15:20Z")

    def test_unparseable_rate_limit_writes_no_fields(self):
        self.assertEqual(xs.lane_quota_fields("rate_limit", attempts_with("provider request failed")), {})
        self.assertEqual(xs.lane_quota_fields("rate_limit", [{"error_class": "rate_limit"}]), {})

    def test_non_rate_limit_result_has_no_quota_fields(self):
        for error_class in ("exec_error", "auth", "stalled", None):
            self.assertEqual(xs.lane_quota_fields(error_class, attempts_with(MINIMAX_TEXT)), {}, error_class)

    def test_lane_status_row_exposes_last_quota(self):
        lane = {"name": "L1", "backend": "pi", "model": "fake-model"}
        state = {"state": "idle", "supervisor_pid": 42,
                 "last_quota": {"provider_code": "2056", "resets_at": "2023-11-14T23:20:00Z"}}
        with mock.patch.object(xs, "lane_config", return_value=[lane]), \
                mock.patch.object(xs, "jload", return_value=state), \
                mock.patch.object(xs, "pid_alive", return_value=True), \
                mock.patch.object(xs, "execution_provider", return_value="fake-provider"), \
                mock.patch.object(xs, "load_policy", return_value={}):
            row = xs.lane_status_rows()[0]
        self.assertEqual(row["provider_code"], "2056")
        self.assertEqual(row["resets_at"], "2023-11-14T23:20:00Z")

    def test_lane_status_row_without_quota_has_no_quota_keys(self):
        lane = {"name": "L1", "backend": "pi", "model": "fake-model"}
        with mock.patch.object(xs, "lane_config", return_value=[lane]), \
                mock.patch.object(xs, "jload", return_value={"state": "idle", "supervisor_pid": 42}), \
                mock.patch.object(xs, "pid_alive", return_value=True), \
                mock.patch.object(xs, "execution_provider", return_value="fake-provider"), \
                mock.patch.object(xs, "load_policy", return_value={}):
            row = xs.lane_status_rows()[0]
        self.assertNotIn("resets_at", row)
        self.assertNotIn("provider_code", row)


if __name__ == "__main__":
    unittest.main()
