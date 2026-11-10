"""Command line regression tests for the audit section markers of the card builder.

The card builder must reject an audit range whose start or end marker is absent:
it must exit non-zero and leave no card behind. A complete range still produces
a card holding only the audited section.
"""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "mkcard.py"
START = "## AUDIT START"
END = "## AUDIT END"


class MkCardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_audit(self, text):
        audit = self.root / "audit.md"
        audit.write_text(text, encoding="utf-8")
        return audit

    def build(self, audit, out=None, start=START, end=END, checks="check command"):
        out = out or self.root / "card.md"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(out), "Card title", str(audit), start, end,
             "scope text", checks],
            capture_output=True, text=True, check=False)
        return result, out

    def test_generated_cards_include_acceptance_and_failure_patterns(self):
        audit = self.write_audit(START + "\nfinding one\n" + END + "\n")
        for number in range(3):
            result, out = self.build(audit, out=self.root / f"card-{number}.md")
            self.assertEqual(result.returncode, 0)
            card = out.read_text(encoding="utf-8")
            self.assertIn("labels: p1", card)
            self.assertIn("验收：check command", card)
            self.assertIn("## 失败模式清单", card)
            self.assertIn("docs/defect-patterns.md", card)

    def test_three_generated_cards_pass_real_preflight(self):
        audit = self.write_audit(START + "\n" + "\n".join(
            f"Repair scripts/check.ts case {i} and verify its output against the fixture."
            for i in range(35)) + "\n" + END + "\n")
        for number, checks in enumerate(("bun scripts/check.ts", "bunx biome check tools/mkcard.py",
                                         "ls docs/backlog/next/*.md | wc -l | grep -qx 20")):
            with self.subTest(checks=checks):
                result, out = self.build(audit, out=self.root / f"real-{number}.md", checks=checks)
                self.assertEqual(result.returncode, 0, result.stderr)
                lint = subprocess.run([sys.executable, str(SCRIPT.parents[1] / "swrm/bin/swrm"),
                                       "card-lint", str(out)], capture_output=True, text=True)
                self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
                card = out.read_text()
                for field in ("docs/defect-patterns.md", "复刻的引擎数据形状清单", "真实形状 fixture",
                              "单后端 / drain / 暂停 / 已派发后变化 / 合并后清理 / dry-run 无副作用",
                              "tick/集成级测试"):
                    self.assertIn(field, card)

    def test_missing_start_marker_is_rejected(self):
        audit = self.write_audit("intro\n\n" + END + "\nfinding one\n")
        result, out = self.build(audit)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(out.exists())

    def test_missing_end_marker_is_rejected(self):
        audit = self.write_audit("intro\n\n" + START + "\nfinding one\n")
        result, out = self.build(audit)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(out.exists())

    def test_missing_markers_leave_no_partial_card(self):
        audit = self.write_audit("intro\n\nfinding one\n")
        result, out = self.build(audit)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(out.exists())

    def test_existing_card_is_not_rewritten_when_markers_are_missing(self):
        out = self.root / "card.md"
        out.write_text("previous card\n", encoding="utf-8")
        audit = self.write_audit("intro\n\n" + START + "\nfinding one\n")
        result, _ = self.build(audit, out=out)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("finding one", out.read_text(encoding="utf-8") if out.exists() else "")

    def test_error_message_names_the_missing_marker(self):
        audit = self.write_audit("intro\n\n" + START + "\nfinding one\n")
        result, out = self.build(audit)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("end", result.stderr)
        self.assertFalse(out.exists())

    def test_complete_range_writes_the_card(self):
        audit = self.write_audit(
            "intro\n\n" + START + "\nfinding one\n\n" + END + "\ntrailer\n")
        result, out = self.build(audit)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(out.exists())
        card = out.read_text(encoding="utf-8")
        self.assertIn("# Card title", card)
        self.assertIn("finding one", card)
        self.assertNotIn("trailer", card)


if __name__ == "__main__":
    unittest.main()
