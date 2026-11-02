"""Resource section coverage for `openswarm doctor`."""
import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _load

xs = _load.load()


class FakeStatvfs:
    f_blocks = 100
    f_bavail = 80
    f_frsize = 1024 ** 2
    f_files = 100
    f_favail = 4


class DoctorResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        xs.DATA = str(self.data)
        self.probe, self.policy, self.cgroup = xs.doctor_resource_modules()
        self.policy_obj, _ = self.policy.load_resources_policy({})

    def modules(self, inode_pct=96, disk_pct=20):
        sample = {"inode_used_pct": inode_pct, "disk_used_pct": disk_pct,
                  "inode_free": 100 - inode_pct, "disk_free_gb": 8}
        return mock.patch.object(xs, "doctor_resource_modules", return_value=(
            self.probe, self.policy, self.cgroup)), mock.patch.object(self.probe, "fs_usage", return_value=sample)

    def test_inode_96_percent_fails_cli(self):
        module_patch, usage_patch = self.modules(96)
        with module_patch, usage_patch, mock.patch.object(xs, "doctor_lanes_file", return_value=str(self.data / "none")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                xs.cmd_doctor(argparse.Namespace(lane=None, json=True), self.policy_obj)
        self.assertEqual(raised.exception.code, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_inode_93_percent_warns_and_full_inode_fails_with_disk_ok(self):
        for pct, expected in ((93, "WARN"), (100, "FAIL")):
            module_patch, usage_patch = self.modules(pct, 20)
            with module_patch, usage_patch:
                rows = xs.doctor_resources([], self.policy_obj)
            self.assertEqual(next(row for row in rows if row["kind"] == "inode")["level"], expected)
            self.assertEqual(next(row for row in rows if row["kind"] == "disk")["level"], "OK")

    def test_fs_usage_statvfs_injection_computes_inode_and_disk(self):
        result = self.probe.fs_usage("/", statvfs=lambda _: FakeStatvfs())
        self.assertEqual(result["inode_used_pct"], 96)
        self.assertEqual(result["disk_used_pct"], 20)

    def cgroup_dir(self):
        cgroup_dir = self.data / "cg"
        cgroup_dir.mkdir(exist_ok=True)
        (cgroup_dir / "memory.high").write_text("1000")
        (cgroup_dir / "memory.current").write_text("980")
        (cgroup_dir / "memory.events").write_text("high 0\nmax 0\noom 0\noom_kill 0\n")
        return cgroup_dir

    def test_cgroup_high_event_growth_within_run_warns(self):
        cgroup_dir = self.cgroup_dir()
        lane = {"name": "C1", "systemd_unit": "openswarm-lane-C1.service"}
        grow = lambda _: (cgroup_dir / "memory.events").write_text("high 1000\nmax 0\noom 0\noom_kill 0\n")
        module_patch, usage_patch = self.modules(10, 10)
        with module_patch, usage_patch, mock.patch.object(self.cgroup, "unit_cgroup", return_value=cgroup_dir):
            rows = xs.doctor_resources([lane], self.policy_obj, sleep=grow)
        row = next(row for row in rows if row["kind"] == "cgroup.memory_high")
        self.assertEqual(row["level"], "WARN")
        self.assertIn("memory_high_throttling", row["reasons"])

    def test_quiet_cgroup_is_ok_and_writes_nothing(self):
        cgroup_dir = self.cgroup_dir()
        lane = {"name": "C1", "systemd_unit": "openswarm-lane-C1.service"}
        before = sorted(os.listdir(self.data))
        sleeps = []
        module_patch, usage_patch = self.modules(10, 10)
        with module_patch, usage_patch, mock.patch.object(self.cgroup, "unit_cgroup", return_value=cgroup_dir):
            rows = xs.doctor_resources([lane], self.policy_obj, sleep=sleeps.append)
        self.assertEqual(next(row for row in rows if row["kind"] == "cgroup.memory_high")["level"], "OK")
        self.assertEqual(sleeps, [xs.DOCTOR_CGROUP_WINDOW_S])
        self.assertEqual(sorted(os.listdir(self.data)), before)

    def test_lane_without_systemd_unit_skips_cgroup_and_unreadable_unit_warns(self):
        module_patch, usage_patch = self.modules(10, 10)
        with module_patch, usage_patch:
            rows = xs.doctor_resources([{"name": "C1"}], self.policy_obj, sleep=self.fail_if_called)
            self.assertFalse(any(row["kind"] == "cgroup.memory_high" for row in rows))
            lane = {"name": "C1", "systemd_unit": "missing.service"}
            with mock.patch.object(self.cgroup, "unit_cgroup", return_value=self.data / "absent"):
                rows = xs.doctor_resources([lane], self.policy_obj, sleep=lambda _: None)
        row = next(row for row in rows if row["kind"] == "cgroup.memory_high")
        self.assertEqual(row["level"], "WARN")
        self.assertIn("unavailable", row["detail"])

    def fail_if_called(self, _):
        self.fail("cgroup window must not be sampled for a lane without systemd_unit")

    def test_systemd_unit_comes_from_real_lanes_json(self):
        repo = self.data / "repo"
        repo.mkdir()
        lane = {"name": "C1", "backend": "codex", "model": "gpt-6-luna", "effort": "xhigh",
                "repo": str(repo), "systemd_unit": "openswarm-lane-C1.service"}
        (self.data / "lanes.json").write_text(json.dumps({"lanes": [lane]}))
        lanes = xs.lanes_by_name(None, check_env_mode=False)
        cgroup_dir = self.cgroup_dir()
        units = []

        def unit_path(unit, *args, **kwargs):
            units.append(unit)
            return cgroup_dir

        module_patch, usage_patch = self.modules(10, 10)
        with module_patch, usage_patch, mock.patch.object(self.cgroup, "unit_cgroup", side_effect=unit_path):
            rows = xs.doctor_resources(lanes, self.policy_obj, sleep=lambda _: None)
        self.assertTrue(units)
        self.assertEqual(set(units), {"openswarm-lane-C1.service"})
        self.assertEqual(next(row for row in rows if row["kind"] == "cgroup.memory_high")["level"], "OK")

    def test_text_report_lists_resources_and_fails(self):
        module_patch, usage_patch = self.modules(96)
        with module_patch, usage_patch, mock.patch.object(xs, "doctor_lanes_file", return_value=str(self.data / "none")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                xs.cmd_doctor(argparse.Namespace(lane=None, json=False), self.policy_obj)
        text = output.getvalue()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("resources", text)
        self.assertRegex(text, r"FAIL\s+inode")
        self.assertIn("used=96.0%", text)
        self.assertIn("free=4 inodes", text)
        self.assertNotIn("fail=0", text)

    def test_json_has_resource_schema(self):
        module_patch, usage_patch = self.modules(10, 10)
        with module_patch, usage_patch, mock.patch.object(xs, "doctor_lanes_file", return_value=str(self.data / "none")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                xs.cmd_doctor(argparse.Namespace(lane=None, json=True), self.policy_obj)
        report = json.loads(output.getvalue())
        self.assertIn("resources", report)
        self.assertTrue(all({"path", "kind", "used_pct", "level"} <= set(row)
                            for row in report["resources"]))


if __name__ == "__main__":
    unittest.main()
