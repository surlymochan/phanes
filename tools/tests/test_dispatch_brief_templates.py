"""Rendering and dispatch regression tests for the implementation and fix briefs."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "autodispatch_briefs", ROOT / "tools/lane-autodispatch/autodispatch.py")
AD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AD)


class DispatchBriefTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("D", self.tmp.name),
                            ("sh", lambda *args, **kwargs: (0, '{"id":"run-1"}', "")),
                            ("log", lambda *args, **kwargs: None)):
            patcher = patch.object(AD, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.number = 1800

    def render(self, title="实现功能", body="", labels=()):
        self.number += 1
        item = {"n": self.number, "title": title, "body": body, "labels": list(labels)}
        cfg = {"host": "box", "fleet": "fleet", "repo": "repo"}
        AD.dispatch("CX1", cfg, item, "0" * 40)
        return (Path(self.tmp.name) / "tasks" / f"CX1-{self.number}.md").read_text()

    def test_impl_brief_three_lines_and_excluded_class(self):
        task = self.render()
        for marker in ("入口", "调用链", "数据结构来源", "tick", "集成"):
            self.assertIn(marker, task)
        other = self.render(labels=("card:planner",))
        self.assertNotIn(AD.IMPL_CARD_TASK_BOOK, other)

    def test_fix_brief_six_items_and_ordinary_card(self):
        task = self.render(body="目标 PR：#1806", labels=("card:fix",))
        for marker in ("F1", "F2", "F3", "F4", "F5", "F6", "rebase-pr-1806",
                       "祖先", "枚举", "同类", "BLOCKER", "证据", "失败测试",
                       "集成测试", "A4", "A2"):
            self.assertIn(marker, task)
        self.assertNotIn("## Fix round checklist", self.render())
        self.assertNotIn("## Fix round checklist", self.render(labels=("card:planner", "card:fix")))

    def test_fix_markers_placeholder_and_idempotence(self):
        for title, body, labels in (("fix round: 修复", "", ()),
                                    ("实现功能", "## Fix Round\n", ()),
                                    ("实现功能", "", ("review-fix",))):
            with self.subTest(title=title, body=body, labels=labels):
                task = self.render(title, body, labels)
                self.assertIn("rebase-pr-<N>", task)
                self.assertEqual(self.render(title, task, labels).count("## Fix round checklist"), 1)


if __name__ == "__main__":
    unittest.main()
