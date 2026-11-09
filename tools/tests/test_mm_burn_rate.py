import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "mm-burn-rate.py"
spec = importlib.util.spec_from_file_location("mm_burn_rate", SCRIPT)
mmbr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mmbr)

END = "2026-11-04T01:05:00+08:00"
NOW = mmbr.parse_ts("2026-11-02T06:40:00+08:00")


def row(ts, pct, end=END):
    return {"ts": ts, "minimax": {"weekly_pct": pct, "weekly_end": end,
                                    "interval_pct": 80, "interval_end": "2026-11-02T11:00:00+08:00"}}


def point(ts, pct, end=END):
    return {"ts": mmbr.parse_ts(ts), "weekly_pct": pct, "weekly_end": mmbr.parse_ts(end)}


class MiniMaxBurnRateTests(unittest.TestCase):
    def test_two_samples_estimate_burn_and_5h_usage(self):
        points = [point("2026-11-02T00:16:00+08:00", 67), point("2026-11-02T06:28:00+08:00", 52)]
        result = mmbr.analyze(points, now=NOW)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["burn_pct_per_hour"], 15 / (6 + 12 / 60), places=4)
        self.assertAlmostEqual(result["five_hour_pct"], result["burn_pct_per_hour"] * 5, places=3)
        self.assertAlmostEqual(result["window_hours"], 6 + 12 / 60, places=2)
        self.assertEqual(result["newest_ts"], "2026-11-02T06:28:00+08:00")
        self.assertEqual(result["sample_age_s"], 720)
        self.assertTrue(result["alert"])
        self.assertIsNotNone(result["exhaustion_at"])
        self.assertNotIn(".", result["exhaustion_at"])

    def test_week_reset_is_insufficient(self):
        points = [
            point("2026-11-02T05:00:00+08:00", 2, end="2026-11-02T06:00:00+08:00"),
            point("2026-11-02T06:00:00+08:00", 99, end="2026-11-09T06:00:00+08:00"),
        ]
        now = mmbr.parse_ts("2026-11-02T06:05:00+08:00")
        self.assertEqual(mmbr.analyze(points, now=now)["status"], "insufficient")

    def test_append_deduplicates_same_minute(self):
        with tempfile.TemporaryDirectory() as tmp:
            history, health = Path(tmp) / "history.jsonl", Path(tmp) / "health.json"
            health.write_text(json.dumps({"minimax": row("ignored", 52)["minimax"]}), encoding="utf-8")
            self.assertTrue(mmbr.append_health(history, health, now=NOW))
            self.assertFalse(mmbr.append_health(history, health, now=NOW + timedelta(seconds=30)))
            self.assertTrue(mmbr.append_health(history, health, now=NOW + timedelta(minutes=1)))
            self.assertEqual(len(history.read_text(encoding="utf-8").splitlines()), 2)

    def test_bad_lines_are_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "history.jsonl"
            valid = row("2026-11-02T05:00:00+08:00", 52)
            string_pct = row("2026-11-02T05:01:00+08:00", "52%")
            bool_pct = row("2026-11-02T05:02:00+08:00", True)
            history.write_text("not json\n[]\n\n" + json.dumps(string_pct) + "\n"
                               + json.dumps(bool_pct) + "\n" + json.dumps(valid) + "\n",
                               encoding="utf-8")
            points, skipped = mmbr.read_history(history)
            self.assertEqual(len(points), 1)
            self.assertEqual(skipped, 4)
            now = mmbr.parse_ts("2026-11-02T05:05:00+08:00")
            self.assertEqual(mmbr.analyze(points, now=now)["status"], "insufficient")

    def test_sparse_history_does_not_fake_six_hour_rate(self):
        points = [point("2026-11-01T10:00:00+08:00", 70), point("2026-11-02T05:00:00+08:00", 52)]
        result = mmbr.analyze(points, now=mmbr.parse_ts("2026-11-02T05:05:00+08:00"))
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["points"], 1)

    def test_stale_sample_never_alerts(self):
        points = [point("2026-10-31T04:00:00+08:00", 60), point("2026-10-31T05:00:00+08:00", 52)]
        result = mmbr.analyze(points, now=NOW)
        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["alert"])
        self.assertIsNone(result["exhaustion_at"])
        self.assertEqual(result["newest_ts"], "2026-10-31T05:00:00+08:00")

    def test_missing_or_empty_history_is_insufficient(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.jsonl"
            empty = Path(tmp) / "empty.jsonl"
            empty.write_text("", encoding="utf-8")
            for path in (missing, empty):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = mmbr.main(["--history", str(path)])
                self.assertEqual(code, 0)
                self.assertEqual(out.getvalue().strip(), "insufficient (points=0, skipped=0)")

    def test_append_rejects_unreadable_minimax(self):
        with tempfile.TemporaryDirectory() as tmp:
            history, health = Path(tmp) / "history.jsonl", Path(tmp) / "health.json"
            health.write_text(json.dumps({"minimax": {"weekly_pct": "52%", "weekly_end": END}}),
                              encoding="utf-8")
            with self.assertRaises(ValueError):
                mmbr.append_health(history, health, now=NOW)
            self.assertFalse(history.exists())

    def test_health_top_level_not_object_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            history, health = Path(tmp) / "history.jsonl", Path(tmp) / "health.json"
            health.write_text("[]", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = mmbr.main(["--history", str(history), "--append", str(health)])
            self.assertEqual(code, 2)
            self.assertIn("error:", err.getvalue())
            self.assertFalse(history.exists())


class CollectorIntegrityTests(unittest.TestCase):
    def setUp(self):
        (SCRIPT.parent.parent / ".swrm-tmp").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / ".swrm-tmp")
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "broker.requests.jsonl"
        self.now = mmbr.datetime.now(mmbr.timezone.utc)

    def heartbeat(self, age=0, **fields):
        path = mmbr.heartbeat_path(self.log)
        path.write_text(json.dumps({"ts": self.now.isoformat(),
                                    "coverage_start": (self.now - timedelta(hours=6)).isoformat(), **fields}))
        stamp = (self.now - timedelta(seconds=age)).timestamp()
        os.utime(path, (stamp, stamp))

    def reading(self):
        return mmbr.token_reading(mmbr.broker_logs(str(self.log)), now=self.now)

    def test_stopped_heartbeat_overrides_recent_log(self):
        self.log.write_text("")
        self.heartbeat(age=300)
        result = self.reading()
        self.assertEqual(result["token_state"], "stale")
        self.assertIsNone(result["hourly_tokens"])
        self.assertFalse(result["token_alert"])

    def test_new_quiet_collector_has_insufficient_coverage(self):
        self.log.write_text("")
        self.heartbeat(coverage_start=self.now.isoformat())
        result = self.reading()
        self.assertEqual(result["token_state"], "incomplete")
        self.assertIsNone(result["hourly_tokens"])
        self.assertFalse(result["token_alert"])
        self.assertTrue(all(bucket["tokens"] is None for bucket in result["hourly_buckets"]))

    def test_empty_legacy_log_cannot_establish_coverage(self):
        self.log.write_text("")
        mmbr.heartbeat_path(self.log).write_text(self.now.isoformat())
        result = self.reading()
        self.assertEqual(result["token_state"], "incomplete")
        self.assertIsNone(result["hourly_tokens"])
        self.assertFalse(result["token_alert"])

    def test_new_coverage_overrides_old_records_in_rotations(self):
        self.log.write_text("")
        Path(str(self.log) + ".1").write_text(json.dumps({
            "ts": (self.now - timedelta(hours=2)).isoformat(), "in_tok": 10, "out_tok": 10}))
        self.heartbeat(coverage_start=self.now.isoformat())
        result = self.reading()
        self.assertEqual(result["token_state"], "incomplete")
        self.assertIsNone(result["hourly_tokens"])
        self.assertFalse(result["token_alert"])

    def test_quiet_collector_alerts_only_after_a_full_hour(self):
        self.log.write_text("")
        for seconds, complete in ((3599, False), (3600, True)):
            with self.subTest(seconds=seconds):
                self.heartbeat(coverage_start=(self.now - timedelta(seconds=seconds)).isoformat())
                result = self.reading()
                self.assertEqual(result["token_state"], "idle" if complete else "incomplete")
                self.assertEqual(result["hourly_tokens"], 0 if complete else None)
                self.assertEqual(result["token_alert"], complete)

    def test_old_missing_usage_does_not_disable_current_low_burn_alert(self):
        self.log.write_text(json.dumps({"ts": (self.now - timedelta(days=2)).isoformat(),
                                        "status": 200}) + "\n" + json.dumps({
            "ts": self.now.isoformat(), "in_tok": 10, "out_tok": 10}) + "\n")
        self.heartbeat(coverage_start=(self.now - timedelta(days=3)).isoformat())
        result = self.reading()
        self.assertEqual(result["token_state"], "ok")
        self.assertEqual(result["hourly_tokens"], 20)
        self.assertTrue(result["token_alert"])

    def test_bad_usage_invalidates_only_the_windows_containing_it(self):
        self.now = self.now.replace(minute=30, second=0, microsecond=0)
        self.log.write_text(json.dumps({"ts": (self.now - timedelta(hours=2)).isoformat(),
                                        "status": 200}) + "\n" + json.dumps({
            "ts": self.now.isoformat(), "in_tok": 10, "out_tok": 10}) + "\n")
        self.heartbeat(coverage_start=(self.now - timedelta(hours=6)).isoformat())
        end = self.now + timedelta(hours=1)
        result = mmbr.token_reading(mmbr.broker_logs(str(self.log)), now=self.now,
                                   points=[{"interval_end": end}])
        self.assertEqual(result["token_state"], "ok")
        self.assertTrue(result["token_alert"])
        self.assertIsNone(result["interval_tokens"])
        self.assertIsNone(result["hourly_buckets"][-3]["tokens"])
        self.assertEqual(result["hourly_buckets"][-1]["tokens"], 20)

    def test_coverage_is_checked_for_each_clock_hour_and_plan_window(self):
        self.now = self.now.replace(minute=30, second=0, microsecond=0)
        self.log.write_text("")
        self.heartbeat(coverage_start=(self.now - timedelta(hours=1)).isoformat())
        end = self.now + timedelta(hours=1)
        result = mmbr.token_reading(mmbr.broker_logs(str(self.log)), now=self.now,
                                   points=[{"interval_end": end}])
        self.assertTrue(result["token_alert"])
        self.assertEqual(result["hourly_buckets"][-1]["tokens"], 0)
        self.assertTrue(all(bucket["tokens"] is None for bucket in result["hourly_buckets"][:-1]))
        self.assertIsNone(result["interval_tokens"])

    def test_fresh_heartbeat_without_log_is_not_zero_burn(self):
        self.heartbeat(audit_error_at=None)
        result = self.reading()
        self.assertEqual(result["token_collector"], "broken")
        self.assertNotIn(result["token_state"], ("ok", "idle"))
        self.assertIsNone(result["hourly_tokens"])
        self.assertFalse(result["token_alert"])

    def test_deleted_log_with_rotation_is_not_a_complete_reading(self):
        self.log.write_text(json.dumps({
            "ts": self.now.isoformat(), "in_tok": 1000, "out_tok": 1}) + "\n")
        Path(str(self.log) + ".1").write_text(self.log.read_text())
        self.heartbeat(audit_error_at=None)
        self.log.unlink()
        result = self.reading()
        self.assertEqual(result["token_collector"], "broken")
        self.assertIsNone(result["hourly_tokens"])
        self.assertFalse(result["token_alert"])

    def test_log_disappearing_after_liveness_check_is_incomplete(self):
        self.log.write_text("")
        self.heartbeat()
        logs = mmbr.broker_logs(str(self.log))
        self.log.unlink()
        with patch.object(mmbr, "collector_state", return_value="alive"):
            result = mmbr.token_reading(logs, now=self.now)
        self.assertEqual(result["token_state"], "incomplete")
        self.assertIsNone(result["hourly_tokens"])
        self.assertFalse(result["token_alert"])

    def test_audit_write_failure_is_not_zero_burn(self):
        self.heartbeat(audit_error_at=self.now.isoformat())
        result = self.reading()
        self.assertEqual(result["token_collector"], "broken")
        self.assertIsNone(result["hourly_tokens"])
        self.assertFalse(result["token_alert"])

    def test_corruption_and_missing_usage_are_not_zero_burn(self):
        self.heartbeat()
        for line in ("broken", json.dumps({"ts": self.now.isoformat(), "status": 200}),
                     json.dumps({"ts": self.now.isoformat(), "in_tok": -1, "out_tok": 0})):
            with self.subTest(line=line):
                self.log.write_text(line + "\n")
                result = self.reading()
                self.assertEqual(result["token_state"], "incomplete")
                self.assertIsNone(result["hourly_tokens"])
                self.assertFalse(result["token_alert"])

    def test_http_errors_without_usage_invalidate_reporting_windows(self):
        self.now = self.now.replace(minute=30, second=0, microsecond=0)
        self.heartbeat()
        for status in (400, 403, 429, 500, 502, 503):
            with self.subTest(status=status):
                # Includes the broker's 502 shape after an upstream connection
                # failure: status alone cannot prove the provider burned nothing.
                self.log.write_text(json.dumps({
                    "ts": self.now.isoformat(), "status": status,
                    "err": "provider unavailable", "resp_bytes": 0,
                    "in_tok": None, "out_tok": None}) + "\n")
                result = mmbr.token_reading(mmbr.broker_logs(str(self.log)), now=self.now,
                                           points=[{"interval_end": self.now + timedelta(hours=1)}])
                self.assertEqual(result["token_state"], "incomplete")
                self.assertFalse(result["token_window_complete"])
                self.assertEqual(result["token_skipped"], 1)
                self.assertIsNone(result["hourly_tokens"])
                self.assertFalse(result["token_alert"])
                self.assertFalse(result["hourly_buckets"][-1]["complete"])
                self.assertIsNone(result["hourly_buckets"][-1]["tokens"])
                self.assertFalse(result["interval_complete"])
                self.assertIsNone(result["interval_tokens"])

    def test_http_error_with_explicit_zero_usage_can_alert(self):
        self.log.write_text(json.dumps({"ts": self.now.isoformat(), "status": 429,
                                        "in_tok": 0, "out_tok": 0}) + "\n")
        self.heartbeat()
        result = self.reading()
        self.assertTrue(result["token_window_complete"])
        self.assertEqual(result["hourly_tokens"], 0)
        self.assertEqual(result["token_skipped"], 0)
        self.assertTrue(result["token_alert"])

    def test_all_rotations_are_read_and_gaps_detected(self):
        self.heartbeat()
        self.log.write_text("")
        for index in range(1, 8):
            Path(str(self.log) + f".{index}").write_text(json.dumps({
                "ts": self.now.isoformat(), "in_tok": 1000, "out_tok": 1}) + "\n")
        self.assertEqual(self.reading()["hourly_tokens"], 7007)
        self.assertEqual(self.reading()["token_state"], "ok")
        Path(str(self.log) + ".3").unlink()
        self.assertEqual(self.reading()["token_state"], "incomplete")
        self.assertFalse(self.reading()["token_alert"])

    def test_unreadable_history_still_publishes_token_alert(self):
        self.log.write_text("")
        self.heartbeat(coverage_start=(self.now - timedelta(hours=6)).isoformat())
        status = self.log.parent / "status.json"
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = mmbr.main(["--history", self.tmp.name, "--broker-log", str(self.log),
                              "--status-file", str(status), "--json"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(out.getvalue())["alert"])
        self.assertEqual(json.loads(status.read_text())["hourly_tokens"], 0)

    def test_default_linux_log_matches_swrm_fleet(self):
        target = Path("/run/swrm-pi/broker.sock.requests.jsonl")
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "exists", lambda p: p == target):
            self.assertEqual(mmbr.broker_logs()[0], target)


if __name__ == "__main__":
    unittest.main()
