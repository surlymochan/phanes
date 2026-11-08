#!/usr/bin/env python3
"""The artifact filter of the harvest (Refs #1835): the artifacts of the lane (the hidden
build leftovers, the review products) never enter a PR."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
ad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ad)

ARTIFACTS = {"swrm/tests/.dde4xxx-00000000.so": "binary\n",
             "swrm/lib/native-extension.so": "binary\n",
             ".lane-review/PR-1.md": "VERDICT: APPROVE\n"}


class HarvestFilterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.repo / "base").write_text("base")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.patch = mock.patch.object(ad, "LOCAL", str(self.repo))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args]).decode().strip()

    def commit(self, paths):
        for path in paths:
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path)
        self.git("add", "-A")
        self.git("commit", "-qm", "result")
        self.git("update-ref", "refs/fleet/lane", self.git("rev-parse", "HEAD"))

    def files(self, ref):
        return self.git("ls-tree", "-r", "--name-only", ref).splitlines()

    def test_mixed(self):
        self.commit(list(ARTIFACTS) + ["good"])
        head, removed, empty = ad.filter_harvest("HEAD", self.base)
        self.assertEqual(len(removed), 3)
        self.assertFalse(empty)
        self.assertEqual(self.files(head), ["base", "good"])

    def test_clean(self):
        self.commit(["good"])
        self.assertEqual(ad.filter_harvest("HEAD", self.base), ("HEAD", [], False))

    def test_only_artifacts(self):
        self.commit(list(ARTIFACTS))
        head, removed, empty = ad.filter_harvest("HEAD", self.base)
        self.assertEqual(len(removed), 3)
        self.assertTrue(empty)
        self.assertEqual(self.files(head), self.files(self.base))

    def test_commits_that_cancel_each_other(self):
        # the normal change is added and deleted again: the tree is the tree of the base
        self.commit(list(ARTIFACTS) + ["good"])
        (self.repo / "good").unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "revert")
        self.git("update-ref", "refs/fleet/lane", self.git("rev-parse", "HEAD"))
        head, removed, empty = ad.filter_harvest("HEAD", self.base)
        self.assertTrue(empty)
        self.assertNotEqual(head, self.base)
        self.assertEqual(self.files(head), self.files(self.base))

    def test_finish_pushes_the_filtered_branch(self):
        self.commit(list(ARTIFACTS) + ["good"])
        st = {"issue": 1, "run": "run", "base": self.base}
        info = {"branch": "branch", "commit": "commit", "repo": "repo"}
        with mock.patch.object(ad, "harvest_fetch", return_value=(True, "", 0)), \
             mock.patch.object(ad, "log") as log, mock.patch.object(ad, "gh", return_value=(0, "", "")), \
             mock.patch.object(ad, "pr_validation_mode", return_value="warn"), \
             mock.patch.object(ad, "sh", return_value=(0, "", "")) as sh:
            result = ad.finish("lane", {"host": "host"}, st, info, {})
        self.assertEqual(result["status"], "pr-created")
        push = [call for call in sh.call_args_list if "push" in call.args[0]]
        self.assertEqual(len(push), 1)
        self.assertNotIn("refs/fleet/lane:", push[0].args[0][-1])
        self.assertIn("harvest excluded", log.call_args_list[0].args[0])

    def test_finish_with_only_artifacts_pushes_nothing(self):
        self.commit(list(ARTIFACTS))
        st = {"issue": 1, "run": "run", "base": self.base}
        info = {"branch": "branch", "commit": "commit", "repo": "repo"}
        state = {}
        with mock.patch.object(ad, "harvest_fetch", return_value=(True, "", 0)), \
             mock.patch.object(ad, "log"), mock.patch.object(ad, "gh", return_value=(0, "", "")), \
             mock.patch.object(ad, "sh", return_value=(0, "", "")) as sh:
            result = ad.finish("lane", {"host": "host"}, st, info, state)
        self.assertEqual(result["status"], "lane-failed")
        sh.assert_not_called()
        self.assertIn(1, state.get("_done", []))

    def test_finish_filter_failure_does_not_push(self):
        st = {"issue": 1, "run": "run", "base": self.base}
        info = {"branch": "branch", "commit": "commit", "repo": "repo"}
        with mock.patch.object(ad, "harvest_fetch", return_value=(True, "", 0)), \
             mock.patch.object(ad, "filter_harvest", side_effect=RuntimeError("git failed")), \
             mock.patch.object(ad, "log") as log, mock.patch.object(ad, "gh"), \
             mock.patch.object(ad, "sh") as sh:
            result = ad.finish("lane", {"host": "host"}, st, info, {})
        self.assertEqual(result["status"], "filter-failed")
        sh.assert_not_called()
        self.assertIn("git failed", log.call_args.args[0])

    def test_git_error(self):
        with self.assertRaises(RuntimeError):
            ad.filter_harvest("HEAD", "missing")


if __name__ == "__main__":
    unittest.main()
