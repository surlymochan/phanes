"""Codex sandbox integration regression: two lanes on one host each commit in their sandbox.

A Codex ``workspace-write`` sandbox mounts a worktree's git metadata read-only, so a lane can only
commit inside the sandbox when the sandbox's writable roots contain that lane's *exact* git admin
dir (``.git/worktrees/<name>``). Every lane uses a worktree directory named ``wt``, so git numbers
the admin dirs of the second lane (``wt``, ``wt1``); a root pinned to one name covered one lane only
and the other lane failed every git write with ``Read-only file system`` (Refs #1832).

The tests here drive the production path (``LaneChild`` + ``lane_begin_turn``) against the sandbox
fixture ``fixtures/fake_sandbox_app_server.py``, which applies the Codex mount rule to the lane's
git admin dir: a write outside the writable roots fails with ``Read-only file system``. The lane
that has its admin dir in the roots commits, the lane without it does not.

``test_real_codex_sandbox.py`` runs the same scenario against a real Codex CLI; it is skipped
unless ``OPENSWARM_REAL_CODEX=1`` is set, which keeps CI offline.

复审 BLOCKER 映射（B 编号 → 改动文件:行 → 测试名 → 结果；台账见
``docs/audit/issue-1832-codex-worktree-gitdir.md`` 第 4 节）：

* **B1**（真实 codex 沙箱内同机两车道各自提交）：
  ``swrm/bin/swrm:1798``（``codex_app_server_command``，app-server 启动即带可写根）、
  ``swrm/bin/swrm:7655``（``thread/start`` 带可写根）、``swrm/bin/swrm:7048``
  （``lane_align_worktree_admin_dir``：每条车道独占 ``.git/worktrees/wt-<lane>``）。
  测试：本文件 ``test_two_lanes_each_commit_in_their_own_sandbox``（两条同名 ``wt`` 车道各自产出提交）、
  ``test_lane_without_its_gitdir_root_cannot_commit``（反向对照：无根即 ``Read-only file system``）、
  ``test_app_server_is_launched_with_the_lane_admin_dir``、``test_thread_start_carries_the_lane_admin_dir``；
  真实 CLI 版 ``swrm/tests/test_real_codex_sandbox.py``（需 ``OPENSWARM_REAL_CODEX=1``，CI 跳过）。
  结果：离线 4 项全通过（门禁 ``BASE_SHA=4dc2f2c930 bash tools/ci-openswarm.sh`` 内）。
* **B2**（自查工具能检查实际车道）：``tools/check-codex-sandbox-gitdir.py``；
  测试 ``tools/tests/test_check_codex_sandbox_gitdir.py`` 4 项通过；govpn 现场核实仍未闭环（执行器沙箱无 DNS/外网）。
* **B3**（恢复并更新失败台账）：``docs/audit/issue-1832-codex-worktree-gitdir.md``（第 1–8 节）。
* **B4**（逐条映射）：本 docstring 与台账第 4 节。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
SANDBOX_APP_SERVER = os.path.join(FIXTURES, "fake_sandbox_app_server.py")

sys.path.insert(0, HERE)
import _load

osw = _load.load()

PROBE = "SANDBOX_PROBE: git commit --allow-empty -m probe-{name}"


def git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


class CodexSandboxCommitTests(unittest.TestCase):
    """Two lanes with the same worktree basename on one host, one sandbox each."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="swrm-sandbox-")
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
        os.chmod(SANDBOX_APP_SERVER, 0o755)
        self.worktrees = {}
        for name in ("CX5", "CX10"):
            worktree = os.path.join(self.root, "lanes", name, "wt")
            os.makedirs(os.path.dirname(worktree))
            git(self.repo, "worktree", "add", "-q", "--detach", worktree, "HEAD")
            self.worktrees[name] = worktree

    def tearDown(self):
        self.tmp.cleanup()

    def lane(self, name):
        return {"name": name, "backend": "codex", "model": "fake-model", "effort": "high",
                "binary": SANDBOX_APP_SERVER, "repo": self.repo}

    def admin_dir(self, worktree):
        return os.path.realpath(git(worktree, "rev-parse", "--absolute-git-dir"))

    def run_turn(self, lane, worktree, writable_roots=True):
        """Run one sandbox turn in ``worktree`` and return the reported command results.

        ``writable_roots=False`` removes the git metadata roots from the sandbox, both from the
        app-server start-up override and from the per-turn thread config.
        """
        event_file = os.path.join(self.root, "events-{}.jsonl".format(lane["name"]))
        patches = []
        if not writable_roots:
            patches = [mock.patch.object(osw, "codex_gitdir_writable_roots", return_value=[]),
                       mock.patch.object(osw, "codex_config_writable_roots", return_value=[])]
            for patch in patches:
                patch.start()
        child = None
        commands = []
        try:
            child = osw.LaneChild(lane, worktree, osw.load_policy())
            turn = {}
            osw.lane_begin_turn(lane, child, PROBE.format(name=lane["name"]), worktree, event_file, turn)
            deadline = time.monotonic() + 60
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
                if message.get("method") in ("turn/completed", "turn/failed"):
                    break
        finally:
            if child is not None:
                child.close()
            for patch in patches:
                patch.stop()
        return commands

    def head_subject(self, worktree):
        proc = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=worktree,
                              capture_output=True, text=True)
        return proc.stdout.strip()

    def test_two_lanes_each_commit_in_their_own_sandbox(self):
        # Both lanes use a worktree directory named ``wt``; git numbers the admin dirs (``wt``,
        # ``wt1``). The runtime gives every lane its own admin dir, and the sandbox roots follow
        # the real gitdir, so both lanes commit.
        admins = {}
        for name, worktree in self.worktrees.items():
            admins[name] = osw.lane_align_worktree_admin_dir(self.repo, worktree, name)
        self.assertEqual(sorted(os.path.basename(path) for path in admins.values()),
                         ["wt-CX10", "wt-CX5"])
        for name, worktree in self.worktrees.items():
            commands = self.run_turn(self.lane(name), worktree)
            self.assertEqual(self.head_subject(worktree), "probe-{}".format(name),
                             "lane {} did not commit in its sandbox: {}".format(name, commands))
        self.assertNotEqual(admins["CX5"], admins["CX10"])

    def test_lane_without_its_gitdir_root_cannot_commit(self):
        # Control for the test above: without the git admin dir in the writable roots the sandbox
        # mounts the index read-only, so the very same commit fails with "Read-only file system".
        worktree = self.worktrees["CX10"]
        osw.lane_align_worktree_admin_dir(self.repo, worktree, "CX10")
        commands = self.run_turn(self.lane("CX10"), worktree, writable_roots=False)
        self.assertNotEqual(self.head_subject(worktree), "probe-CX10",
                            "the sandbox wrote the index without the git admin dir in its roots")
        reported = "\n".join(str(entry) for entry in commands)
        self.assertIn("Read-only file system", reported,
                      "the sandbox did not report the read-only index: {}".format(reported))

    def test_app_server_is_launched_with_the_lane_admin_dir(self):
        worktree = self.worktrees["CX10"]
        admin = osw.lane_align_worktree_admin_dir(self.repo, worktree, "CX10")
        child = osw.LaneChild(self.lane("CX10"), worktree, osw.load_policy())
        try:
            command = " ".join(child.proc.args)
        finally:
            child.close()
        self.assertIn("sandbox_workspace_write.writable_roots=", command)
        self.assertIn(admin, json.dumps(osw.codex_writable_roots(worktree, osw.load_policy())))

    def test_thread_start_carries_the_lane_admin_dir(self):
        # The per-turn config is what a lane gets after its worktree was rebuilt: the launch
        # override of the resident app-server is stale by then.
        worktree = self.worktrees["CX10"]
        admin = osw.lane_align_worktree_admin_dir(self.repo, worktree, "CX10")
        self.run_turn(self.lane("CX10"), worktree)
        events = []
        with open(os.path.join(self.root, "events-CX10.jsonl"), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        roots = [event for event in events if event.get("type") == "codex_writable_roots"]
        self.assertTrue(roots, "the lane did not record its sandbox writable roots")
        self.assertIn(admin, roots[0]["roots"])


if __name__ == "__main__":
    unittest.main()
