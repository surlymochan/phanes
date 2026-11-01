"""Tests for docs/fusion-box-boot-recovery.md and the offline reboot drill (Refs #1478)."""

import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "fusion-box-boot-recovery.md"
DRILL = ROOT / "tools" / "check-box-boot-recovery.py"
START_CONTROL_PLANE = ROOT / "tools" / "lane-autodispatch" / "start-control-plane.example"

LOOPS = [
    "ib-refresh-loop",
    "lane-autodispatch",
    "lane-merge",
    "bus-worker-loop",
    "health-loop",
]


class BoxBootRecoveryDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(DOC.is_file(), f"Expected {DOC} to exist")
        self.content = DOC.read_text(encoding="utf-8")

    def test_acceptance_keywords_present(self):
        """验收条件: 文档必须点名 boot-ensure / start-control-plane / 自恢复。"""
        self.assertIn("boot-ensure", self.content)
        self.assertIn("start-control-plane", self.content)
        self.assertIn("自恢复", self.content)

    def test_five_loops_documented(self):
        """五件套与四条心跳路径都要在短跑手册里点名。"""
        for loop in LOOPS:
            self.assertIn(loop, self.content, f"docs/fusion-box-boot-recovery.md missing {loop}")
        for heartbeat in ("autodispatch/heartbeat", "merge-heartbeat", "ctl-loop.heartbeat", "health.heartbeat"):
            self.assertIn(heartbeat, self.content, f"docs/fusion-box-boot-recovery.md missing {heartbeat}")

    def test_claim_not_frozen_paths_documented(self):
        """已 claim 的 lane 的三条恢复路径都要写清楚。"""
        self.assertIn("reconcile", self.content)
        self.assertIn("lane-ready", self.content)
        self.assertIn("claim", self.content)

    def test_drill_evidence_is_recorded(self):
        """卡上必须有演练时间线证据：boot-ensure 前后的心跳年龄与结论行。"""
        self.assertIn("实测证据", self.content)
        self.assertIn("STALE", self.content)
        self.assertIn("fresh", self.content)


class BootEnsureMirrorTests(unittest.TestCase):
    def test_mirror_starts_all_five_loops(self):
        """Refs #1478: boot-ensure 镜像必须拉起五件套（缺 health-loop 会冻结 lane）。"""
        self.assertTrue(START_CONTROL_PLANE.is_file())
        content = START_CONTROL_PLANE.read_text(encoding="utf-8")
        for loop in LOOPS:
            self.assertIn(loop, content, f"start-control-plane.example missing {loop}")


class BoxBootRecoveryDrillTests(unittest.TestCase):
    def test_offline_reboot_drill_passes(self):
        """准真实重启演练：boot-ensure 拉回控制面，已 claim 的 lane 不被冻结。"""
        self.assertTrue(DRILL.is_file(), f"Expected {DRILL} to exist")
        res = subprocess.run([sys.executable, str(DRILL)], capture_output=True, text=True, timeout=300)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("boot-ensure 在模拟重启后拉回控制面五件套", res.stdout)
        self.assertIn("已 claim 的 lane 被释放回 lane-ready", res.stdout)


if __name__ == "__main__":
    unittest.main()
