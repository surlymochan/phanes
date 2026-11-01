"""Offline filesystem resource contracts and subprocess parity for the host probe."""

from collections import namedtuple
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "lib" / "resources" / "probe.py"
spec = importlib.util.spec_from_file_location("resources_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
VFS = namedtuple("VFS", "f_frsize f_blocks f_bavail f_bfree f_files f_favail")
FIELDS = {"path", "disk_free_gb", "disk_used_pct", "inode_total", "inode_free", "inode_used_pct"}


class UsageTests(unittest.TestCase):
    def sample(self, **changes):
        values = {"f_frsize": 4096, "f_blocks": 1000, "f_bavail": 800,
                  "f_bfree": 900, "f_files": 1000, "f_favail": 700}
        values.update(changes)
        return VFS(**values)

    def test_normal_usage_and_injected_statvfs_path(self):
        seen = []
        def fake_statvfs(path):
            seen.append(path)
            return self.sample()
        result = probe.fs_usage("/fixture", statvfs=fake_statvfs)
        self.assertEqual(seen, ["/fixture"])
        self.assertEqual(set(result), FIELDS)
        self.assertEqual(result["path"], "/fixture")
        self.assertAlmostEqual(result["disk_free_gb"], 800 * 4096 / 1024 ** 3)
        self.assertAlmostEqual(result["disk_used_pct"], 20)
        self.assertEqual(result["inode_total"], 1000)
        self.assertEqual(result["inode_free"], 700)
        self.assertAlmostEqual(result["inode_used_pct"], 30)

    def test_inode_exhaustion_classifies_hard_despite_free_disk(self):
        result = probe.fs_usage("/fixture", statvfs=lambda _: self.sample(f_favail=40))
        self.assertAlmostEqual(result["inode_used_pct"], 96)
        self.assertEqual(probe.classify(result), "hard")

    def test_block_exhaustion_classifies_hard_despite_free_inodes(self):
        result = probe.fs_usage("/fixture", statvfs=lambda _: self.sample(f_bavail=40))
        self.assertAlmostEqual(result["disk_used_pct"], 96)
        self.assertEqual(probe.classify(result), "hard")

    def test_unreported_inode_capacity_has_three_null_fields(self):
        result = probe.fs_usage("/fixture", statvfs=lambda _: self.sample(f_files=0, f_favail=0))
        for key in ("inode_total", "inode_free", "inode_used_pct"):
            self.assertIsNone(result[key])
        self.assertEqual(probe.classify(result), "ok")

    def test_zero_block_capacity_does_not_divide_by_zero(self):
        result = probe.fs_usage("/fixture", statvfs=lambda _: self.sample(f_blocks=0, f_bavail=0))
        self.assertIsNone(result["disk_used_pct"])
        self.assertEqual(result["disk_free_gb"], 0)
        self.assertEqual(probe.classify(result), "ok")

    def test_statvfs_errors_are_not_reported_as_healthy_measurements(self):
        def unavailable(_):
            raise OSError("fixture unavailable")
        with self.assertRaisesRegex(OSError, "fixture unavailable"):
            probe.fs_usage("/fixture", statvfs=unavailable)


class ClassificationTests(unittest.TestCase):
    def test_soft_and_hard_thresholds_are_inclusive_for_either_resource(self):
        for field in ("disk_used_pct", "inode_used_pct"):
            for percent, expected in ((89.99, "ok"), (90, "soft"), (94.99, "soft"), (95, "hard"), (100, "hard")):
                with self.subTest(field=field, percent=percent):
                    usage = {"disk_used_pct": 10, "inode_used_pct": 10, field: percent}
                    self.assertEqual(probe.classify(usage), expected)

    def test_worse_resource_wins_independent_of_order(self):
        for disk, inode in ((90, 96), (96, 90), (5, 96), (96, 5)):
            with self.subTest(disk=disk, inode=inode):
                self.assertEqual(probe.classify({"disk_used_pct": disk, "inode_used_pct": inode}), "hard")

    def test_custom_thresholds_and_unknown_inode(self):
        usage = {"disk_used_pct": 70, "inode_used_pct": None}
        self.assertEqual(probe.classify(usage, pause_pct=80, soft_pct=70), "soft")
        usage["disk_used_pct"] = 80
        self.assertEqual(probe.classify(usage, pause_pct=80, soft_pct=70), "hard")
        self.assertEqual(probe.classify({"disk_used_pct": None, "inode_used_pct": None}), "ok")

    def test_unknown_resource_does_not_mask_pressure_in_the_other(self):
        for unknown, known in (("inode_used_pct", "disk_used_pct"),
                               ("disk_used_pct", "inode_used_pct")):
            for percent, expected in ((90, "soft"), (96, "hard")):
                with self.subTest(unknown=unknown, percent=percent):
                    self.assertEqual(probe.classify({unknown: None, known: percent}), expected)
                    self.assertEqual(probe.classify({known: percent}), expected)


class SubprocessTests(unittest.TestCase):
    def assert_usage_rows(self, rows, paths):
        self.assertIsInstance(rows, list)
        self.assertEqual([row["path"] for row in rows], [str(path) for path in paths])
        for row in rows:
            self.assertEqual(set(row), FIELDS)
            self.assertGreaterEqual(row["disk_free_gb"], 0)
            self.assertTrue(row["inode_used_pct"] is None or 0 <= row["inode_used_pct"] <= 100)

    def test_remote_snippet_is_one_line_and_runs_without_repository_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory), Path(directory) / "second"]
            paths[1].mkdir()
            code = probe.remote_snippet([str(path) for path in paths])
            self.assertIsInstance(code, str)
            self.assertNotIn("\n", code)
            result = subprocess.run([sys.executable, "-I", "-c", code], cwd=directory,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_usage_rows(json.loads(result.stdout), paths)

    def test_remote_path_quoting_does_not_execute_path_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "quote' and \"double\"; $(touch SENTINEL)\nnext"
            path.mkdir()
            code = probe.remote_snippet([str(path)])
            self.assertNotIn("\n", code)
            result = subprocess.run([sys.executable, "-I", "-c", code], cwd=root,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_usage_rows(json.loads(result.stdout), [path])
            self.assertEqual(list(root.iterdir()), [path])

    def test_cli_json_for_multiple_paths_and_default_root(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [directory, os.path.join(directory, "child")]
            Path(paths[1]).mkdir()
            result = subprocess.run([sys.executable, str(SCRIPT), *paths, "--json"],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_usage_rows(json.loads(result.stdout), paths)
        result = subprocess.run([sys.executable, str(SCRIPT), "--json"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_usage_rows(json.loads(result.stdout), ["/"])


if __name__ == "__main__":
    unittest.main()
