"""Regression of #1851: the AD ships a fix round brief that forces the rubric F1..F6.

The issue asks the dispatcher to hand a fix round card a task book that forces the six
checklist items of the rubric:

===== =========================================================
item  what the fix round must force
===== =========================================================
F1    continue from ``rebase-pr-<N>`` (ancestor check)
F2    enumerate the same kind of points first
F3    tick every BLOCKER of the last round, with evidence
F4    write the failing test first
F5    do not delete the existing integration tests
F6    re-run A4/A2
===== =========================================================

The brief is a constant of the dispatcher (``FIX_ROUND_TASK_BOOK``) and it is injected
into the task book of a fix round card -- a card that carries a fix marker (a fix label,
a ``## Fix Round`` heading or a fix round title). An ordinary work card and a card of
another class (planner / follow-up / verify) keep their task book: the checklist asks
for code, and those cards ask for none.

The rendering of the template itself is covered by
``tools/tests/test_dispatch_brief_templates.py``; this file covers the wiring: which
card gets the checklist, which one does not, and that the PR of the round is named.

Run:

    python3 -m unittest tools.tests.test_fix_round_brief -v
"""

import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
import warnings


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
SPEC = importlib.util.spec_from_file_location("autodispatch_fix_round", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# The dispatcher opens a few files without closing them: the warnings must not
# hide the failures of this file.
warnings.filterwarnings("ignore", category=ResourceWarning)

# The six checklist items, as the marker groups of the brief: every group of an item
# must be in the task book.
FIX_MARKERS = {
    "F1": (r"rebase-pr-", r"祖先|ancestor"),
    "F2": (r"枚举|enumerat", r"同类|同族|same[- ]kind"),
    "F3": (r"BLOCKER", r"勾选|逐条|check ?list|check off|tick", r"证据|evidence|依据|proof"),
    "F4": (r"失败测试|失败.{0,8}测试|failing test|先写失败|先红|red[- ]first",),
    "F5": (r"不删|不得删|不要删|保留|keep|not delete|no delete", r"集成测试|集成级测试|integration test"),
    "F6": (r"(?<![0-9A-Za-z_])A4(?![0-9A-Za-z_])", r"(?<![0-9A-Za-z_])A2(?![0-9A-Za-z_])"),
}

# The heading of the brief: it names the checklist, so it is the marker of a task book
# that carries the fix round brief.
FIX_ROUND_HEADING = "## Fix round checklist"


def missing_items(text):
    """The checklist items the task book does not carry."""
    return [name for name, groups in FIX_MARKERS.items()
            if not all(re.search(pattern, text, re.I) for pattern in groups)]


def carries_the_fix_brief(text):
    """True when the task book carries the fix round brief."""
    return FIX_ROUND_HEADING in text and "rebase-pr-" in text


class FakeShell:
    """The ssh side of the dispatcher: the upload and the lane assignment succeed."""

    def __init__(self):
        self.commands = []

    def __call__(self, cmd, timeout=120, inp=None):
        self.commands.append(cmd)
        return 0, '{"id": "run-1"}', ""


class FixRoundBriefTests(unittest.TestCase):
    """The task book a lane receives for a card, per card class."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(self.scratch_dir()))
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("D", self.tmp.name), ("sh", MODULE.sh), ("log", MODULE.log)):
            self.addCleanup(setattr, MODULE, name, getattr(MODULE, name))
            setattr(MODULE, name, value)
        MODULE.sh = FakeShell()
        MODULE.log = lambda message, brain=True: None
        self.cards = 0

    @staticmethod
    def scratch_dir():
        """A temporary directory inside the worktree (the repo keeps `.tmp/` ignored)."""
        base = ROOT / ".tmp"
        base.mkdir(exist_ok=True)
        return base

    def task_book(self, title="修复轮：#1806 的 BLOCKER", body="上一轮审查给了 3 条 BLOCKER。\n",
                 labels=("p1", "backend:codex", "card:fix"), number=None):
        """The task file the dispatcher writes for a card."""
        self.cards += 1
        number = number or 1800 + self.cards
        item = {"n": number, "title": title, "body": body, "labels": sorted(labels)}
        cfg = {"host": "box", "fleet": "/usr/local/bin/swrm-fleet", "repo": "~/work/phanes-fleet"}
        MODULE.dispatch("CX1", cfg, item, "0" * 40)
        task_file = Path(self.tmp.name) / "tasks" / f"CX1-{number}.md"
        self.assertTrue(task_file.is_file(), "the dispatcher wrote no task file for the card")
        return task_file.read_text(encoding="utf-8")

    # -- the fix round card gets the checklist ------------------------------------
    def test_a_fix_round_card_task_book_carries_the_six_checklist_items(self):
        task = self.task_book()
        self.assertEqual(missing_items(task), [],
                         "the task book of a fix round card must force the six checklist items")

    def test_a_fix_round_card_task_book_lists_the_six_items_one_by_one(self):
        task = self.task_book()
        for number in range(1, 7):
            with self.subTest(item=f"F{number}"):
                self.assertRegex(task, rf"(?<![0-9A-Za-z_])F{number}(?![0-9A-Za-z_])")

    def test_a_fix_round_card_keeps_the_impl_delivery_requirements(self):
        """A fix round delivers code: the three delivery lines still apply."""
        task = self.task_book()
        for marker in ("入口", "调用链", "数据结构"):
            with self.subTest(marker=marker):
                self.assertIn(marker, task)

    # -- the card names the PR of the round ----------------------------------------
    def test_the_brief_names_the_pr_of_the_round(self):
        task = self.task_book(body="上一轮审查给了 3 条 BLOCKER。\n目标 PR：#1806\n")
        self.assertIn("rebase-pr-1806", task)
        self.assertNotIn("{pr}", task)

    def test_the_brief_names_the_pr_named_in_the_title(self):
        task = self.task_book(title="fix round: PR #1806 的 BLOCKER")
        self.assertIn("rebase-pr-1806", task)

    def test_the_brief_keeps_the_placeholder_when_the_card_names_no_pr(self):
        task = self.task_book(body="上一轮审查给了 3 条 BLOCKER。\n")
        self.assertIn("rebase-pr-<N>", task)

    # -- the other cards keep their task book --------------------------------------
    def test_an_ordinary_work_card_does_not_get_the_fix_round_checklist(self):
        task = self.task_book(title="解析器补充空值保护", labels=("p1", "backend:codex"))
        self.assertFalse(carries_the_fix_brief(task),
                         "an ordinary work card must not get the fix round checklist")

    def test_a_card_of_another_class_does_not_get_the_fix_round_checklist(self):
        for kind in ("planner", "follow-up", "ops", "upstream", "verify-intent"):
            with self.subTest(card_class=kind):
                task = self.task_book(title=f"{kind}: 修复轮：#1806 的 BLOCKER",
                                      labels=("p1", "backend:codex", f"card:{kind}", "card:fix"))
                self.assertFalse(carries_the_fix_brief(task),
                                 f"a {kind} card must not get the fix round checklist")

    # -- the markers of a fix round card --------------------------------------------
    def test_a_fix_round_heading_marks_the_card(self):
        task = self.task_book(title="解析器补充空值保护", body="## Fix Round\n上一轮 3 条 BLOCKER。\n",
                              labels=("p1", "backend:codex"))
        self.assertEqual(missing_items(task), [])

    def test_a_fix_round_title_marks_the_card(self):
        task = self.task_book(title="fix round: #1806 的 BLOCKER", labels=("p1", "backend:codex"))
        self.assertEqual(missing_items(task), [])

    def test_every_fix_label_marks_the_card(self):
        for label in ("fix", "fix-round", "review-fix", "rework",
                      "card:fix", "card:fix-round", "card:review-fix", "card:rework",
                      "type:fix", "type:fix-round", "type:review-fix", "type:rework"):
            with self.subTest(label=label):
                task = self.task_book(title="解析器补充空值保护", labels=("p1", "backend:codex", label))
                self.assertEqual(missing_items(task), [])

    # -- the checklist is injected once ---------------------------------------------
    def test_the_checklist_is_not_injected_twice(self):
        first = self.task_book()
        second = self.task_book(body=first)
        self.assertEqual(second.count("rebase-pr-"), first.count("rebase-pr-"),
                         "the fix round checklist must be injected once, not once per pass")


if __name__ == "__main__":
    unittest.main()
