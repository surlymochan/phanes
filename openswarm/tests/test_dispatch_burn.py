"""Tests for lib/dispatch/burn.py module."""

import importlib.util
from pathlib import Path
import unittest

BURN_PATH = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "burn.py"
spec = importlib.util.spec_from_file_location("burn", BURN_PATH)
burn_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(burn_mod)

burn_factor = burn_mod.burn_factor
apply = burn_mod.apply

CFG = {"pi": {"burn_before_reset_h": 1.5, "factor": 2}}
RESET = 1_800_000_000
WINDOW = {"resets_at": RESET}


class TestBurnFactor(unittest.TestCase):
    def test_inside_and_outside_window(self):
        self.assertEqual(burn_factor("pi", RESET - 3600, WINDOW, CFG), 2)
        self.assertEqual(burn_factor("pi", RESET - 2 * 3600, WINDOW, CFG), 1)

    def test_window_edges_and_reset_boundary(self):
        self.assertEqual(burn_factor("pi", RESET - 1.5 * 3600, WINDOW, CFG), 2)
        self.assertEqual(burn_factor("pi", RESET, WINDOW, CFG), 2)
        self.assertEqual(burn_factor("pi", RESET + 1, WINDOW, CFG), 1)
        next_window = {"resets_at": RESET + 5 * 3600}
        self.assertEqual(burn_factor("pi", RESET + 60, next_window, CFG), 1)

    def test_missing_or_invalid_config_and_window(self):
        for cfg in (None, {}, {"other": CFG["pi"]}, {"pi": {"factor": 2}}):
            self.assertEqual(burn_factor("pi", RESET - 60, WINDOW, cfg), 1)
        for window in (None, {}, {"resets_at": "soon"}):
            self.assertEqual(burn_factor("pi", RESET - 60, window, CFG), 1)


class TestApply(unittest.TestCase):
    def test_scales_and_rounds_limits(self):
        out = apply({"concurrency": 3, "review": 2, "name": "x"}, 2)
        self.assertEqual(out, {"concurrency": 6, "review": 4, "name": "x"})
        self.assertEqual(apply({"concurrency": 3}, 1.5)["concurrency"], 4)
        self.assertEqual(apply({"review": 5}, 1.5)["review"], 8)

    def test_caps_truncate_scaled_values(self):
        limits = {"concurrency": 8, "review": 4, "max_concurrency": 12, "max_review": 5}
        out = apply(limits, 2)
        self.assertEqual(out["concurrency"], 12)
        self.assertEqual(out["review"], 5)

    def test_noop_preserves_input_and_unrelated_keys(self):
        limits = {"concurrency": 8, "review": 4, "other": 1}
        self.assertEqual(apply(limits, 1), limits)
        self.assertEqual(apply(limits, 0.5), limits)
        self.assertEqual(apply(limits, "2"), limits)
        self.assertEqual(limits, {"concurrency": 8, "review": 4, "other": 1})
        self.assertEqual(apply({"other": 1}, 2), {"other": 1})


if __name__ == "__main__":
    unittest.main()
