import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "mm-burn-rate.py"
spec = importlib.util.spec_from_file_location("mm_burn_rate", SCRIPT)
mmbr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mmbr)


def row(ts, pct, end="2026-11-04T01:05:00+08:00"):
    return {"ts": ts, "minimax": {"weekly_pct": pct, "weekly_end": end,
                                    "interval_pct": 80, "interval_end": "2026-11-02T11:00:00+08:00"}}


class MiniMaxBurnRateTests(unittest.TestCase):
    def test_two_samples_estimate_burn_and_5h_usage(self):
        points = [
            {"ts": mmbr.parse_ts("2026-11-02T00:16:00+08:00"), "weekly_pct": 67,
             "weekly_end": mmbr.parse_ts("2026-11-04T01:05:00+08:00")},
            {"ts": mmbr.parse_ts("2026-11-02T06:28:00+08:00"), "weekly_pct": 52,
             "weekly_end": mmbr.parse_ts("2026-11-04T01:05:00+08:00")},
        ]
        result = mmbr.analyze(points)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["burn_pct_per_hour"], 15 / (6 + 12 / 60), places=4)
        self.assertAlmostEqual(result["five_hour_pct"], result["burn_pct_per_hour"] * 5, places=3)
        self.assertTrue(result["alert"])
        self.assertIsNotNone(result["exhaustion_at"])

    def test_week_reset_is_insufficient(self):
        points = [
            {"ts": mmbr.parse_ts("2026-11-02T05:00:00+08:00"), "weekly_pct": 2,
             "weekly_end": mmbr.parse_ts("2026-11-02T06:00:00+08:00")},
            {"ts": mmbr.parse_ts("2026-11-02T06:00:00+08:00"), "weekly_pct": 99,
             "weekly_end": mmbr.parse_ts("2026-11-09T06:00:00+08:00")},
        ]
        self.assertEqual(mmbr.analyze(points)["status"], "insufficient")

    def test_append_deduplicates_same_minute(self):
        with tempfile.TemporaryDirectory() as tmp:
            history, health = Path(tmp) / "history.jsonl", Path(tmp) / "health.json"
            health.write_text(json.dumps({"minimax": row("ignored", 52)["minimax"]}), encoding="utf-8")
            self.assertTrue(mmbr.append_health(history, health))
            self.assertFalse(mmbr.append_health(history, health))
            self.assertEqual(len(history.read_text(encoding="utf-8").splitlines()), 1)

    def test_bad_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "history.jsonl"
            valid = row("2026-11-02T05:00:00+08:00", 52)
            history.write_text("not json\n[]\n" + json.dumps(valid) + "\n", encoding="utf-8")
            points = mmbr.read_history(history)
            self.assertEqual(len(points), 1)
            self.assertEqual(mmbr.analyze(points)["status"], "insufficient")


if __name__ == "__main__":
    unittest.main()
