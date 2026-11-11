"""Reproduction of #1839: the task book of an impl card must carry two hard requirements.

The issue asks for the task book (the text a lane receives for a card) of a *new*
impl card to carry two hard requirements, and only for that class of card:

* ① the delivery note must list the **entry point -> call chain -> data structure
  source** of every new function (who calls it, where the input comes from, who
  fills the field);
* ② at least one **tick/integration level test**: driven from the real entry (the
  tick or an equivalent) to an observable result; a function level test with mocks
  is not accepted.

The template is a constant and it is injected only into impl cards: the upstream /
ops / follow-up cards keep their task book unchanged, and a card that already
carries the block must not get a second copy.

None of that exists on the base commit (a4a49b83): the lane dispatcher writes the
task book as the issue title, the issue body and the shared rules -- there is no
card class, no template and no injection.

The tests below drive the shipped dispatcher (``tools/lane-autodispatch/autodispatch.py``)
against a fake GitHub and a fake lane host: a backlog card is filed by the AD, the
AD queues it and hands it to a lane, and the task file the lane receives is
inspected.

**The tests are red on the base on purpose**: they are the reproduction of #1839.
They turn green once the dispatcher injects the two requirements into the task
book of an impl card (and only there).

Coverage includes the ordinary PLANNER format (no impl marker), individual class
markers and conflicting markers where an excluded class must take precedence.

The requirements are a precondition for the author, not a change of the review
standard: the tests say nothing about the strictness of a review.

Mapping of the acceptance of the issue to the tests
---------------------------------------------------

* "impl 类卡任务书含两项要求" ->
  ``ImplCardTaskBookTests.test_an_impl_card_task_book_lists_the_entry_the_call_chain_and_the_data_source``,
  ``ImplCardTaskBookTests.test_an_impl_card_task_book_demands_a_tick_or_integration_level_test``
* "其他类型不含" ->
  ``ImplCardTaskBookTests.test_the_requirements_are_not_injected_into_the_other_card_classes``
* "不重复注入" ->
  ``ImplCardTaskBookTests.test_the_requirements_are_not_injected_twice``

Run:

    python3 -m unittest tools.tests.test_impl_card_task_book -v
"""

import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
import warnings


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
SPEC = importlib.util.spec_from_file_location("autodispatch_impl_card", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# The dispatcher opens a few files without closing them: the warnings must not
# hide the failures of this file.
warnings.filterwarnings("ignore", category=ResourceWarning)

# The card classes of the issue: only the impl class gets the template.
IMPL = "impl"
OTHER_CLASSES = ("upstream", "ops", "follow-up")

# ① the delivery note must list entry point -> call chain -> data structure source.
REQUIREMENT_ONE_MARKERS = ("入口", "调用链", "数据结构")
# ② at least one tick/integration level test, driven from the real entry.
REQUIREMENT_TWO_MARKERS = (r"(?:tick|集成|integration)", r"(?:测试|test)")


def scratch_dir():
    """A temporary directory inside the worktree (the repo keeps `.tmp/` ignored)."""
    base = ROOT / ".tmp"
    base.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(base))


def has_requirement_one(text):
    """True when the task book asks for the entry point -> call chain -> data source."""
    return all(marker in text for marker in REQUIREMENT_ONE_MARKERS)


def has_requirement_two(text):
    """True when the task book asks for a tick/integration level test."""
    return all(re.search(marker, text, re.I) for marker in REQUIREMENT_TWO_MARKERS)


class FakeGitHub:
    """A `gh` stand-in for the lane dispatcher.

    It answers the issue reads, the issue creation and the label edits, so the
    dispatcher really queues the card it filed.  The card class is carried by the
    title and by the labels of the issue.
    """

    def __init__(self, first_number=1839):
        self.issues = {}
        self.next_number = first_number
        self.created = []
        self.calls = []

    # -- the issue store ----------------------------------------------------------
    def issue(self, number):
        return self.issues.setdefault(number, {"number": number, "title": f"card {number}",
                                               "body": "", "state": "OPEN", "labels": []})

    def rows(self, wanted_label=None):
        rows = []
        for issue in self.issues.values():
            if wanted_label and wanted_label not in issue["labels"]:
                continue
            rows.append({"number": issue["number"], "title": issue["title"], "body": issue["body"],
                         "state": issue["state"], "labels": [{"name": name} for name in issue["labels"]]})
        return rows

    # -- the dispatcher entry points ----------------------------------------------
    def gh(self, args, timeout=60):
        self.calls.append(list(args))
        if args[:2] == ["issue", "create"]:
            title = args[args.index("-t") + 1]
            body = args[args.index("-b") + 1]
            labels = [args[index + 1] for index, part in enumerate(args) if part == "-l"]
            number = self.next_number
            self.next_number += 1
            self.issues[number] = {"number": number, "title": title, "body": body,
                                    "state": "OPEN", "labels": labels}
            self.created.append(self.issues[number])
            return 0, f"https://github.com/surlymochan/phanes/issues/{number}", ""
        if args[:2] == ["issue", "list"]:
            wanted = None
            if "--label" in args:
                wanted = args[args.index("--label") + 1]
            return 0, json.dumps(self.rows(wanted)), ""
        if args[:2] == ["issue", "view"]:
            issue = self.issue(int(args[2]))
            return 0, json.dumps({"state": issue["state"],
                                   "labels": [{"name": name} for name in issue["labels"]]}), ""
        if args[:2] == ["issue", "edit"]:
            number = int(args[2])
            issue = self.issue(number)
            for index, part in enumerate(args):
                if part == "--add-label":
                    issue["labels"].append(args[index + 1])
                elif part == "--remove-label":
                    name = args[index + 1]
                    if name in issue["labels"]:
                        issue["labels"].remove(name)
            return 0, "", ""
        if args[:2] == ["issue", "comment"]:
            return 0, "", ""
        return 0, "", ""


class FakeLaneHost:
    """The ssh side of the dispatcher: the lane takes the card and answers a run id."""

    def __init__(self):
        self.commands = []

    def sh(self, cmd, timeout=120, inp=None):
        argv = cmd if isinstance(cmd, list) else [cmd]
        self.commands.append(argv)
        if any("ls-tree" in part for part in argv):
            return 0, "", ""
        return 0, '{"id": "run-1"}', ""


class ImplCardTaskBookTests(unittest.TestCase):
    """The task book a lane receives for a card, per card class."""

    def setUp(self):
        self.tmp = scratch_dir()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("D", self.tmp.name), ("sh", MODULE.sh), ("gh", MODULE.gh), ("log", MODULE.log),
                            ("preflight_card", lambda text: None),  # the card lint is not under test here
                            ("card_quality", lambda text: None)):  # nor the dispatch card quality
            self.addCleanup(setattr, MODULE, name, getattr(MODULE, name))
            setattr(MODULE, name, value)
        self.github = FakeGitHub()
        self.host = FakeLaneHost()
        self.cards = 0
        MODULE.sh, MODULE.gh = self.host.sh, self.github.gh
        MODULE.log = lambda message, brain=True: None

    # -- the path the AD really walks ---------------------------------------------
    def backlog_card(self, kind, title, body, labels=None):
        """The backlog file of a card: the title, the labels (the class) and the body."""
        if labels is None:
            labels = f"p1,backend:codex,{kind},card:{kind},type:{kind}"
        return f"# {title}\nlabels: {labels}\n{body}\n"

    def task_book(self, kind, title=None, body=None, labels=None):
        """The task file a lane receives for a card of the given class.

        The card is filed by the AD (replenish -> file_issue), the AD queues it
        (queue) and the AD hands it to a lane (dispatch); the task file the
        dispatcher writes is the task book of the card.
        """
        title = title or f"{kind}: 修复一个分支"
        body = body if body is not None else (
            "在解析器里补一个空值保护，调用方只有一处。\n"
            "验收：`python3 -m unittest tools.tests.test_impl_card_task_book`")
        backlog = Path(self.tmp.name) / "backlog"
        backlog.mkdir(parents=True, exist_ok=True)
        self.cards += 1
        (backlog / f"{self.cards:02d}-{kind}.md").write_text(
            self.backlog_card(kind, title, body, labels), encoding="utf-8")

        filed = MODULE.replenish(1, {})
        self.assertEqual(len(filed), 1, "the AD did not file the backlog card")
        number = int(filed[0].lstrip("#"))

        items = MODULE.queue()
        item = next((entry for entry in items if entry["n"] == number), None)
        self.assertIsNotNone(item, "the filed card did not reach the dispatch queue")

        cfg = {"host": "box", "fleet": "/usr/local/bin/swrm-fleet", "repo": "~/work/phanes-fleet"}
        MODULE.dispatch("CX1", cfg, item, "0" * 40)
        task_file = Path(self.tmp.name) / "tasks" / f"CX1-{number}.md"
        self.assertTrue(task_file.is_file(), "the dispatcher wrote no task file for the card")
        return task_file.read_text(encoding="utf-8")

    # -- ① the entry point -> call chain -> data structure source ------------------
    def test_an_impl_card_task_book_lists_the_entry_the_call_chain_and_the_data_source(self):
        task = self.task_book(IMPL)
        missing = [marker for marker in REQUIREMENT_ONE_MARKERS if marker not in task]
        self.assertEqual(missing, [],
                         "the task book of an impl card must ask for the entry point, the call chain "
                         f"and the data structure source (missing: {missing})")

    # -- ② at least one tick/integration level test -------------------------------
    def test_an_impl_card_task_book_demands_a_tick_or_integration_level_test(self):
        task = self.task_book(IMPL)
        self.assertTrue(has_requirement_two(task),
                        "the task book of an impl card must ask for at least one tick/integration "
                        "level test, driven from the real entry to an observable result")

    # -- only the impl class gets the template ------------------------------------
    def test_the_requirements_are_not_injected_into_the_other_card_classes(self):
        for kind in OTHER_CLASSES:
            with self.subTest(card_class=kind):
                task = self.task_book(kind, title=f"{kind}: 同步或跟进一项工作")
                self.assertFalse(has_requirement_one(task),
                                 f"a {kind} card must not get the impl delivery requirements")
                self.assertFalse(has_requirement_two(task),
                                 f"a {kind} card must not get the impl delivery requirements")

    # -- the injection happens once ------------------------------------------------
    def test_planner_format_impl_card_gets_requirements_once(self):
        first = self.task_book(IMPL, title="解析器补充空值保护", labels="p1,backend:codex")
        self.assertEqual(self.github.created[-1]["labels"], ["lane-ready", "p1", "backend:codex"])
        self.assertIn(MODULE.IMPL_CARD_TASK_BOOK, first)
        second = self.task_book(IMPL, title="解析器补充空值保护", body=first,
                                labels="p1,backend:codex")
        self.assertEqual(second.count(MODULE.IMPL_CARD_TASK_BOOK), 1)

    def test_excluded_class_markers_override_impl_markers(self):
        for kind in (*OTHER_CLASSES, "planner", "verify-intent"):
            for prefix in ("", "card:", "type:"):
                with self.subTest(card_class=kind, label_prefix=prefix):
                    body = ("## Verify Intent\n目标 PR：#42\n证据要求：\n- [ ] 检查结果\n"
                            if kind == "verify-intent" else None)
                    task = self.task_book(IMPL, title="impl: 同步或跟进一项工作",
                                          labels=f"p1,backend:codex,impl,card:impl,type:impl,{prefix}{kind}",
                                          body=body)
                    self.assertNotIn(MODULE.IMPL_CARD_TASK_BOOK, task)
                    self.assertFalse(has_requirement_one(task))
                    self.assertFalse(has_requirement_two(task))

    def test_excluded_title_prefix_overrides_impl_label(self):
        for kind in OTHER_CLASSES:
            for separator in (":", "：", " "):
                with self.subTest(card_class=kind, separator=separator):
                    task = self.task_book(IMPL, title=f"{kind}{separator} 同步或跟进一项工作",
                                          labels="p1,backend:codex,impl")
                    self.assertNotIn(MODULE.IMPL_CARD_TASK_BOOK, task)

    def test_real_upstream_label_excludes_an_ordinary_chinese_title(self):
        fixture = json.loads((ROOT / "tools/tests/fixtures/tester-risk-issue-1392.json").read_text())
        upstream_label = next(label["name"] for label in fixture["labels"]
                              if label["name"].startswith("upstream"))
        for impl_label in ("", ",impl"):
            with self.subTest(impl_label=impl_label):
                task = self.task_book("upstream", title="增加远端主机保护",
                                      labels=f"p1,backend:codex,{upstream_label}{impl_label}")
                self.assertFalse(has_requirement_one(task))
                self.assertFalse(has_requirement_two(task))

    def test_a_heading_mention_or_incomplete_template_does_not_skip_requirements(self):
        heading, first, second = MODULE.IMPL_CARD_TASK_BOOK.splitlines()
        for body in ("请处理标题 ## Impl card delivery requirements 的兼容问题",
                     heading, f"{heading}\n{first}", f"{heading}\n{second}"):
            with self.subTest(body=body):
                task = self.task_book(IMPL, body=body)
                self.assertIn(body, task)
                self.assertEqual(task.count(MODULE.IMPL_CARD_TASK_BOOK), 1)
                repeated = self.task_book(IMPL, body=task)
                self.assertEqual(repeated.count(MODULE.IMPL_CARD_TASK_BOOK), 1)

    def test_a_complete_template_without_a_trailing_newline_is_not_injected_twice(self):
        task = self.task_book(IMPL, body=MODULE.IMPL_CARD_TASK_BOOK.rstrip())
        self.assertEqual(task.count("调用链"), 1)
        self.assertEqual(task.count("至少补一个 tick/集成级测试"), 1)

    def test_the_requirements_are_not_injected_twice(self):
        first = self.task_book(IMPL)
        self.assertTrue(has_requirement_one(first),
                        "the task book of an impl card must carry the delivery requirements")
        # the same card content, now carrying the requirements already: a second pass
        # must not add a second copy of the block
        second = self.task_book(IMPL, title="impl: 再走一遍同一张卡", body=first)
        self.assertTrue(has_requirement_one(second),
                        "the task book of an impl card must still carry the delivery requirements")
        self.assertEqual(second.count("调用链"), first.count("调用链"),
                         "the delivery requirements must be injected once, not once per pass")


if __name__ == "__main__":
    unittest.main()
