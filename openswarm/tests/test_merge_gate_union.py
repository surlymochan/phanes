"""Union conflict handling in the merge gate, using a local bare remote."""
import argparse
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _load
xs = _load.load()


class MergeGateUnionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="merge-gate-union-")
        self.addCleanup(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.data_patch = mock.patch.object(xs, "DATA", str(self.home))
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(self.remote)], check=True,
                       capture_output=True)
        seed = root / "seed"
        subprocess.run(["git", "clone", str(self.remote), str(seed)], check=True, capture_output=True)
        self.git(seed, "config", "user.name", "Test")
        self.git(seed, "config", "user.email", "test@example.invalid")
        (seed / "docs").mkdir()
        (seed / "docs/ctl-boot.md").write_text("start\n")
        (seed / "code.txt").write_text("start\n")
        self.git(seed, "add", ".")
        self.git(seed, "commit", "-m", "base")
        self.git(seed, "push", "origin", "main")
        self.base = self.git(seed, "rev-parse", "HEAD")
        self.git(seed, "checkout", "-b", "feature")
        (seed / "docs/ctl-boot.md").write_text("start\nfeature\n")
        (seed / "code.txt").write_text("start\nfeature\n")
        self.git(seed, "commit", "-am", "feature")
        self.git(seed, "push", "origin", "feature")
        self.head = self.git(seed, "rev-parse", "HEAD")
        self.git(seed, "checkout", "main")
        (seed / "docs/ctl-boot.md").write_text("start\nbase\n")
        (seed / "code.txt").write_text("start\nbase\n")
        self.git(seed, "commit", "-am", "base changes")
        self.git(seed, "push", "origin", "main")

    @staticmethod
    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout.strip()

    def run_gate(self, union_files="docs/*.md", dry_run=False, lease_failure=False):
        view = {"number": 4, "headRefOid": self.head, "headRefName": "feature", "baseRefName": "main",
                "labels": [], "mergeable": "CONFLICTING",
                "statusCheckRollup": [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED",
                                       "conclusion": "SUCCESS"}]}
        calls = []
        def runner(argv):
            calls.append(list(argv))
            if argv[:2] == ["pr", "view"]:
                return 0, json.dumps(view), ""
            command = argv[1:]
            if "clone" in command:
                command = [str(self.remote) if item == "https://github.com/acme/project.git" else item
                           for item in command]
            command = ["feature" if item == "refs/pull/4/head" else item for item in command]
            if "push" in command and lease_failure:
                return 1, "", "stale lease"
            result = subprocess.run(["git", *command], capture_output=True, text=True)
            return result.returncode, result.stdout, result.stderr
        args = argparse.Namespace(repo="acme/project", pr=4, dry_run=dry_run, json=True,
                                  union_files=union_files)
        with mock.patch.object(xs, "merge_gate_reviews_path", return_value=str(self.home / "reviews.json")):
            (self.home / "reviews.json").write_text(json.dumps({"acme/project#4": {"sha": self.head}}))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = xs.cmd_merge_gate(args, None, runner=runner)
        return code, json.loads(buffer.getvalue())["results"][0], calls

    def pushed_head(self):
        return subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "refs/heads/feature"],
                              check=True, capture_output=True, text=True).stdout.strip()

    def test_union_only_conflict_is_pushed(self):
        # Code conflicts too in fixture; retain it in sync so this PR only conflicts in docs.
        self._make_code_identical()
        _, row, _ = self.run_gate()
        self.assertTrue(row["status"].startswith("blocked:union_merged(pushed "))
        self.assertNotEqual(self.pushed_head(), self.head)

    def _make_code_identical(self):
        # Reset the base side code edit, preserving a docs-only conflict.
        seed = pathlib.Path(self.temp.name) / "seed"
        self.git(seed, "checkout", "main")
        (seed / "code.txt").write_text("start\n")
        self.git(seed, "commit", "-am", "revert code change")
        self.git(seed, "push", "origin", "main")
        self.base = self.git(seed, "rev-parse", "HEAD")

    def test_mixed_conflict_does_not_push(self):
        _, row, _ = self.run_gate()
        self.assertEqual(row["status"], "blocked:conflict")
        self.assertEqual(self.pushed_head(), self.head)

    def test_dry_run_does_not_push(self):
        self._make_code_identical()
        _, row, _ = self.run_gate(dry_run=True)
        self.assertEqual(row["action"], "dry_run")
        self.assertEqual(self.pushed_head(), self.head)

    def test_lease_failure_is_reported(self):
        self._make_code_identical()
        _, row, _ = self.run_gate(lease_failure=True)
        self.assertEqual(row["status"], "blocked:union_lease")
        self.assertEqual(self.pushed_head(), self.head)

    def test_union_disabled_keeps_conflict_behavior(self):
        _, row, calls = self.run_gate(union_files="")
        self.assertEqual(row["status"], "blocked:conflict")
        self.assertFalse(any(call[:1] == ["git"] for call in calls))


if __name__ == "__main__":
    unittest.main()
