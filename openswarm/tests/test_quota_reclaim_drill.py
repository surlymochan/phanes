"""Tests for docs/fusion-quota-reclaim-drill.md and the quota reclaim drill (Refs #1477)."""

import importlib.util
import os
import pathlib
import re
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DOC = ROOT / "docs" / "fusion-quota-reclaim-drill.md"
DRILL = ROOT / "tools" / "quota-reclaim-drill.py"
sys.path.insert(0, str(HERE))
import _load
xs = _load.load()


def load_drill():
    """Load the drill script as a module (it lives in tools/, outside the package)."""
    spec = importlib.util.spec_from_file_location("quota_reclaim_drill", str(DRILL))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QuotaReclaimDrillDocumentationTests(unittest.TestCase):
    """The evidence card is the issue's acceptance artifact, so it must stay in the repo."""

    def setUp(self):
        self.assertTrue(DOC.is_file(), f"Expected {DOC} to exist")
        self.content = DOC.read_text(encoding="utf-8")

    def test_acceptance_keywords_present(self):
        """验收条件: 文档必须点名 kill / reclaim / 名额。"""
        for keyword in ("kill", "reclaim", "名额"):
            self.assertIn(keyword, self.content)

    def test_injection_steps_and_snapshots_are_documented(self):
        """注入步骤、前后 lanes-status 快照与名额快照都要在卡上。"""
        for keyword in ("lane up", "lane assign", "fleet-capacity", "lanes-status",
                        "reservations", "fleet_recovered_at"):
            self.assertIn(keyword, self.content, f"docs/fusion-quota-reclaim-drill.md missing {keyword}")

    def test_evidence_has_timestamps(self):
        """回收成功证据必须带时间戳，不能只有结论。"""
        self.assertIn("实测证据", self.content)
        self.assertRegex(self.content, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_reaper_boundary_is_documented(self):
        """supervisor 死掉后由谁回收，必须写清楚，避免误读成「kill 掉就有人收尸」。"""
        self.assertIn("边界", self.content)
        self.assertIn("supervisor", self.content)


class QuotaReclaimDrillSafetyTests(unittest.TestCase):
    """The drill only ever deletes its own sandbox, and never inherits the host config."""

    def test_refuses_to_delete_a_foreign_directory(self):
        """--workdir 指向别人的目录时必须拒绝删除。"""
        drill = load_drill()
        with tempfile.TemporaryDirectory() as tmp:
            foreign = pathlib.Path(tmp) / "not-a-drill"
            foreign.mkdir()
            (foreign / "keep.txt").write_text("important\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                drill.Drill(str(foreign)).reset_sandbox()
            self.assertTrue((foreign / "keep.txt").exists())

    def test_deletes_its_own_marked_sandbox(self):
        """带标记的沙箱可以安全清掉。"""
        drill = load_drill()
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = pathlib.Path(tmp) / "sandbox"
            sandbox.mkdir()
            (sandbox / drill.SANDBOX_MARKER).write_text("x\n", encoding="utf-8")
            (sandbox / "junk.txt").write_text("x\n", encoding="utf-8")
            drill.Drill(str(sandbox)).reset_sandbox()
            self.assertFalse(sandbox.exists())

    def test_refuses_dangerous_workdirs(self):
        """/、$HOME、仓库根这类路径绝不能被当成沙箱。"""
        drill = load_drill()
        for path in ("/", str(pathlib.Path.home()), str(drill.ROOT), str(drill.ROOT.parent)):
            with self.subTest(path=path):
                with self.assertRaises(RuntimeError):
                    drill.Drill.assert_safe_workdir(pathlib.Path(path))

    def test_host_environment_is_sanitised(self):
        """生产的 lanes.json / 真 pi 二进制不能漏进沙箱。"""
        drill = load_drill()
        polluted = {"OPENSWARM_LANES_FILE": "/production/lanes.json",
                    "XSWARM_LANES_FILE": "/production/lanes.json",
                    "OPENSWARM_PI_RPC_BIN": "/usr/bin/pi"}
        os.environ.update(polluted)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                instance = drill.Drill(str(pathlib.Path(tmp) / "q"))
        finally:
            for key in polluted:
                os.environ.pop(key, None)
        for key in polluted:
            self.assertNotIn(key, instance.env)

    def test_sandbox_env_keeps_only_the_drill_variables(self):
        """沙箱里只允许 drill 自己设的那三个 OPENSWARM_* 变量。"""
        drill = load_drill()
        with tempfile.TemporaryDirectory() as tmp:
            instance = drill.Drill(str(pathlib.Path(tmp) / "q"))
            instance.prepare()
            keys = sorted(key for key in instance.env if key.startswith(("OPENSWARM_", "XSWARM_")))
        self.assertEqual(["OPENSWARM_FLEET_CAPACITY_DIR", "OPENSWARM_FLEET_GUARD_CONFIG",
                          "OPENSWARM_HOME"], keys)

    def test_socket_path_limit_matches_the_cli(self):
        """AF_UNIX 上限必须跟 openswarm 自己的校验一致（macOS 103 / Linux 107）。"""
        drill = load_drill()
        with self.assertRaises(RuntimeError) as caught:
            xs.lane_validate_socket_path("x" * 200)
        capacity = int(re.search(r"exceeds the (\d+)-byte", str(caught.exception)).group(1))
        self.assertEqual(drill.SOCKET_PATH_LIMIT, capacity - 1)


if __name__ == "__main__":
    unittest.main()
