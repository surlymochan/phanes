"""Tests for lib/dispatch/weekly_reserve.py."""

import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "weekly_reserve.py"
spec = importlib.util.spec_from_file_location("weekly_reserve", MODULE_PATH)
weekly_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(weekly_mod)

weekly_throttle = weekly_mod.weekly_throttle
apply_throttle = weekly_mod.apply_throttle

NOW = 1_800_000_000
CFG = {"floor_pct": 10, "target_pct_per_day": 20}


class TestWeeklyThrottle(unittest.TestCase):
    def test_today_sample_reduces_dispatch(self):
        window = {"weekly_pct": 52, "weekly_resets_at": NOW + 2.7 * 86400}
        self.assertAlmostEqual(weekly_throttle(window, NOW, CFG), 52 / 2.7 / 20)

    def test_sufficient_reserve_keeps_full_dispatch(self):
        window = {"weekly_pct": 100, "weekly_resets_at": NOW + 5 * 86400}
        self.assertEqual(weekly_throttle(window, NOW, CFG), 1.0)

    def test_floor_stops_dispatch(self):
        for pct in (10, 5):
            self.assertEqual(
                weekly_throttle(
                    {"weekly_pct": pct, "weekly_resets_at": NOW + 86400}, NOW, CFG
                ),
                0.0,
            )

    def test_missing_fields_fail_open(self):
        self.assertEqual(weekly_throttle({}, NOW, CFG), 1.0)
        self.assertEqual(weekly_throttle({"weekly_pct": 50}, NOW, CFG), 1.0)
        self.assertEqual(weekly_throttle(None, NOW, CFG), 1.0)
        self.assertEqual(weekly_throttle({"weekly_pct": 50, "weekly_resets_at": NOW + 1}, NOW, {}), 1.0)

    def test_throttle_is_clamped_at_quarter(self):
        window = {"weekly_pct": 20, "weekly_resets_at": NOW + 10 * 86400}
        self.assertEqual(weekly_throttle(window, NOW, CFG), 0.25)


class TestApplyThrottle(unittest.TestCase):
    def test_floor_and_minimum(self):
        limits = {"concurrency": 7, "review": 3, "min_concurrency": 2, "other": "x"}
        self.assertEqual(
            apply_throttle(limits, 0.5),
            {"concurrency": 3, "review": 2, "min_concurrency": 2, "other": "x"},
        )
        self.assertEqual(limits["concurrency"], 7)

    def test_default_minimum_and_zero_factor(self):
        self.assertEqual(apply_throttle({"concurrency": 2, "review": 1}, 0.25),
                         {"concurrency": 1, "review": 1})
        self.assertEqual(apply_throttle({"concurrency": 4, "review": 2}, 0),
                         {"concurrency": 0, "review": 0})

    def test_compounds_with_burn_multiplier(self):
        limits = {"concurrency": 8, "review": 5}
        burn_factor = 0.5
        reserve_factor = weekly_throttle(
            {"weekly_pct": 52, "weekly_resets_at": NOW + 2.7 * 86400}, NOW, CFG
        )
        combined_factor = reserve_factor * burn_factor
        result = apply_throttle(limits, combined_factor)
        self.assertEqual(combined_factor, 52 / 2.7 / 20 * 0.5)
        self.assertEqual(result, {"concurrency": 3, "review": 2})


if __name__ == "__main__":
    unittest.main()
