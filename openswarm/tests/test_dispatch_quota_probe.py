"""Tests for lib/dispatch/quota_probe.py (read-only MiniMax quota probe)."""

import contextlib
import importlib.util
import io
import json
import os
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

PROBE_PATH = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "quota_probe.py"
spec = importlib.util.spec_from_file_location("quota_probe", PROBE_PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

KEY = "sk-cp-TESTSECRET-0123456789"
TZ = timezone(timedelta(hours=8))


def ms(hh, mm, day=2):
    return int(datetime(2026, 11, day, hh, mm, tzinfo=TZ).timestamp() * 1000)


def sec(hh, mm, day=2):
    return ms(hh, mm, day) // 1000


# The two samples of the coding_plan/remains endpoint that the issue reports:
#
# * EXHAUSTED_BODY -- the 5h window of 2026-11-02 that ran out (01:28 ->
#   06:28) while the week still had 52% left (until 11-04 01:05);
# * FRESH_BODY -- the window that started at 06:29 with 100% left, the week
#   still at 52%.
#
# The field names and the direction of the reading are the ones the
# repository already merged for the same endpoint: ``lib/dispatch/quota.py``
# (issue #1536, PR #1584) reads ``current_interval_remaining_percent`` and
# ``current_weekly_remaining_percent`` as the REMAINING share of the 5h
# window and of the weekly window, and ``end_time`` / ``weekly_end_time`` as
# the window ends in epoch milliseconds.  This probe reads the same fields,
# so the repository has one reading of the endpoint.
#
# The live capture of 2026-11-02 06:28 could not be taken in this sandbox
# (no network, the API key is out of scope), so the values are the readings
# of the issue.  The direction is pinned by the tests below: the spent window
# reads 0% and the fresh window 100%, and a flipped reading breaks them.
EXHAUSTED_BODY = json.dumps({
    "base_resp": {"status_code": 0, "status_msg": "success"},
    "model_remains": [{
        "model_name": "MiniMax-M2.7",
        "current_interval_remaining_percent": 0,
        "current_weekly_remaining_percent": 52,
        "end_time": ms(6, 28),
        "weekly_end_time": ms(1, 5, 4),
    }],
})

FRESH_BODY = json.dumps({
    "base_resp": {"status_code": 0, "status_msg": "success"},
    "model_remains": [{
        "model_name": "MiniMax-M2.7",
        "current_interval_remaining_percent": 100,
        "current_weekly_remaining_percent": 52,
        "end_time": ms(11, 29),
        "weekly_end_time": ms(1, 5, 4),
    }],
})

NOW = sec(6, 30)


def fetch_returning(status, text):
    def fetch(url, headers):
        fetch.calls.append((url, headers))
        return status, text
    fetch.calls = []
    return fetch


def fetch_raising(exc):
    def fetch(url, headers):
        raise exc
    return fetch


def body_with(**entry_overrides):
    body = json.loads(FRESH_BODY)
    body["model_remains"][0].update(entry_overrides)
    return json.dumps(body)


def entry_only(**entry_fields):
    """A response whose only quota reading is the given entry fields."""
    return json.dumps({
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "model_remains": [dict({"model_name": "MiniMax-M2.7"}, **entry_fields)],
    })


class TestProbeMinimax(unittest.TestCase):
    """The probe reads the window state of the MiniMax Token Plan."""

    def test_spent_window_sample_reads_as_exhausted(self):
        out = probe.probe_minimax(fetch_returning(200, EXHAUSTED_BODY), KEY, NOW)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["error"])
        self.assertTrue(out["exhausted"])
        self.assertEqual(out["interval_pct"], 0.0)
        self.assertEqual(out["weekly_pct"], 52.0)
        self.assertEqual(out["resets_at"], sec(6, 28))
        self.assertEqual(out["weekly_resets_at"], sec(1, 5, 4))

    def test_new_window_sample_reads_as_available(self):
        out = probe.probe_minimax(fetch_returning(200, FRESH_BODY), KEY, NOW)
        self.assertTrue(out["ok"])
        self.assertFalse(out["exhausted"])
        self.assertEqual(out["interval_pct"], 100.0)
        self.assertEqual(out["weekly_pct"], 52.0)
        self.assertEqual(out["resets_at"], sec(11, 29))
        self.assertEqual(out["weekly_resets_at"], sec(1, 5, 4))

    def test_weekly_exhausted_resets_at_is_the_weekly_end(self):
        # The spent window decides when the plan may be used again: a plan
        # whose week is used up does not come back at the end of the 5h window.
        body = body_with(current_weekly_remaining_percent=0)
        out = probe.probe_minimax(fetch_returning(200, body), KEY, NOW)
        self.assertTrue(out["ok"])
        self.assertEqual(out["interval_pct"], 100.0)
        self.assertEqual(out["weekly_pct"], 0.0)
        self.assertTrue(out["exhausted"])
        self.assertEqual(out["resets_at"], sec(1, 5, 4))
        self.assertEqual(out["weekly_resets_at"], sec(1, 5, 4))

    def test_both_windows_exhausted_resets_at_is_the_interval_end(self):
        body = body_with(current_interval_remaining_percent=0,
                         current_weekly_remaining_percent=0)
        out = probe.probe_minimax(fetch_returning(200, body), KEY, NOW)
        self.assertTrue(out["exhausted"])
        self.assertEqual(out["resets_at"], sec(11, 29))

    def test_interval_start_is_none_without_a_window_start(self):
        out = probe.probe_minimax(fetch_returning(200, FRESH_BODY), KEY, NOW)
        self.assertIsNone(out["interval_start"])

    def test_interval_start_is_read_when_the_response_carries_one(self):
        body = body_with(start_time=ms(6, 29))
        out = probe.probe_minimax(fetch_returning(200, body), KEY, NOW)
        self.assertEqual(out["interval_start"], sec(6, 29))

    def test_model_remains_is_found_in_a_nested_container(self):
        for container in ("coding_plan", "data", "result"):
            body = json.loads(FRESH_BODY)
            remains = body.pop("model_remains")
            body[container] = {"model_remains": remains}
            out = probe.probe_minimax(fetch_returning(200, json.dumps(body)), KEY, NOW)
            self.assertTrue(out["ok"], container)
            self.assertEqual(out["interval_pct"], 100.0, container)

    def test_the_whole_payload_may_be_nested(self):
        body = json.loads(FRESH_BODY)
        out = probe.probe_minimax(fetch_returning(200, json.dumps({"coding_plan": body})),
                                  KEY, NOW)
        self.assertTrue(out["ok"])
        self.assertEqual(out["interval_pct"], 100.0)

    def test_count_fields_are_read_as_used_counts(self):
        # The count fields are only read when the percentages are missing, and
        # they are read as USED counts: the spent window has used everything.
        spent = probe.probe_minimax(
            fetch_returning(200, entry_only(current_interval_usage_count=1000,
                                            current_interval_total_count=1000)),
            KEY, NOW)
        self.assertTrue(spent["ok"])
        self.assertEqual(spent["interval_pct"], 0.0)
        self.assertTrue(spent["exhausted"])
        fresh = probe.probe_minimax(
            fetch_returning(200, entry_only(current_interval_usage_count=0,
                                            current_interval_total_count=1000)),
            KEY, NOW)
        self.assertTrue(fresh["ok"])
        self.assertEqual(fresh["interval_pct"], 100.0)
        self.assertFalse(fresh["exhausted"])

    def test_percentage_fields_win_over_the_count_fields(self):
        body = body_with(current_interval_usage_count=0,
                         current_interval_total_count=1000)
        out = probe.probe_minimax(fetch_returning(200, body), KEY, NOW)
        self.assertEqual(out["interval_pct"], 100.0)

    def test_missing_quota_fields_fail_closed(self):
        out = probe.probe_minimax(fetch_returning(200, entry_only(model_name="x")), KEY, NOW)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["interval_pct"])

    def test_failure_returns_full_key_set(self):
        out = probe.probe_minimax(fetch_returning(500, "boom"), KEY, NOW)
        self.assertEqual(set(out), {"ok", "error", "interval_pct", "weekly_pct", "interval_start",
                                    "resets_at", "weekly_resets_at", "exhausted"})
        self.assertIsNone(out["interval_pct"])
        self.assertIsNone(out["resets_at"])

    def test_key_only_in_authorization_header(self):
        fetch = fetch_returning(200, FRESH_BODY)
        out = probe.probe_minimax(fetch, KEY, NOW)
        url, headers = fetch.calls[0]
        self.assertEqual(url, probe.MINIMAX_REMAINS_URL)
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertNotIn(KEY, url)
        self.assertNotIn(KEY, json.dumps(out))

    def test_http_500_is_failure_not_exception(self):
        out = probe.probe_minimax(fetch_returning(500, "boom"), KEY, NOW)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "HTTP 500")

    def test_http_429_is_failure(self):
        body = json.dumps({"type": "rate_limit_error", "message": "已达到 Token Plan 用量上限"})
        out = probe.probe_minimax(fetch_returning(429, body), KEY, NOW)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "HTTP 429")

    def test_bad_json(self):
        out = probe.probe_minimax(fetch_returning(200, "{not json"), KEY, NOW)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "invalid JSON")

    def test_nan_is_not_treated_as_quota(self):
        text = FRESH_BODY.replace('"current_interval_remaining_percent": 100',
                                  '"current_interval_remaining_percent": NaN')
        out = probe.probe_minimax(fetch_returning(200, text), KEY, NOW)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["exhausted"])

    def test_base_resp_error(self):
        body = json.dumps({"model_remains": [], "base_resp": {"status_code": 1004, "status_msg": "bad key " + KEY}})
        out = probe.probe_minimax(fetch_returning(200, body), KEY, NOW)
        self.assertFalse(out["ok"])
        self.assertIn("1004", out["error"])
        self.assertNotIn(KEY, out["error"])

    def test_truncated_key_is_scrubbed(self):
        body = json.dumps({"model_remains": [], "base_resp": {"status_code": 1004,
                                                              "status_msg": "key " + KEY[:12]}})
        out = probe.probe_minimax(fetch_returning(200, body), KEY, NOW)
        self.assertFalse(out["ok"])
        self.assertNotIn(KEY[:12], out["error"])

    def test_base_resp_string_zero_is_success(self):
        body = json.loads(FRESH_BODY)
        body["base_resp"]["status_code"] = "0"
        out = probe.probe_minimax(fetch_returning(200, json.dumps(body)), KEY, NOW)
        self.assertTrue(out["ok"])

    def test_missing_base_resp_is_failure(self):
        body = json.loads(FRESH_BODY)
        del body["base_resp"]
        out = probe.probe_minimax(fetch_returning(200, json.dumps(body)), KEY, NOW)
        self.assertFalse(out["ok"])

    def test_missing_model_remains(self):
        out = probe.probe_minimax(fetch_returning(200, json.dumps({"base_resp": {"status_code": 0}})), KEY, NOW)
        self.assertFalse(out["ok"])

    def test_network_error_does_not_leak_key(self):
        out = probe.probe_minimax(fetch_raising(OSError("connect failed for " + KEY)), KEY, NOW)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "network error: OSError")
        self.assertNotIn(KEY, json.dumps(out))

    def test_resets_at_falls_back_to_remains_time_in_ms(self):
        body = json.loads(FRESH_BODY)
        del body["model_remains"][0]["end_time"]
        body["model_remains"][0]["remains_time"] = 18000000
        out = probe.probe_minimax(fetch_returning(200, json.dumps(body)), KEY, NOW)
        self.assertEqual(out["resets_at"], NOW + 18000)

    def test_two_hour_window_in_ms_is_not_read_as_seconds(self):
        body = json.loads(FRESH_BODY)
        del body["model_remains"][0]["end_time"]
        body["model_remains"][0]["remains_time"] = 7200000
        out = probe.probe_minimax(fetch_returning(200, json.dumps(body)), KEY, NOW)
        self.assertEqual(out["resets_at"], NOW + 7200)

    def test_weekly_reset_falls_back_to_remains_time_in_seconds(self):
        body = json.loads(FRESH_BODY)
        del body["model_remains"][0]["weekly_end_time"]
        body["model_remains"][0]["weekly_remains_time"] = 604800
        out = probe.probe_minimax(fetch_returning(200, json.dumps(body)), KEY, NOW)
        self.assertEqual(out["weekly_resets_at"], NOW + 604800)

    def test_weekly_reset_falls_back_to_remains_time_in_ms(self):
        body = json.loads(FRESH_BODY)
        del body["model_remains"][0]["weekly_end_time"]
        body["model_remains"][0]["weekly_remains_time"] = 237600000
        out = probe.probe_minimax(fetch_returning(200, json.dumps(body)), KEY, NOW)
        self.assertEqual(out["weekly_resets_at"], NOW + 237600)


class TestRedirect(unittest.TestCase):
    def test_cross_host_redirect_is_refused(self):
        req = urllib.request.Request(probe.MINIMAX_REMAINS_URL, headers={"Authorization": "Bearer x"})
        handler = probe._SameHostRedirect()
        self.assertIsNone(handler.redirect_request(req, None, 302, "Found", None, "https://evil.example/steal"))

    def test_same_host_redirect_is_followed(self):
        req = urllib.request.Request(probe.MINIMAX_REMAINS_URL, headers={"Authorization": "Bearer x"})
        handler = probe._SameHostRedirect()
        new = handler.redirect_request(req, None, 302, "Found", None,
                                       "https://www.minimaxi.com/v1/api/openplatform/other")
        self.assertIsNotNone(new)


class TestCli(unittest.TestCase):
    def run_cli(self, argv, fetch_result=None, env=None):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(probe, "_http_fetch", return_value=fetch_result), \
                contextlib.redirect_stdout(buf):
            code = probe.main(argv)
        return code, buf.getvalue()

    def test_exit_codes(self):
        argv = ["--minimax", "--key-env", "MINIMAX_CN_API_KEY"]
        env = {"MINIMAX_CN_API_KEY": KEY}
        code, out = self.run_cli(argv, (200, FRESH_BODY), env)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])
        code, _ = self.run_cli(argv, (200, EXHAUSTED_BODY), env)
        self.assertEqual(code, 3)
        code, _ = self.run_cli(argv, (200, body_with(current_weekly_remaining_percent=0)), env)
        self.assertEqual(code, 3)
        code, out = self.run_cli(argv, (500, "x"), env)
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])

    def test_missing_env_fails_without_leaking(self):
        code, out = self.run_cli(["--minimax"], None, {})
        self.assertEqual(code, 2)
        self.assertNotIn(KEY, out)
        self.assertIn("MINIMAX_CN_API_KEY", out)

    def test_cli_output_never_contains_key(self):
        code, out = self.run_cli(["--minimax"], (200, FRESH_BODY), {"MINIMAX_CN_API_KEY": KEY})
        self.assertNotIn(KEY, out)


if __name__ == "__main__":
    unittest.main()
