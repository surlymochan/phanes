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

    def test_cgroup_high_event_growth_warns_across_runs(self):
        lane = {"name": "C1", "service_manager": "systemd-unit"}
        cgroup_dir = self.data / "cg"
        cgroup_dir.mkdir()
        (cgroup_dir / "memory.high").write_text("1000")
        (cgroup_dir / "memory.current").write_text("980")
        (cgroup_dir / "memory.events").write_text("high 0\nmax 0\noom 0\noom_kill 0\n")
        module_patch, usage_patch = self.modules(10, 10)
        with module_patch, usage_patch, mock.patch.object(self.cgroup, "unit_cgroup", return_value=cgroup_dir):
            xs.doctor_resources([lane], self.policy_obj)
            (cgroup_dir / "memory.events").write_text("high 1000\nmax 0\noom 0\noom_kill 0\n")
            rows = xs.doctor_resources([lane], self.policy_obj)
        self.assertEqual(next(row for row in rows if row["kind"] == "cgroup.memory_high")["level"], "WARN")

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
