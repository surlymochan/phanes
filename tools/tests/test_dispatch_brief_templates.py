"""Reproduction of #1851: the dispatch brief templates of the AD.

The issue asks for two brief templates in the AD (the lane dispatcher mirror under
``tools/lane-autodispatch/``), the text a lane receives as its task book:

* the **impl card** brief forces the three delivery lines -- entry point -> call
  chain -> data structure source -- and at least one tick/integration level test.
  That half is already shipped (#1839, see ``tools/tests/test_impl_card_task_book.py``);
  the test below keeps it pinned.
* the **fix round** brief forces the six checklist items of the rubric:

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

The acceptance of the issue is a rendering test: the rendered impl brief carries the
three-line template, the rendered fix brief carries the six items F1..F6. Whether
the AD really hands the brief to a lane is verified after the dispatch by the
tester (same idea as #1847), so this file stays on the template level.

**The fix round tests are red on the base on purpose**: the base commit
(``dfaffd73b``) ships the impl task book only -- there is no fix round brief
anywhere in the AD sources. They turn green once the AD ships a fix round brief
that forces F1..F6.

The templates are looked up by content, not by name: a brief is any string literal
of the AD sources that carries the required markers, so the constant may live in
``autodispatch.py``, in a module next to it, or inside the dispatcher itself.

Mapping of the acceptance of the issue to the tests
---------------------------------------------------

* "渲染实现卡简报含三行模板" ->
  ``DispatchBriefTemplateTests.test_the_impl_card_brief_forces_the_three_delivery_lines``
* "渲染 fix 简报含 F1–F6 六条" ->
  ``DispatchBriefTemplateTests.test_the_fix_round_brief_forces_the_six_checklist_items``,
  ``DispatchBriefTemplateTests.test_the_fix_round_brief_lists_the_six_items_one_by_one``

Run:

    python3 -m unittest tools.tests.test_dispatch_brief_templates -v
"""

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AD_DIR = ROOT / "tools" / "lane-autodispatch"
TOOLS_DIR = ROOT / "tools"

# The sources that may carry the brief templates: the AD mirror and the modules
# next to it, the top level tools modules and the swrm dispatcher CLI. The test
# files of `tools/tests/` are not scanned: a template lives in the dispatcher, not
# in a test.
def ad_sources():
    """The source files of the dispatchers, in a stable order."""
    sources = sorted(AD_DIR.glob("*.py")) + sorted(TOOLS_DIR.glob("*.py"))
    sources.append(ROOT / "swrm" / "bin" / "swrm")
    return [path for path in sources if path.is_file()]


def string_literals(path):
    """Every string literal of a source file (a brief template is a literal)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def brief_candidates():
    """The brief templates the dispatchers ship: (where, text) of every literal."""
    return [(path, literal) for path in ad_sources() for literal in string_literals(path)]


# ① the impl card brief: the three delivery lines.
IMPL_MARKERS = (r"入口", r"调用链", r"数据结构")
# and at least one tick/integration level test.
TICK_TEST_MARKERS = (r"tick|集成|integration", r"测试|test")

# ② the fix round brief: the six items F1..F6. Every entry is a marker group: a
# group matches when one of its alternatives is in the text, an item is satisfied
# when all of its groups are. The alternatives keep the check on the meaning and
# not on one wording (the AD writes its briefs in Chinese, a lane may read them
# in either language).
FIX_ITEMS = {
    "F1": (r"rebase-pr-", r"祖先|ancestor"),
    "F2": (r"枚举|enumerat", r"同类|同族|same[- ]kind"),
    "F3": (r"BLOCKER", r"勾选|逐条|check ?list|check off|tick",
           r"证据|evidence|依据|proof"),
    "F4": (r"失败测试|失败.{0,8}测试|failing test|先写失败|先红|red[- ]first",),
    "F5": (r"不删|不得删|不要删|保留|keep|not delete|no delete",
           r"集成测试|集成级测试|integration test"),
    "F6": (r"(?<![0-9A-Za-z_])A4(?![0-9A-Za-z_])", r"(?<![0-9A-Za-z_])A2(?![0-9A-Za-z_])"),
}


def marker_groups(patterns):
    """The marker groups of an item: always a tuple, never a bare string."""
    return (patterns,) if isinstance(patterns, str) else tuple(patterns)


def item_satisfied(text, patterns):
    """True when the text carries every marker group of one fix round item."""
    return all(re.search(pattern, text, re.I) for pattern in marker_groups(patterns))


def missing_items(text):
    """The fix round items the text does not carry."""
    return [name for name, patterns in FIX_ITEMS.items() if not item_satisfied(text, patterns)]


def lists_the_six_items(text):
    """True when the six items are six separate entries of the brief.

    Either the entries are labelled F1..F6, or every item has a line of its own.
    """
    if all(re.search(rf"(?<![0-9A-Za-z_])F{n}(?![0-9A-Za-z_])", text) for n in range(1, 7)):
        return True
    lines = text.splitlines()
    used = set()
    for patterns in FIX_ITEMS.values():
        choices = [index for index, line in enumerate(lines)
                   if index not in used and item_satisfied(line, patterns)]
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
