import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import _load


xs = _load.load()


class LaneRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lane-recovery-")
        self.root = pathlib.Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Lane Test")
        self.git("config", "user.email", "lane-test@example.invalid")
        (self.repo / "base.txt").write_text("base\n")
        self.git("add", "base.txt")
        self.git("commit", "-q", "-m", "base")
        self.lane = {"name": "L1", "backend": "pi", "model": "test", "repo": str(self.repo)}
        self.data = self.root / "data"
        self.runs = self.data / "runs"
        self.wt = self.data / "lanes" / "L1" / "wt"
        self.run_id = "run-test"

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def setup_run(self):
        rdir = self.runs / self.run_id
        rdir.mkdir(parents=True)
        (rdir / "status.json").write_text(json.dumps({"state": "running", "lane": "L1"}))
        (rdir / "assignment.json").write_text(json.dumps({"run_id": self.run_id, "task": "continue",
                                                           "workspace_path": str(self.wt),
                                                           "base": self.git("rev-parse", "HEAD")}))
        self.wt.parent.mkdir(parents=True)
        self.git("worktree", "add", "--detach", "-q", str(self.wt), "HEAD")
        self.git("checkout", "-q", "-b", f"swrm/{self.run_id}", cwd=self.wt)
        (self.data / "lanes" / "L1" / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.data / "lanes" / "L1" / "state.json").write_text(json.dumps({
            "state": "busy", "current_run": self.run_id}))

    def recover(self):
        with mock.patch.object(xs, "DATA", str(self.data)), \
                mock.patch.object(xs, "RUNS", str(self.runs)), \
                mock.patch.object(xs, "lane_config", return_value=[self.lane]):
            return xs.lane_recover_assignment(self.lane, {"state": "busy", "current_run": self.run_id})

    def test_unpushed_worktree_changes_recover_without_cleaning(self):
        self.setup_run()
        (self.wt / "local-commit.txt").write_text("local only\n")
        self.git("add", "local-commit.txt", cwd=self.wt)
        self.git("commit", "-q", "-m", "local unpushed commit", cwd=self.wt)
        (self.wt / "partial.txt").write_text("unfinished work\n")
        spec, error = self.recover()
        self.assertIsNone(error)
        self.assertEqual(spec["_recovery_level"], "workspace")
        with mock.patch.object(xs, "DATA", str(self.data)):
            ws = xs.lane_prepare_workspace(self.lane, self.run_id, self.git("rev-parse", "HEAD"), preserve=True)
        self.assertEqual((pathlib.Path(ws["root"]) / "partial.txt").read_text(), "unfinished work\n")

    def test_pushed_commit_recovers_at_remote_tip(self):
        self.setup_run()
        (self.wt / "saved.txt").write_text("saved\n")
        self.git("add", "saved.txt", cwd=self.wt)
        self.git("commit", "-q", "-m", "saved progress", cwd=self.wt)
        bare = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        self.git("remote", "add", "origin", str(bare), cwd=self.wt)
        self.git("push", "-q", "origin", f"swrm/{self.run_id}", cwd=self.wt)
        self.git("fetch", "-q", "origin", cwd=self.wt)
        spec, error = self.recover()
        self.assertIsNone(error)
        self.assertEqual(spec["_recovery_level"], "commit")
        self.assertEqual(spec["_recovery_point"], self.git("rev-parse", "HEAD", cwd=self.wt))

    def test_remote_branch_at_base_is_not_commit_recovery(self):
        self.setup_run()
        bare = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        self.git("remote", "add", "origin", str(bare), cwd=self.wt)
        self.git("push", "-q", "origin", f"swrm/{self.run_id}", cwd=self.wt)
        self.git("fetch", "-q", "origin", cwd=self.wt)
        spec, error = self.recover()
        self.assertIsNone(error)
        self.assertEqual(spec["_recovery_level"], "workspace")

    def test_mismatched_run_status_is_abandoned(self):
        self.setup_run()
        status_path = self.runs / self.run_id / "status.json"
        status_path.write_text(json.dumps({"state": "finished", "lane": "L1"}))
        spec, error = self.recover()
        self.assertIsNone(spec)
        self.assertIn("not queued/running", error)


if __name__ == "__main__":
    unittest.main()
