import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
