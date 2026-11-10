"""Rendering and dispatch regression tests for the implementation and fix briefs."""
import ast
import re
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


class DispatchWiringTests(unittest.TestCase):
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

    def test_review_requested_body_marks_fix_round(self):
        self.assertTrue(AD.is_fix_round_card([], "实现功能", "审查要求修改（lane review）"))
        self.assertIn("## Fix round checklist", self.render(body="审查要求修改（lane review）"))

    def test_rebase_branch_body_marks_fix_round(self):
        self.assertTrue(AD.is_fix_round_card([], "实现功能", "从 rebase-pr-1841 续改"))
        self.assertIn("从 `rebase-pr-1841` 续改", self.render(body="从 rebase-pr-1841 续改"))

    def test_fix_markers_placeholder_and_idempotence(self):
        for title, body, labels in (("fix round: 修复", "", ()),
                                    ("实现功能", "## Fix Round\n", ()),
                                    ("实现功能", "", ("review-fix",))):
            with self.subTest(title=title, body=body, labels=labels):
                task = self.render(title, body, labels)
                self.assertIn("rebase-pr-<N>", task)
                self.assertEqual(self.render(title, task, labels).count("## Fix round checklist"), 1)



AD_DIR = ROOT / 'tools' / 'lane-autodispatch'
TOOLS_DIR = ROOT / 'tools'

def ad_sources():
    sources = sorted(AD_DIR.glob('*.py')) + sorted(TOOLS_DIR.glob('*.py'))
    sources.append(ROOT / 'swrm' / 'bin' / 'swrm')
    return [path for path in sources if path.is_file()]

def string_literals(path):
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]

def brief_candidates():
    return [(path, literal) for path in ad_sources() for literal in string_literals(path)]
IMPL_MARKERS = ('入口', '调用链', '数据结构')
TICK_TEST_MARKERS = ('tick|集成|integration', '测试|test')
FIX_ITEMS = {'F1': ('rebase-pr-', '祖先|ancestor'), 'F2': ('枚举|enumerat', '同类|同族|same[- ]kind'), 'F3': ('BLOCKER', '勾选|逐条|check ?list|check off|tick', '证据|evidence|依据|proof'), 'F4': ('失败测试|失败.{0,8}测试|failing test|先写失败|先红|red[- ]first',), 'F5': ('不删|不得删|不要删|保留|keep|not delete|no delete', '集成测试|集成级测试|integration test'), 'F6': ('(?<![0-9A-Za-z_])A4(?![0-9A-Za-z_])', '(?<![0-9A-Za-z_])A2(?![0-9A-Za-z_])')}

def marker_groups(patterns):
    return (patterns,) if isinstance(patterns, str) else tuple(patterns)

def item_satisfied(text, patterns):
    return all((re.search(pattern, text, re.I) for pattern in marker_groups(patterns)))

def missing_items(text):
    return [name for name, patterns in FIX_ITEMS.items() if not item_satisfied(text, patterns)]

def lists_the_six_items(text):
    if all((re.search(f'(?<![0-9A-Za-z_])F{n}(?![0-9A-Za-z_])', text) for n in range(1, 7))):
        return True
    lines = text.splitlines()
    used = set()
    for patterns in FIX_ITEMS.values():
        choices = [index for index, line in enumerate(lines) if index not in used and item_satisfied(line, patterns)]
        if not choices:
            return False
        used.add(choices[0])
    return True


class DispatchBriefTemplateTests(unittest.TestCase):
    """The brief templates the AD ships for an impl card and for a fix round."""

    @classmethod
    def setUpClass(cls):
        cls.candidates = brief_candidates()

    def fix_brief(self):
        """The fix round brief the dispatchers ship (None when there is none)."""
        for where, text in self.candidates:
            if not missing_items(text):
                return where, text
        return None, None

    def closest_candidate(self):
        """The literal that carries the most fix round items (for the failure report)."""
        best = None
        for where, text in self.candidates:
            missing = missing_items(text)
            score = len(FIX_ITEMS) - len(missing)
            if best is None or score > best[0]:
                best = (score, where, missing)
        return best

    # -- ① the impl card brief ----------------------------------------------------
    def test_the_impl_card_brief_forces_the_three_delivery_lines(self):
        """The impl card brief asks for entry point -> call chain -> data source."""
        matches = [(where, text) for where, text in self.candidates
                   if all(re.search(pattern, text) for pattern in IMPL_MARKERS)]
        self.assertTrue(matches,
                        "the AD ships no impl card brief with the three delivery lines "
                        "(入口 -> 调用链 -> 数据结构来源); the dispatch brief of an impl card "
                        "must force them")
        where, text = matches[0]
        self.assertTrue(all(re.search(pattern, text, re.I) for pattern in TICK_TEST_MARKERS),
                        f"the impl card brief ({where}) does not demand a tick/integration "
                        "level test driven from the real entry")

    # -- ② the fix round brief ----------------------------------------------------
    def test_the_fix_round_brief_forces_the_six_checklist_items(self):
        """The fix round brief forces F1..F6."""
        where, text = self.fix_brief()
        self.assertIsNotNone(text, self.no_fix_brief_message())
        self.assertEqual(missing_items(text), [],
                         f"the fix round brief ({where}) does not force every checklist item")

    def test_the_fix_round_brief_lists_the_six_items_one_by_one(self):
        """The six items are six entries of the brief, not one paragraph."""
        where, text = self.fix_brief()
        self.assertIsNotNone(text, self.no_fix_brief_message())
        self.assertTrue(lists_the_six_items(text),
                        f"the fix round brief ({where}) must list the six items F1..F6 "
                        "one by one, each with its own entry")

    # -- the failure report -------------------------------------------------------
    def no_fix_brief_message(self):
        """Why no fix round brief was found: the closest literal and what it misses."""
        score, where, missing = self.closest_candidate()
        if score <= 0:
            return ("the AD ships no fix round brief: no literal of the dispatcher sources "
                    "carries the F1..F6 checklist (F1 continue from rebase-pr-<N> with an "
                    "ancestor check, F2 enumerate the same kind of points first, F3 tick every "
                    "BLOCKER with evidence, F4 write the failing test first, F5 do not delete "
                    "the existing integration tests, F6 re-run A4/A2)")
        return (f"the AD ships no fix round brief: the closest literal ({where}) still misses "
                f"{', '.join(missing)}")


if __name__ == "__main__":
    unittest.main()
