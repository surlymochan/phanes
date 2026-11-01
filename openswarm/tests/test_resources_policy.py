"""Tests for resource threshold policy loading and decisions."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "resources" / "policy.py"
spec = importlib.util.spec_from_file_location("resources_policy", MODULE)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class ResourcesPolicyTests(unittest.TestCase):
    def test_missing_resources_uses_defaults(self):
        loaded, warnings = policy.load_resources_policy({"other": 1})
        self.assertEqual(loaded["resources"], policy.DEFAULTS)
        self.assertEqual(warnings, [])

    def test_partial_override_deep_merges_and_does_not_mutate_input(self):
        source = {"resources": {"inode": {"pause_pct": 97}}}
        before = json.loads(json.dumps(source))
        loaded, _ = policy.load_resources_policy(source)
        self.assertEqual(loaded["resources"]["inode"], {"pause_pct": 97, "resume_pct": 92, "cleanup_pct": 90})
        self.assertEqual(source, before)

    def test_unknown_keys_are_preserved_and_warned(self):
        loaded, warnings = policy.load_resources_policy({"resources": {"inode": {"extra": 1}, "other": {}}})
        self.assertEqual(loaded["resources"]["inode"]["extra"], 1)
        self.assertTrue(any("resources.inode.extra" in item for item in warnings))
        self.assertTrue(any("resources.other" in item for item in warnings))

    def test_defaults_validate(self):
        loaded, _ = policy.load_resources_policy({})
        self.assertEqual(policy.validate(loaded), [])

    def test_invalid_percentages_and_hysteresis_order_are_reported(self):
        loaded, _ = policy.load_resources_policy({"resources": {"inode": {
            "pause_pct": 95, "resume_pct": 95, "cleanup_pct": 96},
            "disk": {"pause_pct": 101}}})
        errors = policy.validate(loaded)
        self.assertTrue(any("pause_pct: must be a number" in error for error in errors))
        self.assertTrue(any("resume_pct: must be < pause_pct" in error for error in errors))
        self.assertTrue(any("cleanup_pct: must be <= resume_pct" in error for error in errors))

    def test_types_ratio_and_event_delta_are_checked(self):
        loaded, _ = policy.load_resources_policy({"resources": {"disk": {"pause_pct": "95"},
            "cgroup": {"high_ratio": 0, "shmem_ratio": 1.1, "high_events_delta": 1.5}}})
        errors = policy.validate(loaded)
        self.assertTrue(any("resources.disk.pause_pct" in error for error in errors))
        self.assertTrue(any("high_ratio" in error for error in errors))
        self.assertTrue(any("shmem_ratio" in error for error in errors))
        self.assertTrue(any("high_events_delta" in error for error in errors))

    def test_decision_hysteresis_and_cleanup_band(self):
        loaded, _ = policy.load_resources_policy({})
        self.assertEqual(policy.decide("inode", 96, False, loaded), "pause")
        self.assertEqual(policy.decide("inode", 93, True, loaded), "stay_paused")
        self.assertEqual(policy.decide("inode", 91, True, loaded), "resume")
        self.assertEqual(policy.decide("inode", 91, False, loaded), "cleanup")
        self.assertEqual(policy.decide("inode", 89, False, loaded), "ok")

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "policy.json"
            path.write_text("{}", encoding="utf-8")
            ok = subprocess.run([sys.executable, str(MODULE), str(path), "--json"], capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            path.write_text(json.dumps({"resources": {"disk": {"pause_pct": 101}}}), encoding="utf-8")
            bad = subprocess.run([sys.executable, str(MODULE), str(path)], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 2)


if __name__ == "__main__":
    unittest.main()
