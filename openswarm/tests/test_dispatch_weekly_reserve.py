"""Tests for weekly reserve dispatch throttling."""

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "weekly_reserve", ROOT / "lib" / "dispatch" / "weekly_reserve.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

weekly_throttle = mod.weekly_throttle
apply_throttle = mod.apply_throttle


class TestWeeklyThrottle(unittest.TestCase):
    def test_low_reserve_sample_scales_down(self):
        now = 1_800_000_000
        window = {"weekly_pct": 52, "weekly_resets_at": now + 2.7 * 86400}
        self.assertAlmostEqual(weekly_throttle(window, now, {
            "floor_pct": 5, "target_pct_per_day": 25,
        }), 52 / 2.7 / 25)

    def test_sufficient_reserve_is_unthrottled(self):
        now = 1_800_000_000
        self.assertEqual(weekly_throttle({"weekly_pct": 80, "weekly_resets_at": now + 5 * 86400}, now,
                                         {"floor_pct": 5, "target_pct_per_day": 15}), 1)

    def test_floor_stops_dispatch(self):
        self.assertEqual(weekly_throttle({"weekly_pct": 5, "weekly_resets_at": 1_900_000_000}, 1_800_000_000,
                                         {"floor_pct": 5, "target_pct_per_day": 15}), 0)

    def test_missing_fields_fail_open(self):
        self.assertEqual(weekly_throttle({}, 1_800_000_000, {"floor_pct": 5, "target_pct_per_day": 15}), 1)


class TestApplyThrottle(unittest.TestCase):
    def test_disabled_lane_stays_disabled(self):
        self.assertEqual(apply_throttle({"concurrency": 0, "review": 2}, 0.5),
                         {"concurrency": 0, "review": 1})

    def test_minimum_can_exceed_base_and_zero_still_stops(self):
        self.assertEqual(apply_throttle({"concurrency": 2, "min_concurrency": 4}, 0.5)["concurrency"], 4)
        self.assertEqual(apply_throttle({"concurrency": 2, "min_concurrency": 4}, 0)["concurrency"], 0)

    def test_burn_then_weekly_floor_boundary(self):
        # burn.apply scales 4 by 1.5 to 6; weekly factor 0.5 lands exactly at 3.
        burn_spec = importlib.util.spec_from_file_location("burn", ROOT / "lib" / "dispatch" / "burn.py")
        burn = importlib.util.module_from_spec(burn_spec)
        burn_spec.loader.exec_module(burn)
        burned = burn.apply({"concurrency": 4, "review": 2}, 1.5)
        self.assertEqual(apply_throttle(burned, 0.5), {"concurrency": 3, "review": 1})
        self.assertEqual(apply_throttle(burned, 0), {"concurrency": 0, "review": 0})


if __name__ == "__main__":
    unittest.main()
