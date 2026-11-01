"""Filesystem fixtures for the read-only cleanup planner, loaded by path."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "lib" / "resources" / "cleanup.py"
spec = importlib.util.spec_from_file_location("resources_cleanup", SCRIPT)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)
NOW = 1800000000


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.allowed = self.root / "allowed"
        self.allowed.mkdir()
        self.rules = {"allow_roots": [str(self.allowed)]}

    def candidate(self, kind="run_dir", age=4 * 86400, entries=0, path=None):
        path = path or self.allowed / kind
        path.mkdir(parents=True, exist_ok=True)
        os.utime(path, (NOW - age, NOW - age))
        return {"path": str(path), "kind": kind, "mtime": path.stat().st_mtime, "entries": entries}

    def test_each_age_threshold_is_strict(self):
        for kind in ("run_dir", "worktree_node_modules", "tmp_dir"):
            limit = cleanup.DEFAULT_RULES[kind]
            for age, expected in ((limit - 1, 0), (limit, 0), (limit + 1, 1), (-1, 0)):
                with self.subTest(kind=kind, age=age):
                    plan = cleanup.plan_cleanup([self.candidate(kind, age)], [], NOW, self.rules)
                    self.assertEqual(len(plan), expected)
                    if plan:
                        self.assertEqual(set(plan[0]), {"path", "kind", "reason"})

    def test_cache_count_threshold(self):
        for entries, expected in ((19999, 0), (20000, 0), (20001, 1)):
            with self.subTest(entries=entries):
                self.assertEqual(len(cleanup.plan_cleanup(
                    [self.candidate("pkg_cache", age=0, entries=entries)], [], NOW, self.rules)), expected)

    def test_busy_self_ancestor_and_descendant_are_protected(self):
        busy = self.allowed / "worktree"
        for path in (busy, self.allowed, busy / "node_modules"):
            with self.subTest(path=path):
                item = self.candidate(path=path)
                self.assertEqual(cleanup.plan_cleanup([item], [busy], NOW, self.rules), [])
        sibling = self.candidate(path=self.allowed / "worktree-other")
        self.assertEqual(len(cleanup.plan_cleanup([sibling], [busy], NOW, self.rules)), 1)

    def test_allowlist_uses_directory_boundaries_and_denies_missing_roots(self):
        outside = self.candidate(path=self.root / "allowed-other" / "old")
        self.assertEqual(cleanup.plan_cleanup([outside], [], NOW, self.rules), [])
        item = self.candidate()
        self.assertEqual(cleanup.plan_cleanup([item], [], NOW, {}), [])

    def test_symlink_escape_and_busy_alias_are_protected(self):
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.allowed / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        item = self.candidate(path=alias / "old")
        self.assertEqual(cleanup.plan_cleanup([item], [], NOW, self.rules), [])
        busy = self.allowed / "busy"
        item = self.candidate(path=busy / "old")
        busy_alias = self.root / "busy-alias"
        busy_alias.symlink_to(busy, target_is_directory=True)
        self.assertEqual(cleanup.plan_cleanup([item], [busy_alias], NOW, self.rules), [])

    def test_parent_components_are_normalized(self):
        busy = self.allowed / "busy"
        item = self.candidate(path=busy / "old")
        item["path"] = str(self.allowed / "other" / ".." / "busy" / "old")
        self.assertEqual(cleanup.plan_cleanup([item], [busy], NOW, self.rules), [])

    def test_rules_override_defaults_without_mutating_inputs(self):
        items = [self.candidate(kind, age=5, entries=2) for kind in cleanup.DEFAULT_RULES]
        rules = {**self.rules, **{kind: 1 for kind in cleanup.DEFAULT_RULES}}
        before = copy.deepcopy((items, rules))
        self.assertEqual(len(cleanup.plan_cleanup(items, [], NOW, self.rules)), 0)
        self.assertEqual(len(cleanup.plan_cleanup(items, [], NOW, rules)), 4)
        self.assertEqual((items, rules), before)

    def test_scan_counts_one_level_and_skips_files_and_links(self):
        child = self.allowed / "old"
        nested = child / "nested"
        nested.mkdir(parents=True)
        (nested / "not-counted").write_text("content")
        (child / "file").write_text("content")
        (self.allowed / "file").write_text("content")
        (self.allowed / "alias").symlink_to(child, target_is_directory=True)
        os.utime(child, (NOW - 4 * 86400, NOW - 4 * 86400))
        candidates = cleanup.scan([self.allowed], ["run_dir"])
        self.assertEqual(candidates, [{"path": str(child), "kind": "run_dir",
                                       "mtime": child.stat().st_mtime, "entries": 2}])

    def test_node_modules_scan_does_not_propose_whole_worktree_or_source(self):
        (self.allowed / "node_modules").mkdir()
        (self.allowed / "src").mkdir()
        result = cleanup.scan([self.allowed], "worktree_node_modules")
        self.assertEqual([item["path"] for item in result], [str(self.allowed / "node_modules")])

    def test_scan_maps_multiple_roots_and_rejects_unknown_kind(self):
        (self.allowed / "run").mkdir()
        other = self.root / "tmp"
        (other / "old").mkdir(parents=True)
        self.assertEqual({item["kind"] for item in cleanup.scan(
            [self.allowed, other], ["run_dir", "tmp_dir"])}, {"run_dir", "tmp_dir"})
        with self.assertRaises(ValueError):
            cleanup.scan([self.allowed], ["unknown"])

    def test_unreadable_scan_fails_instead_of_returning_partial_plan(self):
        with mock.patch.object(cleanup.os, "scandir", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                cleanup.scan([self.allowed], "run_dir")

    def test_duplicate_candidates_are_reported_once(self):
        item = self.candidate()
        self.assertEqual(len(cleanup.plan_cleanup([item, item], [], NOW, self.rules)), 1)

    def test_invalid_thresholds_fail_closed(self):
        for value in (-1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cleanup.plan_cleanup([self.candidate()], [], NOW, {**self.rules, "run_dir": value})

    def test_cli_is_dry_run_and_has_no_delete_option(self):
        old = self.candidate(age=4 * 86400)
        path = Path(old["path"])
        (path / "keep").write_text("unchanged")
        os.utime(path, (time.time() - 4 * 86400,) * 2)
        before = {str(item.relative_to(self.root)): item.read_bytes()
                  for item in self.root.rglob("*") if item.is_file()}
        command = [sys.executable, str(SCRIPT), "--roots", str(self.allowed), "--busy", ""]
        result = subprocess.run([*command, "--json"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]["path"], str(path))
        table = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(table.returncode, 0)
        self.assertIn("DRY RUN", table.stdout)
        blocked = subprocess.run([*command[:-1], str(path), "--json"], text=True, capture_output=True)
        self.assertEqual(json.loads(blocked.stdout), [])
        invalid = subprocess.run([*command, "--delete"], text=True, capture_output=True)
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(before, {str(item.relative_to(self.root)): item.read_bytes()
                                 for item in self.root.rglob("*") if item.is_file()})
        self.assertTrue(path.is_dir())


if __name__ == "__main__":
    unittest.main()
