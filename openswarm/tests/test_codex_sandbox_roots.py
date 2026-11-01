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


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class CodexGitdirWritableRootsTests(unittest.TestCase):
    def setUp(self):
        # A fixed /tmp parent keeps the "not a repo" case outside every git worktree.
        self.tmp = tempfile.TemporaryDirectory(prefix="oscodex-", dir="/tmp")
        self.root = os.path.realpath(self.tmp.name)
        self.main = os.path.join(self.root, "main")
        os.makedirs(self.main)
        git(self.main, "init", "-q")
        git(self.main, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
        self.wt = os.path.join(self.root, "wt7")
        git(self.main, "worktree", "add", "-q", self.wt, "-b", "lane")
        self.policy = {"codex": {"sandbox": "workspace-write", "worktree_gitdir_writable": True}}

    def tearDown(self):
        self.tmp.cleanup()

    def fake_codex(self, name, body):
        path = os.path.join(self.root, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def config_home(self, roots):
        home = os.path.join(self.root, "home")
        os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
        with open(os.path.join(home, ".codex", "config.toml"), "w") as handle:
            handle.write("[sandbox_workspace_write]\nwritable_roots = [%s]\n"
                         % ", ".join(json.dumps(root) for root in roots))
        return home

    def test_worktree_gets_exact_gitdir_and_shared_dirs(self):
        common = os.path.join(self.main, ".git")
        self.assertEqual(osw.codex_gitdir_writable_roots(self.wt, self.policy),
                         [self.wt, os.path.join(common, "worktrees", "wt7"),
                          os.path.join(common, "objects"), os.path.join(common, "refs"), os.path.join(common, "logs")])

    def test_plain_checkout_gets_its_gitdir_only(self):
        self.assertEqual(osw.codex_gitdir_writable_roots(self.main, self.policy),
                         [self.main, os.path.join(self.main, ".git")])

    def test_switch_off_or_other_sandbox_returns_nothing(self):
        self.assertEqual(osw.codex_gitdir_writable_roots(self.wt, {"codex": {"worktree_gitdir_writable": False}}), [])
        self.assertEqual(osw.codex_gitdir_writable_roots(self.wt, {"codex": {"sandbox": "read-only"}}), [])

    def test_default_is_on(self):
        self.assertTrue(osw.codex_gitdir_writable_roots(self.wt, {"codex": {}}))

    def test_not_a_repo_returns_nothing(self):
        self.assertEqual(osw.codex_gitdir_writable_roots(self.root, self.policy), [])

    def test_exec_flags_add_dir_appends_every_root(self):
        binary = self.fake_codex("codex-add-dir", "echo '      --add-dir <DIR>   Additional writable directories'")
        roots = osw.codex_gitdir_writable_roots(self.wt, self.policy)
        flags = osw.codex_writable_roots_flags(self.wt, self.policy, binary)
        self.assertEqual(flags, [flag for root in roots for flag in ("--add-dir", root)])

    def test_exec_flags_fallback_keeps_the_configured_roots(self):
        binary = self.fake_codex("codex-no-add-dir", "echo '      --json   Print events to stdout as JSONL'")
        home = self.config_home(["/shared/keep-me"])
        roots = osw.codex_gitdir_writable_roots(self.wt, self.policy)
        with mock.patch.object(osw, "HOME", home):
            flags = osw.codex_writable_roots_flags(self.wt, self.policy, binary)
        self.assertEqual(flags[0], "-c")
        key, value = flags[1].split("=", 1)
        self.assertEqual(key, "sandbox_workspace_write.writable_roots")
        self.assertEqual(json.loads(value), ["/shared/keep-me"] + roots)

    def test_exec_flags_are_empty_when_disabled(self):
        binary = self.fake_codex("codex-add-dir", "echo '      --add-dir <DIR>   Additional writable directories'")
        self.assertEqual(osw.codex_writable_roots_flags(self.wt, {"codex": {"worktree_gitdir_writable": False}}, binary), [])

    def test_add_dir_is_detected_per_codex_subcommand(self):
        binary = self.fake_codex(
            "codex-resume-only",
            "if [ \"$2\" = \"resume\" ]; then echo '      --add-dir <DIR>'; else echo '      --json'; fi")
        roots = osw.codex_gitdir_writable_roots(self.wt, self.policy)
        self.assertEqual(osw.codex_writable_roots_flags(self.wt, self.policy, binary, resume=True),
                         [flag for root in roots for flag in ("--add-dir", root)])
        self.assertTrue(osw.codex_writable_roots_flags(self.wt, self.policy, binary)[0] == "-c")


if __name__ == "__main__":
    unittest.main()
