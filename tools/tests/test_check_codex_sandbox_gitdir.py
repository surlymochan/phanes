"""Contract of the Codex sandbox gitdir self check (Refs #1832).

The tool has to run on a fleet host with two lanes that share the worktree directory
basename ``wt``: it must read the *real* git admin dir of every lane, ask the deployed
runtime for the writable roots it computes and report a verdict per lane. A lane whose
worktree is missing must be reported, not crash the check (the first version of the tool
raised an AttributeError on every codex lane).
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "check-codex-sandbox-gitdir.py"
SPEC = importlib.util.spec_from_file_location("check_codex_sandbox_gitdir", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

RUNTIME = Path(__file__).resolve().parents[2] / "swrm" / "bin" / "swrm"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True).stdout.strip()


class CheckCodexSandboxGitdirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-gitdir-check-")
        self.home = Path(self.tmp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Check Test")
        git(self.repo, "config", "user.email", "check@example.invalid")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.lanes = []
        for name in ("CX5", "CX10"):
            worktree = self.home / "lanes" / name / "wt"
            worktree.parent.mkdir(parents=True, exist_ok=True)
            git(self.repo, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
            self.lanes.append({"name": name, "backend": "codex", "model": "fake-model",
                                "repo": str(self.repo)})
        (self.home / "lanes.json").write_text(json.dumps({"lanes": self.lanes}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, *extra):
        command = [sys.executable, str(SCRIPT), "--home", str(self.home),
                   "--lanes", str(self.home / "lanes.json"), "--openswarm-bin", str(RUNTIME),
                   "--json"] + list(extra)
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
        return proc

    def test_both_lanes_are_reported_writable_with_their_own_admin_dir(self):
        proc = self.check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        rows = {row["lane"]: row for row in report["lanes"]}
        self.assertEqual(sorted(rows), ["CX10", "CX5"])
        for name, row in rows.items():
            self.assertEqual(row["verdict"], "writable", row)
            self.assertIn(row["gitdir"], row["runtime_roots"], row)
        self.assertNotEqual(rows["CX5"]["gitdir"], rows["CX10"]["gitdir"])
        self.assertEqual(report["shared_admin_dirs"], [])

    def test_a_lane_without_worktree_is_reported_instead_of_crashing(self):
        lanes = self.lanes + [{"name": "CX7", "backend": "codex", "model": "fake-model",
                                "repo": str(self.repo)}]
        (self.home / "lanes.json").write_text(json.dumps({"lanes": lanes}), encoding="utf-8")
        proc = self.check()
        report = json.loads(proc.stdout)
        rows = {row["lane"]: row for row in report["lanes"]}
        self.assertEqual(rows["CX7"]["verdict"], "missing-worktree")
        self.assertEqual(proc.returncode, 1)

    def test_a_lane_whose_admin_dir_is_not_a_root_is_reported_as_read_only(self):
        # A runtime without the gitdir support cannot make the admin dir writable.
        empty = self.home / "empty_runtime.py"
        empty.write_text("def load_policy():\n    return {}\n", encoding="utf-8")
        proc = self.check("--openswarm-bin", str(empty))
        report = json.loads(proc.stdout)
        for row in report["lanes"]:
            self.assertEqual(row["verdict"], "read-only-index", row)
        self.assertEqual(proc.returncode, 1)

    def test_pinned_roots_are_reported(self):
        codex_home = self.home / "codex-home"
        codex_home.mkdir(parents=True)
        (codex_home / "config.toml").write_text(
            "[sandbox_workspace_write]\nwritable_roots = [\"/pinned/root\"]\n", encoding="utf-8")
        proc = self.check("--codex-home", str(codex_home))
        report = json.loads(proc.stdout)
        self.assertEqual(report["pinned_roots"], ["/pinned/root"])
        self.assertTrue(all(row["pinned_root"] is False for row in report["lanes"]))


if __name__ == "__main__":
    unittest.main()
