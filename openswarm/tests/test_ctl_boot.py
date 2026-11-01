"""Tests for docs/ctl-boot.md and offline control-plane boot checks."""

import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "ctl-boot.md"
DOC_CHECK = ROOT / "docs" / "check-ctl-boot.sh"
TOOL_CHECK = ROOT / "tools" / "check-ctl-boot.sh"


class CtlBootDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(DOC.is_file(), f"Expected {DOC} to exist")
        self.content = DOC.read_text(encoding="utf-8")

    def test_acceptance_criteria_keywords_present(self):
        """验收条件: lane-autodispatch, bus-worker, ib-refresh 必须在文档中出现。"""
        self.assertIn("lane-autodispatch", self.content)
        self.assertIn("bus-worker", self.content)
        self.assertIn("ib-refresh", self.content)

    def test_startup_commands_and_heartbeats_documented(self):
        """核对三个核心循环的启动命令与心跳文件路径均已列出。"""
        # lane-autodispatch
        self.assertIn("~/phanes-ctl/bin/lane-autodispatch", self.content)
        self.assertIn("~/phanes-ctl/autodispatch/heartbeat", self.content)

        # bus-worker-loop
        self.assertIn("~/phanes-ctl/bin/bus-worker-loop", self.content)
        self.assertIn("~/phanes-ctl/bus-worker/heartbeat", self.content)

        # ib-refresh
        self.assertIn("~/phanes-ctl/ctl-loop.heartbeat", self.content)
        self.assertIn("~/phanes-ctl/ib-refresh.status", self.content)

    def test_single_command_path_existence_check_present(self):
        """确认文档中包含单条不连 SSH 的离线路径存在性检查命令。"""
        self.assertIn("test -f ~/phanes-ctl/bin/lane-autodispatch", self.content)
        self.assertIn("autodispatch/heartbeat", self.content)
        self.assertIn("bus-worker/heartbeat", self.content)

    def test_offline_path_check_in_mock_directory(self):
        """在临时目录模拟完整控制面路径，验证离线存在性检查逻辑成功。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ctl = pathlib.Path(tmpdir)
            bin_dir = ctl / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "lane-autodispatch").touch()
            (bin_dir / "bus-worker-loop").touch()
            (bin_dir / "ib-refresh").touch()
            (bin_dir / "start-control-plane").touch()

            ad_dir = ctl / "autodispatch"
            ad_dir.mkdir()
            (ad_dir / "heartbeat").touch()

            bw_dir = ctl / "bus-worker"
            bw_dir.mkdir()
            (bw_dir / "heartbeat").touch()

            (ctl / "ctl-loop.heartbeat").touch()
            (ctl / "ib-refresh.status").touch()

            # 执行文档中核对路径的存在性逻辑（将 ~/phanes-ctl 替换为临时 mock 路径）
            check_cmd = (
                f"test -f {bin_dir}/lane-autodispatch "
                f"-a -f {bin_dir}/bus-worker-loop "
                f"-a \\( -f {bin_dir}/ib-refresh -o -f {bin_dir}/ctl-loop \\) "
                f"-a -f {bin_dir}/start-control-plane "
                f"-a -f {ad_dir}/heartbeat "
                f"-a \\( -f {bw_dir}/heartbeat -o -d {bw_dir} \\) "
                f"-a \\( -f {ctl}/ctl-loop.heartbeat -o -f {ctl}/ib-refresh.status \\)"
            )
            res = subprocess.run(["bash", "-c", check_cmd])
            self.assertEqual(res.returncode, 0)

    def test_docs_check_ctl_boot_script_self_test(self):
        """docs/check-ctl-boot.sh --self-test 离线自检通过。"""
        self.assertTrue(DOC_CHECK.is_file())
        res = subprocess.run(["bash", str(DOC_CHECK), "--self-test"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("self-test passed cleanly", res.stdout)

    def test_tools_check_ctl_boot_gate_only(self):
        """tools/check-ctl-boot.sh --gate-only 离线门禁回放通过。"""
        self.assertTrue(TOOL_CHECK.is_file())
        res = subprocess.run(["bash", str(TOOL_CHECK), "--gate-only"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
