"""Regression coverage for merge-gate union dispatch and standalone imports."""
import importlib.util
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
    def make_remote(self, mixed=False):
        root = pathlib.Path(tempfile.mkdtemp(prefix="merge-gate-union-test-"))
        bare = root / "remote.git"
        work = root / "seed"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
        def git(*args):
            return subprocess.run(["git", *args], cwd=work, check=True, capture_output=True, text=True)
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (work / "docs").mkdir()
        (work / "docs/notes.md").write_text("start\n")
        (work / "code.txt").write_text("start\n")
        git("add", ".")
        git("commit", "-m", "base")
        git("branch", "-M", "main")
        git("remote", "add", "origin", str(bare))
        git("push", "-u", "origin", "main")
        git("checkout", "-b", "feature")
        (work / "docs/notes.md").write_text("start\nfeature\n")
        if mixed:
            (work / "code.txt").write_text("feature code\n")
        git("add", ".")
        git("commit", "-m", "feature")
        git("push", "-u", "origin", "feature")
        git("checkout", "main")
        (work / "docs/notes.md").write_text("start\nbase\n")
        if mixed:
            (work / "code.txt").write_text("base code\n")
        git("add", ".")
        git("commit", "-m", "base update")
        git("push", "origin", "main")
        head = git("rev-parse", "feature").stdout.strip()
        view = {"merge_state": "DIRTY", "head_sha": head, "head_ref": "feature",
                "base_ref": "main", "remote_url": str(bare)}
        return root, bare, view

    def test_default_runner_dispatches_git_and_gh_separately(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(xs.subprocess, "run", side_effect=run), \
                mock.patch.object(xs, "envvar", side_effect=lambda key, default: default):
            self.assertEqual(xs.merge_gate_gh_runner(["git", "status"])[0], 0)
            self.assertEqual(xs.merge_gate_gh_runner(["pr", "list"])[0], 0)
        self.assertEqual(calls[0], ["git", "status"])
        self.assertEqual(calls[1], ["gh", "pr", "list"])

    def test_union_module_loads_from_script_relative_path(self):
        path = pathlib.Path(xs.__file__).resolve().parent.parent / "lib" / "merge" / "union.py"
        spec = importlib.util.spec_from_file_location("union_standalone_test", str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.union_merge))

    def test_union_flag_defaults_off_and_preserves_non_dirty_behavior(self):
        view = {"merge_state": "CLEAN", "mergeable": "MERGEABLE"}
        self.assertIsNone(xs.merge_gate_union(lambda argv: (1, "", ""),
                                              "a/b", 1, view, [], False))

    def test_dirty_conflict_remains_blocked_when_union_disabled(self):
        status, _ = xs.merge_gate_evaluate({"head_sha": "a", "merge_state": "DIRTY",
                                            "checks": {"total": 1}, "labels": []}, {"sha": "a"})
        self.assertEqual(status, "blocked:conflict")

    def test_docs_only_union_is_pushed(self):
        root, bare, view = self.make_remote()
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        result = xs.merge_gate_union(xs.merge_gate_gh_runner, "a/b", 1, view, ["docs/*.md"], False)
        self.assertTrue(result["status"].startswith("blocked:union_merged(pushed "))
        pushed = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "refs/heads/feature"],
                                check=True, capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(pushed, view["head_sha"])

    def test_mixed_conflicts_are_not_pushed(self):
        root, bare, view = self.make_remote(mixed=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        result = xs.merge_gate_union(xs.merge_gate_gh_runner, "a/b", 1, view, ["docs/*.md"], False)
        self.assertEqual(result["status"], "blocked:conflict")
        pushed = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "refs/heads/feature"],
                                check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(pushed, view["head_sha"])

    def test_dry_run_reports_union_without_pushing(self):
        root, bare, view = self.make_remote()
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        result = xs.merge_gate_union(xs.merge_gate_gh_runner, "a/b", 1, view, ["docs/*.md"], True)
        self.assertEqual(result["action"], "dry_run")
        pushed = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "refs/heads/feature"],
                                check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(pushed, view["head_sha"])

    def test_failed_push_lease_is_reported(self):
        root, bare, view = self.make_remote()
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        def runner(argv, **kwargs):
            if argv[:2] == ["git", "-C"] and "push" in argv:
                return 1, "", "stale lease"
            return xs.merge_gate_gh_runner(argv, **kwargs)
        result = xs.merge_gate_union(runner, "a/b", 1, view, ["docs/*.md"], False)
        self.assertEqual(result["status"], "blocked:union_lease")
        pushed = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "refs/heads/feature"],
                                check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(pushed, view["head_sha"])


if __name__ == "__main__":
    unittest.main()
