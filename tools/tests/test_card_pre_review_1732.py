from _test_tmp import short_tmp_root

#!/usr/bin/env python3
"""Reproduction of #1732: the card pre-review standard of the casting reform.

Issue #1732 raises the pre-review standard of the cards that the control plane
casts and refills:

* (a) every acceptance item of a card must be **measurable** - otherwise the
  pre-review rejects the card and names the item that cannot be judged;
* (a) every card must carry a **失败模式清单** section, otherwise the pre-review
  rejects the card;
* (b) a **defect pattern library** (`docs/defect-patterns.md`) is written down,
  and the casting template and the production prompt carry the reference to it;
* (c) the watchdog refill (`replenish()`) runs the new pre-review, so a card
  that does not pass it is never filed.

What is red on the base commit (5af78a66a)
-------------------------------------------

* the refill path of the dispatcher has no pre-review at all: every backlog
  card is filed, however its acceptance reads
  (`tools/lane-autodispatch/autodispatch.py`, `replenish()`);
* the casting template writes no `labels:` line, no `验收：` line and no
  `失败模式清单` section (`tools/mkcard.py`);
* the production prompt of the planner asks for neither the failure mode list
  nor the defect pattern library (`PLANNER` in the dispatcher);
* the defect pattern library itself does not exist.

The tests below are the reproduction only: the implementation is not touched
here. They turn green once the pre-review standard of #1732 lands.

Where the fix belongs
---------------------

The batch of #1732 named three files. The old ``openswarm/bin/openswarm``
(the ``card-lint`` sub-command) was deleted by #1894, so the pre-review script
of the old batch has **no counterpart in ``swrm/``** any more - the paths
checked are ``swrm/bin/swrm``, ``swrm/bin/swrm-auto``, ``swrm/tests/`` and
``swrm/docs/``: none of them knows a card, a ``card-lint`` command or a
``失败模式清单`` section. What survived the migration on the Phanes side is the
control plane of the repo, so the reproduction pins the behaviour at the
surviving entry points:

* ``tools/lane-autodispatch/autodispatch.py`` - the migrated counterpart of the
  old ``openswarm/bin/openswarm`` for everything the card pipeline does
  (``replenish()``, ``file_issue()`` and the ``PLANNER`` production prompt);
* ``tools/mkcard.py`` - the casting template of the batch, unchanged by #1894.

Mapping of the acceptance of the issue to the tests
---------------------------------------------------

* "样例卡缺可测验收被预审驳回并指明条目" ->
  ``CardPreReviewRefillTests.test_a_card_with_an_unmeasurable_acceptance_is_rejected_and_the_item_is_named``,
  ``CardPreReviewRefillTests.test_a_vague_item_next_to_a_command_is_rejected_and_the_item_is_named``
* "样例卡缺失败模式清单被驳回" ->
  ``CardPreReviewRefillTests.test_a_card_without_the_failure_mode_section_is_rejected``
* "看门狗自动补池同步执行新预审标准" ->
  ``TickRefillTests.test_the_tick_refills_the_queue_only_with_the_cards_the_pre_review_accepts``,
  ``RepoBacklogRefillTests.test_a_repo_backlog_card_that_fails_the_pre_review_is_not_filed``,
  ``PlannerPromptTests.test_the_production_prompt_asks_for_the_failure_mode_section``,
  ``PlannerPromptTests.test_the_production_prompt_names_the_defect_pattern_library``
* "新铸卡正文自动含缺陷模式清单引用（抽查 3 张）" ->
  ``CardTemplateTests.test_a_generated_card_declares_the_labels_and_the_acceptance``,
  ``CardTemplateTests.test_a_generated_card_carries_the_failure_mode_section``,
  ``CardTemplateTests.test_three_generated_cards_carry_the_defect_pattern_reference``
* "预审脚本单元测试全绿" ->
  ``CardPreReviewRefillTests.test_a_card_with_a_measurable_acceptance_is_filed``,
  ``CardTemplateTests.test_a_generated_card_passes_the_pre_review``,
  ``DefectPatternLibraryTests.test_the_defect_pattern_library_lists_the_first_batch``

Run it with::

    python3 -m unittest tools.tests.test_card_pre_review_1732 -v
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import warnings
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
AD_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
MKCARD_PATH = ROOT / "tools" / "mkcard.py"
FIXTURES = ROOT / "tools" / "tests" / "fixtures"
GOOD_CARD = FIXTURES / "card_pre_review_good.md"
DEFECT_LIBRARY = ROOT / "docs" / "defect-patterns.md"

SPEC = importlib.util.spec_from_file_location("autodispatch_card_pre_review", AD_PATH)
AD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AD)

# The dispatcher opens a few files without closing them: the warnings must not
# hide the failures of this file.
warnings.filterwarnings("ignore", category=ResourceWarning)

# The six patterns of the first batch of the library (2026-11-02, measured).
FIRST_BATCH_PATTERNS = ("测错对象", "报全局", "状态读写结构不一致", "dry-run", "配置优先级", "清理")

# The findings of the audit the casting template turns into a card: they make
# the generated card thick enough for a pre-review that judges the promised
# size of a change.
AUDIT_FINDINGS = (
    "在 `examples/org-health-local/test_run_e2e.py` 补一条 schema 缺失的单测。",
    "失败提示里要写清缺的是哪个 schema 文件。",
    "失败提示里要写清调用方下一步该做什么。",
    "失败路径上退出码必须非零。",
    "失败路径上不能留下半写的产物目录。",
    "schema 目录整体缺失的路径也要覆盖。",
    "schema 文件存在但不是 JSON 的路径也要覆盖。",
    "临时目录在失败路径上也要清理。",
    "traceback 不能比提示信息更早被看到。",
    "退出码转换时不能丢掉原始错误信息。",
    "写产物之前要检查输出目录是否可写。",
    "输出目录不可写时提示要可读。",
    "清理顺序不能被 smoke 测试依赖。",
    "测试收集要区分失败与错误。",
    "汇总行数要含被跳过的用例。",
    "没有测试文件时不能报成功。",
    "提示信息要含 schema 的名字。",
    "提示信息要含可执行的下一步。",
    "空输入与缺字段是两条不同的路径。",
    "嵌套对象里缺字段也要覆盖。",
    "默认值与配置文件默认值要一致。",
    "环境变量缺失与空字符串是两条路径。",
    "缓存命中与未命中都要覆盖。",
    "缓存里的值与解析结果要一致。",
    "写入后再读取的往返要覆盖。",
    "用户改过默认值之后的读取要覆盖。",
    "配置文件被删掉之后的读取要覆盖。",
    "并发读取同一个配置文件要覆盖。",
    "失败后残留的产物目录要能被断言。",
    "提示信息不能只打印路径。",
    "每个发现都要在当前代码上确认过。",
)


def scratch_root():
    """An owned scratch root inside the worktree."""
    base = Path(short_tmp_root())
    base.mkdir(exist_ok=True)
    return base


def good_card():
    """A card that passes the pre-review: title, labels, body, a measurable
    acceptance and the failure mode section."""
    return GOOD_CARD.read_text(encoding="utf-8")


def with_acceptance(text, acceptance):
    """The card with another acceptance line."""
    lines = []
    for line in text.splitlines():
        lines.append(acceptance if line.startswith("验收：") else line)
    return "\n".join(lines) + "\n"


def without_failure_modes(text):
    """The card without its 失败模式清单 section."""
    marker = "## 失败模式清单"
    index = text.find(marker)
    if index < 0:
        raise AssertionError("the fixture card has no 失败模式清单 section")
    return text[:index]


class FakeGitHub:
    """A `gh` stand-in: it files the issues and answers the planner queries."""

    def __init__(self, created, first_number=1732):
        self.created = created
        self.next_number = first_number

    def gh(self, args, timeout=60):
        if args[:2] == ["issue", "create"]:
            title = args[args.index("-t") + 1]
            body = args[args.index("-b") + 1]
            labels = [args[index + 1] for index, part in enumerate(args) if part == "-l"]
            number = self.next_number
            self.next_number += 1
            self.created.append({"number": number, "title": title, "body": body, "labels": labels})
            return 0, f"https://github.com/surlymochan/phanes/issues/{number}", ""
        if args[:2] == ["issue", "list"]:
            # a planner issue is open: the refill must not mint a second one
            return 0, json.dumps([{"number": 42, "title": "planner issue"}]), ""
        return 0, "", ""


class DispatcherHarness:
    """The refill path of the dispatcher, driven offline.

    No `gh`, no `git`, no SSH and no network: the GitHub calls are answered by
    the fake above, the git calls of the repo backlog are answered from a
    dictionary, every other command returns nothing. The log of the dispatcher
    is read back, because the rejection of a card has to reach the operator.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.created = []
        self.repo_backlog = {}
        self.github = FakeGitHub(self.created)

    def gh(self, args, timeout=60):
        return self.github.gh(args, timeout)

    def sh(self, cmd, timeout=120, inp=None):
        argv = cmd if isinstance(cmd, list) else [cmd]
        if "ls-tree" in argv:
            return 0, "".join(f"{path}\n" for path in self.repo_backlog), ""
        if "show" in argv:
            path = argv[-1].split(":", 1)[-1]
            return 0, self.repo_backlog.get(path, ""), ""
        return 0, "", ""

    def evidence(self, *extra):
        """Everything the dispatcher said about the cards it refused."""
        log_file = self.root / "autodispatch.log"
        logged = log_file.read_text(encoding="utf-8") if log_file.is_file() else ""
        return "\n".join([logged, *extra])

    def capture(self, call, *args):
        """Run a dispatcher entry point and collect what it said."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = call(*args)
        return result, self.evidence(stdout.getvalue(), stderr.getvalue())

    def refill(self, cards=(), k=1):
        """Put the cards into the local backlog and run one refill.

        Returns the filed issue numbers and everything the dispatcher said.
        """
        backlog = self.root / "backlog"
        backlog.mkdir(parents=True, exist_ok=True)
        for name, text in cards:
            (backlog / name).write_text(text, encoding="utf-8")
        return self.capture(AD.replenish, k, {})

    def tick(self, lanes=None, state=None):
        """Run one tick of the dispatcher (the watchdog loop of the box)."""
        AD.queue = lambda *args, **kwargs: []
        return self.capture(AD.tick, lanes if lanes is not None else {}, state if state is not None else {}, 0)


class DispatcherTestCase(unittest.TestCase):
    """The base of the tests: the dispatcher runs against a temporary home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(scratch_root()))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.harness = DispatcherHarness(self.root)
        for name, value in (("D", str(self.root)),
                            ("BRAIN_LOG", str(self.root / "brain.log")),
                            ("HOME", str(self.home)),
                            ("gh", self.harness.gh),
                            ("sh", self.harness.sh)):
            self.addCleanup(setattr, AD, name, getattr(AD, name))
            setattr(AD, name, value)
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                                   "OPENSWARM_HOME": str(self.root / "swrm-home"),
                                                   "OPENSWARM_CTL_STATE_DIR": str(self.root / "ctl-state")})
        environment.start()
        self.addCleanup(environment.stop)


class CardPreReviewRefillTests(DispatcherTestCase):
    """(a) the pre-review of the refill path: measurable acceptance, failure modes."""

    def test_a_card_with_an_unmeasurable_acceptance_is_rejected_and_the_item_is_named(self):
        card = with_acceptance(good_card(), "验收：功能正常")
        filed, evidence = self.harness.refill([("01-vague.md", card)])
        self.assertEqual(filed, [], "the refill filed a card whose acceptance cannot be measured")
        self.assertIn("功能正常", evidence,
                      "the rejection must name the acceptance item that cannot be judged")

    def test_a_vague_item_next_to_a_command_is_rejected_and_the_item_is_named(self):
        card = with_acceptance(good_card(), "验收：`pytest`\n- `功能正常`")
        filed, evidence = self.harness.refill([("01-mixed.md", card)])
        self.assertEqual(filed, [], "the refill filed a card with an acceptance item that cannot be measured")
        self.assertIn("功能正常", evidence,
                      "the rejection must name the acceptance item that cannot be judged")

    def test_a_card_without_the_failure_mode_section_is_rejected(self):
        card = without_failure_modes(good_card())
        filed, evidence = self.harness.refill([("01-no-modes.md", card)])
        self.assertEqual(filed, [], "the refill filed a card without a 失败模式清单 section")
        self.assertIn("失败模式", evidence, "the rejection must name the missing 失败模式清单 section")

    def test_a_card_with_a_measurable_acceptance_is_filed(self):
        """The control of the three tests above: the pre-review must not reject
        a card that carries a measurable acceptance and the failure modes."""
        filed, evidence = self.harness.refill([("01-good.md", good_card())])
        self.assertEqual(len(filed), 1, f"the refill did not file the valid card: {evidence}")
        self.assertEqual(len(self.harness.created), 1)
        self.assertIn("组织诊断 Builder", self.harness.created[0]["title"])


class RepoBacklogRefillTests(DispatcherTestCase):
    """The second source of cards: the backlog the planner wrote into the repo."""

    def test_a_repo_backlog_card_that_fails_the_pre_review_is_not_filed(self):
        path = "docs/backlog/next/20261103-1732-01.md"
        self.harness.repo_backlog[path] = with_acceptance(good_card(), "验收：功能正常")
        filed, evidence = self.harness.refill()
        self.assertEqual(filed, [], "the refill filed a card whose acceptance cannot be measured")
        self.assertIn("功能正常", evidence,
                      "the rejection must name the acceptance item that cannot be judged")

    def test_a_repo_backlog_card_that_passes_the_pre_review_is_filed(self):
        path = "docs/backlog/next/20261103-1732-02.md"
        self.harness.repo_backlog[path] = good_card()
        filed, evidence = self.harness.refill()
        self.assertEqual(len(filed), 1, f"the refill did not file the valid card: {evidence}")
        self.assertIn("组织诊断 Builder", self.harness.created[0]["title"])


class TickRefillTests(DispatcherTestCase):
    """(c) the watchdog refill runs the pre-review: the tick level evidence."""

    def test_the_tick_refills_the_queue_only_with_the_cards_the_pre_review_accepts(self):
        good = good_card()
        vague = with_acceptance(good_card(), "验收：功能正常")
        backlog = self.root / "backlog"
        backlog.mkdir(parents=True, exist_ok=True)
        (backlog / "01-good.md").write_text(good, encoding="utf-8")
        (backlog / "02-vague.md").write_text(vague, encoding="utf-8")
        _, evidence = self.harness.tick()
        titles = [issue["title"] for issue in self.harness.created]
        self.assertEqual(len(titles), 1, f"the tick filed {titles}: only the valid card may be filed")
        self.assertIn("组织诊断 Builder", titles[0])
        self.assertIn("功能正常", evidence,
                      "the tick must report the acceptance item that cannot be measured")


class PlannerPromptTests(unittest.TestCase):
    """(b) the production prompt of the planner carries the failure mode list."""

    def test_the_production_prompt_asks_for_the_failure_mode_section(self):
        self.assertIn("失败模式清单", AD.PLANNER,
                      "the planner prompt must ask for a 失败模式清单 section in every new card")

    def test_the_production_prompt_names_the_defect_pattern_library(self):
        self.assertIn("docs/defect-patterns.md", AD.PLANNER,
                      "the planner prompt must point at the defect pattern library")


class CardTemplateTests(DispatcherTestCase):
    """(b) the casting template writes the pre-review format of a card."""

    def build_card(self, checks, title="铸卡样例", scope="只改测试文件，不改实现。"):
        """Cast one card with `tools/mkcard.py` and return its text."""
        audit = self.root / "audit.md"
        audit.write_text("\n".join(["## Findings", *AUDIT_FINDINGS, "## End"]) + "\n", encoding="utf-8")
        out = self.root / "card.md"
        result = subprocess.run([sys.executable, str(MKCARD_PATH), str(out), title, str(audit),
                                 "## Findings", "## End", scope, checks],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"mkcard.py failed: {result.stderr}")
        return out.read_text(encoding="utf-8")

    def test_a_generated_card_declares_the_labels_and_the_acceptance(self):
        card = self.build_card("python3 examples/org-health-local/test_run_e2e.py")
        self.assertTrue(any(line.startswith("labels:") for line in card.splitlines()),
                        "a cast card must declare its labels: the pre-review routes it by them")
        self.assertTrue(any(line.startswith("验收：") for line in card.splitlines()),
                        "a cast card must carry an 验收 line: the pre-review reads the acceptance from it")
        self.assertIn("python3 examples/org-health-local/test_run_e2e.py", card)

    def test_a_generated_card_carries_the_failure_mode_section(self):
        card = self.build_card("python3 examples/org-health-local/test_run_e2e.py")
        self.assertIn("## 失败模式清单", card, "a cast card must carry the 失败模式清单 section")
        self.assertIn("docs/defect-patterns.md", card,
                      "a cast card must point at the defect pattern library")

    def test_three_generated_cards_carry_the_defect_pattern_reference(self):
        """The spot check of the issue: three cast cards, three references."""
        checks = ("python3 examples/org-health-local/test_run_e2e.py",
                  "ls docs/backlog/next/*.md | wc -l | grep -qx 20",
                  "python3 tools/check-ad-ready-target.py")
        for index, command in enumerate(checks, start=1):
            with self.subTest(card=index):
                card = self.build_card(command, title=f"铸卡样例 {index}")
                self.assertIn("## 失败模式清单", card, "a cast card must carry the 失败模式清单 section")
                self.assertIn("docs/defect-patterns.md", card,
                              "a cast card must point at the defect pattern library")

    def test_a_generated_card_passes_the_pre_review(self):
        """The card the template casts must pass the pre-review of the refill
        path (red on the base: the cast card carries no `labels:` line, so the
        refill cannot even file it)."""
        card = self.build_card("python3 examples/org-health-local/test_run_e2e.py")
        filed, evidence = self.harness.refill([("01-cast.md", card)])
        self.assertEqual(len(filed), 1, f"the pre-review refused a cast card: {evidence}")


class DefectPatternLibraryTests(unittest.TestCase):
    """(b) the defect pattern library itself."""

    def test_the_defect_pattern_library_lists_the_first_batch(self):
        self.assertTrue(DEFECT_LIBRARY.is_file(),
                        f"the defect pattern library {DEFECT_LIBRARY.name} does not exist")
        text = DEFECT_LIBRARY.read_text(encoding="utf-8")
        missing = [pattern for pattern in FIRST_BATCH_PATTERNS if pattern not in text]
        self.assertEqual(missing, [],
                         f"the defect pattern library does not record the first batch (missing: {missing})")


if __name__ == "__main__":
    unittest.main()
