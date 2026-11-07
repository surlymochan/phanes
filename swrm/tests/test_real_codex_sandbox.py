"""Real Codex sandbox regression: two lanes on one host each commit in their own sandbox.

A Codex ``workspace-write`` sandbox mounts a worktree's git metadata read-only, so a lane can only
commit inside the sandbox when the sandbox's writable roots contain that lane's *exact* git admin
dir (``.git/worktrees/<name>``). Two lanes whose worktree directories share a basename (``wt``) get
different admin dirs (``wt`` and ``wt1``); a root pinned to one name covered one lane only and the
other lane failed every git write with ``Read-only file system`` (Refs #1832).

These tests drive the production path (``LaneChild`` + ``lane_begin_turn``) against a real Codex
CLI, so the roots the runtime computes are the ones the sandbox really applies. They are skipped
unless a real Codex binary is available and ``OPENSWARM_REAL_CODEX=1`` is set, which keeps CI
offline::

    OPENSWARM_REAL_CODEX=1 OPENSWARM_REAL_CODEX_MODEL=gpt-6-luna \
        python3 -m unittest swrm.tests.test_real_codex_sandbox

The offline regression of the same contract lives in ``test_codex_sandbox_commit.py``.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

# ``_load`` clears the OPENSWARM_* environment, so the opt-in flags are read before it is imported.
REAL_CODEX = os.environ.get("OPENSWARM_REAL_CODEX")
REAL_CODEX_BIN = os.environ.get("OPENSWARM_REAL_CODEX_BIN")
REAL_CODEX_MODEL = os.environ.get("OPENSWARM_REAL_CODEX_MODEL")
TIMEOUT = int(os.environ.get("OPENSWARM_REAL_CODEX_TIMEOUT", "600"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load

osw = _load.load()


def codex_binary():
    binary = REAL_CODEX_BIN or shutil.which("codex")
    return binary if binary and os.path.isfile(binary) else None


def git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


class RealCodexSandboxTests(unittest.TestCase):
    """Two lanes with the same worktree basename on one host, one real Codex sandbox each."""

    def setUp(self):
        if REAL_CODEX != "1":
            self.skipTest("set OPENSWARM_REAL_CODEX=1 to run the real Codex sandbox regression")
        self.binary = codex_binary()
        if not self.binary:
            self.skipTest("no codex binary found")
        self.model = REAL_CODEX_MODEL or "gpt-6-luna"
        self.tmp = tempfile.TemporaryDirectory(prefix="swrm-realcodex-")
        self.root = os.path.realpath(self.tmp.name)
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Sandbox Test")
        git(self.repo, "config", "user.email", "sandbox-test@example.invalid")
        with open(os.path.join(self.repo, "README.md"), "w") as handle:
            handle.write("base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.worktrees = {}
        for name in ("CX5", "CX10"):
            worktree = os.path.join(self.root, "lanes", name, "wt")
            os.makedirs(os.path.dirname(worktree))
            git(self.repo, "worktree", "add", "-q", "--detach", worktree, "HEAD")
            self.worktrees[name] = worktree

    def tearDown(self):
        self.tmp.cleanup()

    def lane(self, name):
        return {"name": name, "backend": "codex", "model": self.model, "effort": "high",
                "binary": self.binary, "repo": self.repo}

    def run_turn(self, lane, worktree, prompt, writable_roots=True):
        """Run one real Codex turn in ``worktree`` and return the commands it reported.

        ``writable_roots=False`` removes the git metadata roots from the sandbox, both from the
        app-server start-up override and from the per-turn thread config.
        """
        event_file = os.path.join(self.root, "events.jsonl")
        patches = []
        if not writable_roots:
            patches = [mock.patch.object(osw, "codex_gitdir_writable_roots", return_value=[]),
                       mock.patch.object(osw, "codex_config_writable_roots", return_value=[])]
            for patch in patches:
                patch.start()
        child = None
        commands = []
        messages = []
        try:
            child = osw.LaneChild(lane, worktree, osw.load_policy())
            turn = {}
            osw.lane_begin_turn(lane, child, prompt, worktree, event_file, turn)
            deadline = time.monotonic() + TIMEOUT
            while time.monotonic() < deadline:
                message = child.next_event(1.0)
                if message is False:
                    continue
                if message is None:
                    break
                params = message.get("params") or {}
                item = params.get("item") or {}
                if item.get("type") == "commandExecution":
                    commands.append({"command": item.get("command"), "exit": item.get("exitCode"),
                                     "output": item.get("aggregatedOutput") or ""})
                if item.get("type") in ("agentMessage", "agent_message"):
                    messages.append(item.get("text") or "")
                if message.get("method") in ("turn/completed", "turn/failed"):
                    break
        finally:
            if child is not None:
                child.close()
            for patch in patches:
                patch.stop()
        return commands, "\n".join(messages)

    def head_subject(self, worktree):
        proc = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=worktree,
                              capture_output=True, text=True)
        return proc.stdout.strip()

    def test_two_lanes_each_commit_in_their_own_sandbox(self):
        for name, worktree in self.worktrees.items():
            osw.lane_align_worktree_admin_dir(self.repo, worktree, name)
            prompt = ("Run exactly this shell command in the current directory and then stop: "
                      f"git commit --allow-empty -m probe-{name}. "
                      "Report the exact output of the command, including any error.")
            commands, messages = self.run_turn(self.lane(name), worktree, prompt)
            self.assertEqual(self.head_subject(worktree), "probe-{}".format(name),
                             "lane {} did not commit in its sandbox: {} {}".format(name, commands, messages))

    def test_lane_without_its_gitdir_root_cannot_commit(self):
        # Control for the test above: without the git admin dir in the writable roots the sandbox
        # mounts the index read-only, so the very same commit fails with "Read-only file system".
        worktree = self.worktrees["CX10"]
        osw.lane_align_worktree_admin_dir(self.repo, worktree, "CX10")
        prompt = ("Run exactly this shell command in the current directory and then stop: "
                  "git commit --allow-empty -m probe-blocked. "
                  "Report the exact output of the command, including any error.")
        commands, messages = self.run_turn(self.lane("CX10"), worktree, prompt, writable_roots=False)
        self.assertNotEqual(self.head_subject(worktree), "probe-blocked",
                            "the sandbox wrote the index without the git admin dir in its roots")
        reported = "\n".join([str(entry) for entry in commands] + [messages])
        self.assertIn("Read-only file system", reported,
                      "the sandbox did not report the read-only index: {}".format(reported))


if __name__ == "__main__":
    unittest.main()
