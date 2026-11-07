"""Lane worktree git admin dir and Codex sandbox writability (Refs #1832).

A Codex ``workspace-write`` sandbox mounts a worktree's git metadata read-only. A lane can only
commit inside the sandbox when the sandbox's writable roots contain that lane's *exact* git admin
dir (``.git/worktrees/<name>``). Two lanes that share a worktree directory basename got different
admin dirs (``wt`` and ``wt1``), so a root pinned to one name covered one lane only and the other
lane failed every git write with ``Read-only file system``.

The tests here cover the runtime fix:

* the roots follow the worktree's real gitdir instead of a name derived from the directory,
* every lane owns its admin dir, so two lanes on one host never share one,
* the codex app-server is started with those roots.

The regression that needs a real Codex CLI lives in ``test_codex_sandbox_commit.py``.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load

osw = _load.load()

POLICY = {"codex": {"sandbox": "workspace-write", "worktree_gitdir_writable": True}}


def git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


class LaneWorktreeGitdirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="osgitdir-", dir="/tmp")
        self.root = os.path.realpath(self.tmp.name)
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Lane Test")
        git(self.repo, "config", "user.email", "lane-test@example.invalid")
        with open(os.path.join(self.repo, "README.md"), "w") as handle:
            handle.write("base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def add_worktree(self, path):
        git(self.repo, "worktree", "add", "-q", "--detach", path, "HEAD")
        return path

    def admin_dir(self, worktree):
        return os.path.realpath(git(worktree, "rev-parse", "--absolute-git-dir"))

    def test_two_lanes_with_the_same_worktree_basename_get_their_own_gitdir(self):
        # The fleet layout gives every lane a worktree directory named ``wt``; git numbers the
        # admin dirs of the second lane (``wt1``). The roots must follow the real gitdir.
        first = self.add_worktree(os.path.join(self.root, "lanes", "CX5", "wt"))
        second = self.add_worktree(os.path.join(self.root, "lanes", "CX10", "wt"))
        self.assertEqual(os.path.basename(self.admin_dir(first)), "wt")
        self.assertEqual(os.path.basename(self.admin_dir(second)), "wt1")
        second_roots = osw.codex_gitdir_writable_roots(second, POLICY)
        self.assertIn(self.admin_dir(second), second_roots)
        self.assertNotIn(self.admin_dir(first), second_roots)

    def test_lane_admin_dir_is_unique_per_lane(self):
        lanes = []
        for name in ("CX5", "CX10"):
            worktree = os.path.join(self.root, "lanes", name, "wt")
            os.makedirs(worktree)
            lanes.append({"name": name, "repo": self.repo, "worktree": worktree})
        admins = []
        for lane in lanes:
            self.add_worktree(lane["worktree"])
            admins.append(osw.lane_align_worktree_admin_dir(self.repo, lane["worktree"], lane["name"]))
        self.assertEqual([os.path.basename(path) for path in admins], ["wt-CX5", "wt-CX10"])
        self.assertEqual(len(set(admins)), 2)
        for lane, admin in zip(lanes, admins):
            self.assertEqual(self.admin_dir(lane["worktree"]), admin)
            self.assertIn(admin, osw.codex_gitdir_writable_roots(lane["worktree"], POLICY))

    def test_align_is_idempotent_and_keeps_the_worktree_usable(self):
        worktree = self.add_worktree(os.path.join(self.root, "lanes", "CX10", "wt"))
        first = osw.lane_align_worktree_admin_dir(self.repo, worktree, "CX10")
        second = osw.lane_align_worktree_admin_dir(self.repo, worktree, "CX10")
        self.assertEqual(first, second)
        self.assertEqual(git(worktree, "rev-parse", "--absolute-git-dir"), first)
        git(worktree, "commit", "-q", "--allow-empty", "-m", "probe")
        self.assertEqual(git(worktree, "log", "-1", "--format=%s"), "probe")

    def test_align_migrates_a_numbered_admin_dir(self):
        # A lane created before the fix owns ``wt1`` because another lane took ``wt``.
        self.add_worktree(os.path.join(self.root, "lanes", "CX5", "wt"))
        worktree = self.add_worktree(os.path.join(self.root, "lanes", "CX10", "wt"))
        self.assertEqual(os.path.basename(self.admin_dir(worktree)), "wt1")
        admin = osw.lane_align_worktree_admin_dir(self.repo, worktree, "CX10")
        self.assertEqual(os.path.basename(admin), "wt-CX10")
        self.assertEqual(self.admin_dir(worktree), admin)
        self.assertIn(worktree, git(self.repo, "worktree", "list"))

    def test_app_server_is_started_with_the_lane_writable_roots(self):
        worktree = self.add_worktree(os.path.join(self.root, "lanes", "CX10", "wt"))
        admin = self.admin_dir(worktree)
        with mock.patch.object(osw, "codex_config_writable_roots", return_value=[]):
            cmd = osw.codex_app_server_command("/usr/local/bin/codex", worktree, POLICY)
        self.assertIn("app-server", cmd)
        override = [arg for arg in cmd if arg.startswith("sandbox_workspace_write.writable_roots=")]
        self.assertEqual(len(override), 1)
        roots = json.loads(override[0].split("=", 1)[1])
        self.assertIn(admin, roots)
        self.assertIn(worktree, roots)

    def test_app_server_keeps_the_configured_roots(self):
        worktree = self.add_worktree(os.path.join(self.root, "lanes", "CX10", "wt"))
        with mock.patch.object(osw, "codex_config_writable_roots", return_value=["/shared/keep-me"]):
            roots = osw.codex_writable_roots(worktree, POLICY)
            cmd = osw.codex_app_server_command("/usr/local/bin/codex", worktree, POLICY)
        self.assertEqual(roots[0], "/shared/keep-me")
        self.assertIn(self.admin_dir(worktree), roots)
        override = [arg for arg in cmd if arg.startswith("sandbox_workspace_write.writable_roots=")]
        self.assertIn("/shared/keep-me", json.loads(override[0].split("=", 1)[1]))

    def test_roots_are_empty_when_the_switch_is_off(self):
        worktree = self.add_worktree(os.path.join(self.root, "lanes", "CX10", "wt"))
        off = {"codex": {"sandbox": "workspace-write", "worktree_gitdir_writable": False}}
        with mock.patch.object(osw, "codex_config_writable_roots", return_value=[]):
            self.assertEqual(osw.codex_writable_roots(worktree, off), [])
            cmd = osw.codex_app_server_command("/usr/local/bin/codex", worktree, off)
        self.assertFalse([arg for arg in cmd if arg.startswith("sandbox_workspace_write.writable_roots=")])


if __name__ == "__main__":
    unittest.main()
