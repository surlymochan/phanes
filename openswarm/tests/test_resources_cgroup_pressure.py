"""Tests for cgroup v2 lane memory pressure assessment."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "lib" / "resources" / "cgroup_pressure.py"
spec = importlib.util.spec_from_file_location("cgroup_pressure", MODULE_PATH)
cgroup_pressure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cgroup_pressure)


class CgroupPressureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cgroup-pressure-")
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "system.slice" / "example.service"
        self.path.mkdir(parents=True)

    def write_sample(self, current="100", high="1000", maximum="max", events="high 0\nmax 0\noom 0\noom_kill 0\n", stat="anon 10\nshmem 0\nfile 20\n"):
        for name, value in (("memory.current", current), ("memory.high", high), ("memory.max", maximum), ("memory.events", events), ("memory.stat", stat)):
            (self.path / name).write_text(value, encoding="utf-8")

    def test_normal_sample_is_ok(self):
        self.write_sample()
        sample = cgroup_pressure.read_cgroup(self.path)
        self.assertEqual(cgroup_pressure.assess(sample), {"state": "ok", "reasons": [], "advice": None})

    def test_m10_shmem_pressure_is_throttled(self):
        previous = {"events": {"high": 10, "oom_kill": 0}}
        self.write_sample("694321152", "681574400", events="high 5010\nmax 0\noom 0\noom_kill 0\n", stat="anon 1\nshmem 491966464\nfile 2\n")
        result = cgroup_pressure.assess(cgroup_pressure.read_cgroup(self.path), prev=previous)
        self.assertEqual(result, {"state": "throttled", "reasons": ["memory_high_throttling", "tmpfs_shmem"], "advice": "restart_when_idle"})

    def test_unlimited_high_is_always_ok(self):
        self.write_sample("999999", "max")
        sample = cgroup_pressure.read_cgroup(self.path)
        sample["events"]["high"] = 9000
        self.assertEqual(cgroup_pressure.assess(sample, prev={"events": {"high": 0}})["state"], "ok")

    def test_increased_oom_kill_reports_oom(self):
        self.write_sample(events="oom_kill 2\nhigh 0\n")
        result = cgroup_pressure.assess(cgroup_pressure.read_cgroup(self.path), prev={"events": {"oom_kill": 1, "high": 0}})
        self.assertEqual(result["state"], "oom")
        self.assertEqual(result["advice"], "restart_when_idle")

    def test_missing_files_return_none_fields(self):
        sample = cgroup_pressure.read_cgroup(self.path)
        self.assertIsNone(sample["memory_current"])
        self.assertIsNone(sample["memory_high"])
        self.assertIsNone(sample["events"]["high"])
        self.assertIsNone(sample["stat"]["shmem"])

    def test_first_sample_does_not_call_high_ratio_throttling(self):
        self.write_sample("990", "1000")
        self.assertEqual(cgroup_pressure.assess(cgroup_pressure.read_cgroup(self.path))["state"], "ok")

    def test_cli_json_contains_assessment_and_sample_fields(self):
        self.write_sample()
        completed = subprocess.run([sys.executable, str(MODULE_PATH), "example.service", "--root", self.tmp.name, "--json"], check=True, capture_output=True, text=True)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["state"], "ok")
        self.assertEqual(payload["reasons"], [])
        self.assertIn("advice", payload)
        self.assertEqual(payload["sample"]["memory_current"], 100)
        self.assertIn("memory_high", payload["sample"])


if __name__ == "__main__":
    unittest.main()
