"""Cross backend fallback mapping for dispatch (Refs #1397, part of #1361).

The module is loaded by file path: ``openswarm/lib/dispatch`` is a namespace package
(no ``__init__.py``), so it cannot be imported as a package from the repository root.
"""
import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "fallback.py"
SPEC = importlib.util.spec_from_file_location("dispatch_fallback", MODULE_PATH)
fallback = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fallback)


class DispatchFallbackTests(unittest.TestCase):
    def test_below_the_threshold_only_the_pinned_backend_is_returned(self):
        cfg = {"wait_s": 600, "map": {"codex": ["pi"]}}

        self.assertEqual(fallback.fallback_backends("codex", 599, cfg, set()), ["codex"])

    def test_expired_wait_appends_the_mapped_backends_in_order(self):
        cfg = {"wait_s": 600, "map": {"agy": ["codex", "pi"]}}

        actual = fallback.fallback_backends("agy", 600, cfg, set())

        self.assertEqual(actual, ["agy", "codex", "pi"])

    def test_paused_backends_are_skipped(self):
        cfg = {"wait_s": 600, "map": {"agy": ["codex", "pi", "gemini"]}}

        actual = fallback.fallback_backends("agy", 900, cfg, {"pi"})

        self.assertEqual(actual, ["agy", "codex", "gemini"])

    def test_strict_never_falls_back(self):
        cfg = {"wait_s": 0, "strict": True, "map": {"codex": ["pi"]}}

        self.assertEqual(
            fallback.fallback_backends("codex", 3600, cfg, set()), ["codex"])

    def test_missing_mapping_returns_only_the_pinned_backend(self):
        cfg = {"wait_s": 1, "map": {"agy": ["pi"]}}

        self.assertEqual(
            fallback.fallback_backends("codex", 1000, cfg, set()), ["codex"])

    def test_missing_mapping_key_returns_only_the_pinned_backend(self):
        self.assertEqual(
            fallback.fallback_backends("codex", 1000, {"wait_s": 1}, set()), ["codex"])

    def test_missing_wait_has_no_grace_period(self):
        cfg = {"map": {"codex": ["pi"]}}

        self.assertEqual(fallback.fallback_backends("codex", 0, cfg, set()), ["codex", "pi"])

    def test_pinned_backend_is_not_repeated_in_its_own_chain(self):
        cfg = {"wait_s": 0, "map": {"codex": ["codex", "pi"]}}

        self.assertEqual(
            fallback.fallback_backends("codex", 10, cfg, set()), ["codex", "pi"])

    def test_a_backend_is_appended_once(self):
        cfg = {"wait_s": 0, "map": {"codex": ["pi", "pi"]}}

        self.assertEqual(fallback.fallback_backends("codex", 10, cfg, set()), ["codex", "pi"])

    def test_a_broken_configuration_keeps_the_pinned_backend(self):
        broken = (
            None,
            {},
            {"wait_s": 600},
            {"wait_s": 600, "map": None},
            {"wait_s": 600, "map": {"codex": "pi"}},
            {"wait_s": 600, "map": {"codex": [None, ""]}},
        )
        for cfg in broken:
            with self.subTest(cfg=cfg):
                actual = fallback.fallback_backends("codex", 1000, cfg, set())
                self.assertEqual(actual, ["codex"])


if __name__ == "__main__":
    unittest.main()
