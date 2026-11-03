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
state_windows = burn_mod.state_windows
factors = burn_mod.factors

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


class TestStateWindows(unittest.TestCase):
    """The windows of every backend, not of one backend only."""

    def state(self):
        return {"quota_window": {
            "pi": {"resets_at": RESET, "source": "minimax-coding-plan"},
            "codex": {"resets_at": RESET + 7200, "source": "codex-rate-limit"},
            "ocgo": {"resets_at": RESET + 86400, "source": "opencode-go-cycle"},
        }}

    def test_every_backend_is_read(self):
        windows = state_windows(self.state())
        self.assertEqual(sorted(windows), ["codex", "ocgo", "pi"])
        self.assertEqual(windows["pi"]["resets_at"], RESET)
        self.assertEqual(windows["pi"]["source"], "minimax-coding-plan")
        self.assertEqual(windows["ocgo"]["resets_at"], RESET + 86400)

    def test_entries_without_a_reset_time_are_skipped(self):
        state = self.state()
        state["quota_window"]["agy"] = {"source": "unknown"}
        state["quota_window"]["broken"] = "soon"
        windows = state_windows(state)
        self.assertNotIn("agy", windows)
        self.assertNotIn("broken", windows)

    def test_codex_windows_are_read_from_the_legacy_state_shape(self):
        state = {"codex": {"limits": {"codex": {"windows": {
            "five_hour": {"resets_at": RESET + 600},
            "seven_day": {"resets_at": RESET + 400},
        }}}}}
        windows = state_windows(state)
        self.assertEqual(windows["codex"]["resets_at"], RESET + 400)  # the nearest reset

    def test_a_state_without_windows_yields_nothing(self):
        self.assertEqual(state_windows(None), {})
        self.assertEqual(state_windows({}), {})
        self.assertEqual(state_windows({"codex": {"limits": {}}}), {})


class TestFactors(unittest.TestCase):
    def test_only_the_backends_inside_their_window_burn(self):
        windows = {"pi": {"resets_at": RESET}, "codex": {"resets_at": RESET + 10 * 3600}}
        cfg = {"pi": {"burn_before_reset_h": 1.5, "factor": 2},
               "codex": {"burn_before_reset_h": 1.5, "factor": 3}}
        self.assertEqual(factors(windows, cfg, RESET - 3600), {"pi": 2})
        self.assertEqual(factors(windows, cfg, RESET - 2 * 3600), {})

    def test_a_backend_without_a_policy_entry_never_burns(self):
        windows = {"pi": {"resets_at": RESET}}
        self.assertEqual(factors(windows, {"codex": {"burn_before_reset_h": 1, "factor": 2}}, RESET - 60), {})

    def test_a_backend_without_a_window_never_burns(self):
        cfg = {"pi": {"burn_before_reset_h": 1.5, "factor": 2}}
        self.assertEqual(factors({}, cfg, RESET - 60), {})
        self.assertEqual(factors(None, cfg, RESET - 60), {})
        self.assertEqual(factors({"pi": {"resets_at": RESET}}, None, RESET - 60), {})


if __name__ == "__main__":
    unittest.main()
