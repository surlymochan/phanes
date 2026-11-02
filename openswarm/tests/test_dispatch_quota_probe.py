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


# Field semantics: ``current_*_usage_count`` is read as the REMAINING count.
# Assumption, not yet confirmed against a live response. The numbers follow
# the 2026-11-02 readings in the issue: 5h window 01:28->06:28 exhausted
# (0/1000 left), new window at 06:29 at 100%, weekly quota 52% left (5200/10000)
# until 11-04 01:05.
EXHAUSTED_BODY = json.dumps({
    "model_remains": [{
        "model_name": "MiniMax-M2",
        "start_time": ms(1, 28), "end_time": ms(6, 28),
        "remains_time": 0,
        "current_interval_total_count": 1000, "current_interval_usage_count": 0,
        "weekly_start_time": ms(1, 5, 2), "weekly_end_time": ms(1, 5, 4),
        "weekly_remains_time": 80000000,
        "current_weekly_total_count": 10000, "current_weekly_usage_count": 5200,
    }],
    "base_resp": {"status_code": 0, "status_msg": "success"},
})

FRESH_BODY = json.dumps({
    "model_remains": [{
        "model_name": "MiniMax-M2",
        "start_time": ms(6, 29), "end_time": ms(11, 29),
        "remains_time": 18000000,
        "current_interval_total_count": 1000, "current_interval_usage_count": 1000,
        "weekly_start_time": ms(1, 5, 2), "weekly_end_time": ms(1, 5, 4),
        "weekly_remains_time": 80000000,
        "current_weekly_total_count": 10000, "current_weekly_usage_count": 5200,
    }],
    "base_resp": {"status_code": 0, "status_msg": "success"},
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


class TestProbeMinimax(unittest.TestCase):
    def test_exhausted_window_real_semantics(self):
        out = probe.probe_minimax(fetch_returning(200, EXHAUSTED_BODY), KEY, NOW)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["error"])
        self.assertTrue(out["exhausted"])
        self.assertEqual(out["interval_pct"], 0)
        self.assertEqual(out["weekly_pct"], 52.0)
        self.assertEqual(out["interval_start"], sec(1, 28))
        self.assertEqual(out["resets_at"], sec(6, 28))
        self.assertEqual(out["weekly_resets_at"], sec(1, 5, 4))

    def test_fresh_window_real_semantics(self):
        out = probe.probe_minimax(fetch_returning(200, FRESH_BODY), KEY, NOW)
        self.assertTrue(out["ok"])
        self.assertFalse(out["exhausted"])
        self.assertEqual(out["interval_pct"], 100.0)
        self.assertEqual(out["weekly_pct"], 52.0)
        self.assertEqual(out["interval_start"], sec(6, 29))
        self.assertEqual(out["resets_at"], sec(11, 29))

    def test_weekly_exhausted_with_interval_left_is_exhausted(self):
        body = body_with(current_weekly_usage_count=0)
        out = probe.probe_minimax(fetch_returning(200, body), KEY, NOW)
        self.assertTrue(out["ok"])
        self.assertEqual(out["interval_pct"], 100.0)
        self.assertEqual(out["weekly_pct"], 0.0)
        self.assertTrue(out["exhausted"])

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
        text = FRESH_BODY.replace('"current_interval_usage_count": 1000', '"current_interval_usage_count": NaN')
        out = probe.probe_minimax(fetch_returning(200, text), KEY, NOW)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["exhausted"])

    def test_base_resp_error(self):
        body = json.dumps({"model_remains": [], "base_resp": {"status_code": 1004, "status_msg": "bad key " + KEY}})
        out = probe.probe_minimax(fetch_returning(200, body), KEY, NOW)
        self.assertFalse(out["ok"])
        self.assertIn("1004", out["error"])
        self.assertNotIn(KEY, out["error"])

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
        out = probe.probe_minimax(fetch_returning(200, json.dumps(body)), KEY, NOW)
        self.assertEqual(out["resets_at"], NOW + 18000)

    def test_weekly_reset_falls_back_to_remains_time_in_seconds(self):
        body = json.loads(FRESH_BODY)
        del body["model_remains"][0]["weekly_end_time"]
        body["model_remains"][0]["weekly_remains_time"] = 604800
        out = probe.probe_minimax(fetch_returning(200, json.dumps(body)), KEY, NOW)
        self.assertEqual(out["weekly_resets_at"], NOW + 604800)

    def test_remains_time_ms_is_not_read_as_seconds(self):
        body = json.loads(FRESH_BODY)
        del body["model_remains"][0]["end_time"]
        body["model_remains"][0]["remains_time"] = 18000000
        out = probe.probe_minimax(fetch_returning(200, json.dumps(body)), KEY, NOW)
        self.assertNotEqual(out["resets_at"], NOW + 18000000)
        self.assertEqual(out["resets_at"], NOW + 18000)


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
        code, _ = self.run_cli(argv, (200, body_with(current_weekly_usage_count=0)), env)
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
