"""The artifact filter of the harvest (lib/dispatch/harvest_filter.py): the artifacts of the
lane never enter a PR (Refs #1835)."""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("harvest_filter_under_test",
                                               os.path.join(os.path.dirname(HERE), "lib", "dispatch", "harvest_filter.py"))
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

ARTIFACTS = {"openswarm/tests/.dde4xxx-00000000.so": "binary\n",
             ".lane-review/PR-1.md": "VERDICT: APPROVE\n"}


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="harvest-filter-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.write("base.txt", "base\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *args):
        proc = subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True)
        if proc.returncode:
            raise AssertionError("git %s failed: %s" % (args[0], proc.stderr.strip()))
        return proc.stdout.strip()

    def write(self, path, content):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as stream:
            stream.write(content)

    def commit(self, files, message="result"):
        for path, content in files.items():
            full = os.path.join(self.repo, path)
            if content is None:
                if os.path.exists(full):
                    os.unlink(full)
                continue
            self.write(path, content)
        self.git("add", "-A", "--", *files.keys())
        self.git("commit", "-qm", message)

    def files(self, ref):
        return self.git("ls-tree", "-r", "--name-only", ref).splitlines()

    def test_the_rules_match_the_artifacts_and_nothing_else(self):
        matched = H.excluded_paths([
            "openswarm/tests/.dde4xxx-00000000.so", ".lane-review/PR-1.md",
            "docs/.lane-review/PR-2.md", "src/lib.so", "src/main.py", "README.md"])
        self.assertEqual(matched, [".lane-review/PR-1.md", "docs/.lane-review/PR-2.md",
                                   "openswarm/tests/.dde4xxx-00000000.so"])

    def test_the_artifacts_are_dropped_and_the_rest_is_kept(self):
        self.commit(dict(ARTIFACTS, **{"code.py": "print('lane')\n"}))
        head, removed, empty = H.filter_branch(self.repo, "HEAD", self.base)
        self.assertEqual(removed, [".lane-review/PR-1.md", "openswarm/tests/.dde4xxx-00000000.so"])
        self.assertFalse(empty)
        self.assertEqual(self.files(head), ["base.txt", "code.py"])

    def test_a_branch_with_only_artifacts_is_empty(self):
        self.commit(dict(ARTIFACTS))
        head, removed, empty = H.filter_branch(self.repo, "HEAD", self.base)
        self.assertEqual(len(removed), 2)
        self.assertTrue(empty)
        self.assertEqual(self.files(head), self.files(self.base))

    def test_commits_that_cancel_each_other_leave_an_empty_result(self):
        self.commit(dict(ARTIFACTS, **{"code.py": "print('lane')\n"}))
        self.commit({"code.py": None})
        head, removed, empty = H.filter_branch(self.repo, "HEAD", self.base)
        self.assertTrue(empty)
        self.assertNotEqual(head, self.base)      # the head differs, the tree does not
        self.assertEqual(self.files(head), self.files(self.base))

    def test_a_clean_branch_is_not_touched(self):
        self.commit({"code.py": "print('lane')\n"})
        head, removed, empty = H.filter_branch(self.repo, "HEAD", self.base)
        self.assertEqual(head, "HEAD")
        self.assertEqual(removed, [])
        self.assertFalse(empty)

    def test_a_failing_git_command_raises(self):
        with self.assertRaises(H.FilterError):
            H.filter_branch(self.repo, "HEAD", "0" * 40)
        with self.assertRaises(H.FilterError):
            H.filter_branch(self.repo, "HEAD", "")

    def test_a_merge_commit_cannot_be_filtered(self):
        self.git("checkout", "-q", "-b", "side")
        self.write("side.txt", "side\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "side")
        self.git("checkout", "-q", "-")
        self.write("main.txt", "main\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "side")
        with self.assertRaises(H.FilterError):
            H.filter_branch(self.repo, "HEAD", self.base)

    def test_the_configured_patterns_replace_the_default_rules(self):
        self.commit({"build.log": "noise\n"})
        head, removed, empty = H.filter_branch(self.repo, "HEAD", self.base, [r"(^|/)build\.log$"])
        self.assertEqual(removed, ["build.log"])
        self.assertNotIn("build.log", self.files(head))
        # the default rules do not touch it
        head, removed, empty = H.filter_branch(self.repo, "HEAD", self.base)
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
