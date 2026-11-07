"""Acceptance mapping: the PR carries the evidence, the reviewer checks it (Refs #1730).

The gate has two halves:

* the harvest writes an ``验收映射`` section into every PR body it creates, and a PR
  whose section carries no evidence is sent back to its author lane *before* the
  review is dispatched - that send-back is not a review round;
* the review prompt carries the mapping table, the reviewer has to verify every
  acceptance item by number and an item the mapping does not cover is a BLOCKER.

A PR without the section at all is an old PR: it takes the compatibility path and is
only hinted at, never sent back.  "Old" means "created before the mapping gate went
live" - the boundary is the go-live moment of the gate, never a date written into
the code and never something the PR body can decide.
"""
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dispatch_engine_map_test",
                                               os.path.join(HERE, "..", "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

_map_spec = importlib.util.spec_from_file_location(
    "acceptance_map_under_test", os.path.join(HERE, "..", "lib", "review", "acceptance_map.py"))
M = importlib.util.module_from_spec(_map_spec)
_map_spec.loader.exec_module(M)

SHA = "a" * 40
SHA2 = "b" * 40

# The moment the mapping gate went live: the boundary between an old PR and a new one.
GATE_LIVE = "2026-11-03T00:00:00Z"
# The clock of the dispatcher under test: later than the gate went live.
NOW = datetime.datetime(2026, 11, 5, tzinfo=datetime.timezone.utc).timestamp()

CARD = "# 卡：验收映射\nlabels: p1\n\n做一件事。\n\n验收：`python3 -m unittest discover -s tests`\n"
CARD_TWO = "# 卡：两条验收\nlabels: p1\n\n## 验收\n\n- 第一条：边界条件\n- 第二条：文档更新\n"
MAPPING = ("1. `python3 -m unittest discover -s tests` 证据：`tests/test_x.py::test_y` PASS\n")
MAPPING_TWO = "1. 第一条：边界条件 证据：`tests/test_x.py::test_one` PASS\n"


def mapping_body(issue_n, table=""):
    """A PR body as the dispatcher writes it: the mapping section and its marker."""
    content = table or "（未给出：请按卡面验收项逐条编号）"
    return f"Closes #{issue_n}\n\n## 验收映射\n\n{M.MARKER}\n\n{content}\n"


class CardItemTests(unittest.TestCase):
    """The card is the authority: it owns the acceptance items and their numbering."""

    def test_an_acceptance_line_is_one_item(self):
        items = M.card_items(CARD)
        self.assertEqual(items, [{"number": 1, "text": "`python3 -m unittest discover -s tests`"}])

    def test_every_acceptance_line_is_its_own_item(self):
        card = "验收：`a`\n验收：`b`\n"
        self.assertEqual([item["number"] for item in M.card_items(card)], [1, 2])

    def test_an_acceptance_section_carries_the_items(self):
        items = M.card_items(CARD_TWO)
        self.assertEqual([item["text"] for item in items], ["第一条：边界条件", "第二条：文档更新"])
        self.assertEqual([item["number"] for item in items], [1, 2])

    def test_a_card_without_acceptance_has_no_items(self):
        self.assertEqual(M.card_items("# 卡\nlabels: p1\n\n只是描述。\n"), [])

    def test_a_bare_acceptance_prefix_followed_by_a_list_carries_the_items(self):
        """The card of this very issue: `验收：` alone on a line, then the list."""
        card = ("# #1730 [质量门] PR 验收映射表\n\n预算：90\n验收：\n"
                "- 样例 PR 缺映射段被打回且不消耗 review 轮次，有日志证据\n"
                "- 样例 PR 带映射段正常进 review，review 产出引用验收编号\n"
                "- 映射漏项样例被 reviewer 标 BLOCKER\n"
                "- 相关单元测试全绿\n## Findings\n- [BLOCKER] 某某\n")
        items = M.card_items(card)
        self.assertEqual([item["number"] for item in items], [1, 2, 3, 4])
        self.assertEqual(items[0]["text"], "样例 PR 缺映射段被打回且不消耗 review 轮次，有日志证据")
        self.assertEqual(items[3]["text"], "相关单元测试全绿")


class MappingEntryTests(unittest.TestCase):
    """A mapping line is a number, the acceptance item and the evidence that proves it."""

    def test_an_entry_carries_number_item_and_evidence(self):
        entries = M.parse_entries(MAPPING)
        self.assertEqual(entries, [{"number": 1, "text": "`python3 -m unittest discover -s tests`",
                                    "evidence": "`tests/test_x.py::test_y` PASS"}])

    def test_an_entry_without_evidence_carries_none(self):
        entries = M.parse_entries("1. `python3 -m unittest`\n")
        self.assertEqual(entries[0]["evidence"], "")

    def test_the_marker_and_headings_are_not_entries(self):
        self.assertEqual(M.parse_entries("## 验收映射\n\n<!-- lane-acceptance-map -->\n\n"), [])

    def test_the_section_of_a_body_is_found(self):
        body = "Closes #1\n\n## 验收映射\n\n1. x 证据：y\n\n## 别的\n"
        self.assertEqual(M.section(body, "验收映射").strip(), "1. x 证据：y")

    def test_a_body_without_the_section_has_none(self):
        self.assertIsNone(M.section("Closes #1\n\n正文。\n", "验收映射"))


class CoverageTests(unittest.TestCase):
    """The card wins: a mapping that renumbers or rewords the items covers nothing."""

    def test_a_matching_entry_covers_the_item(self):
        items = M.card_items(CARD)
        entries = M.parse_entries(MAPPING)
        self.assertEqual(M.coverage(items, entries), {"covered": [1], "missing": [], "mismatched": []})

    def test_a_renumbered_mapping_leaves_the_card_item_uncovered(self):
        items = M.card_items(CARD_TWO)
        entries = M.parse_entries("2. 第一条：边界条件 证据：`tests/test_x.py::test_one` PASS\n")
        result = M.coverage(items, entries)
        self.assertEqual(result["covered"], [])
        self.assertEqual(result["missing"], [1, 2])

    def test_an_entry_without_evidence_covers_nothing(self):
        items = M.card_items(CARD)
        entries = M.parse_entries("1. `python3 -m unittest discover -s tests`\n")
        self.assertEqual(M.coverage(items, entries)["missing"], [1])

    def test_an_entry_about_something_else_is_a_mismatch(self):
        items = M.card_items(CARD_TWO)
        entries = M.parse_entries("1. 完全无关的东西 证据：`tests/test_x.py::test_one` PASS\n")
        result = M.coverage(items, entries)
        self.assertEqual(result["mismatched"], [1])
        self.assertEqual(result["missing"], [1, 2])


class PromptBlockTests(unittest.TestCase):
    """The prompt carries the items, the mapping and the items the mapping misses."""

    def test_the_block_lists_the_items_the_mapping_and_the_gap(self):
        items = M.card_items(CARD_TWO)
        entries = M.parse_entries(MAPPING_TWO)
        block = M.prompt_block(items, entries, [2])
        self.assertIn("第一条：边界条件", block)
        self.assertIn("tests/test_x.py::test_one", block)
        self.assertIn("A2", block)
        self.assertIn("BLOCKER", block)

    def test_an_old_pr_only_gets_the_hint(self):
        block = M.prompt_block(M.card_items(CARD), [], [], legacy=True)
        self.assertIn("兼容路径", block)
        self.assertNotIn("A1", block)


class FakeRunner:
    """Answers commands by first matching substring rule; records every call."""

    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self.calls = []

    def __call__(self, cmd, timeout=120, inp=None, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(text)
        for needle, result in self.rules:
            if needle in text:
                return result(text) if callable(result) else result
        return 0, "", ""

    def called(self, needle):
        return [c for c in self.calls if needle in c]


def pr(n, issue_n, body=None, head=SHA, labels=()):
    roll = [{"name": "linux-0", "conclusion": "SUCCESS"}]
    return {"number": n, "title": f"pr {n}", "headRefName": f"fleet/M5-issue{issue_n}-r1", "headRefOid": head,
            "isDraft": False, "mergeable": "MERGEABLE", "statusCheckRollup": roll,
            "labels": [{"name": label} for label in labels], "body": body or f"Closes #{issue_n}",
            "createdAt": "2026-11-04T00:00:00Z"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acceptance-map-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        raw = {"repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "local_hosts": ["box"],
               # The gate went live at a known moment: the tests around it do not depend
               # on the wall clock.
               "review": {"acceptance_map": {"enabled_since": GATE_LIVE}}}
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        self.cfg = E.load_config(path)

    def make(self, rules=None, dry_run=False, clock=None):
        self.runner = FakeRunner(rules)
        dispatcher = E.Dispatcher(self.cfg, runner=self.runner, dry_run=dry_run,
                                  clock=clock or (lambda: NOW), out=open(os.devnull, "w"))
        self.addCleanup(dispatcher.out.close)
        return dispatcher

    def real_git(self, *args):
        """A real git command in the local clone: the artifact filter works on it."""
        proc = subprocess.run(["git", "-C", self.tmp, *args], capture_output=True, text=True)
        if proc.returncode:
            raise AssertionError("git %s failed: %s" % (args[0], proc.stderr.strip()))
        return proc.stdout.strip()

    def init_repo(self):
        """Turn the local clone into a real git repo with one base commit."""
        self.real_git("init", "-q")
        self.real_git("config", "user.name", "Test")
        self.real_git("config", "user.email", "test@example.com")
        with open(os.path.join(self.tmp, "base.txt"), "w") as stream:
            stream.write("base\n")
        self.real_git("add", "base.txt")
        self.real_git("commit", "-qm", "base")
        return self.real_git("rev-parse", "HEAD")

    def lane_commit(self, files, message="result"):
        """A commit of the lane branch (refs/fleet/M5)."""
        for path, content in files.items():
            full = os.path.join(self.tmp, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as stream:
                stream.write(content)
        self.real_git("add", "-A", "--", *files.keys())
        self.real_git("commit", "-qm", message)
        sha = self.real_git("rev-parse", "HEAD")
        self.real_git("update-ref", "refs/fleet/M5", sha)
        return sha

    def rules(self, prs, issues=None, reviews=None):
        os.makedirs(self.cfg["state_dir"], exist_ok=True)
        if reviews is not None:
            with open(os.path.join(self.cfg["state_dir"], "reviews.json"), "w") as f:
                json.dump(reviews, f)
        issues = issues or {}

        def view(text):
            number = int(text.split("issue view ")[1].split()[0])
            return 0, json.dumps(issues.get(number, {"state": "OPEN", "body": "orig"})), ""

        return [("pr list", (0, json.dumps(prs), "")), ("issue view", view),
                ("pr diff", (0, "diff --git a/a b/a\n+change", ""))]

    def log_text(self, dispatcher):
        with open(dispatcher.path("dispatch.log"), encoding="utf-8") as f:
            return f.read()


class MappingGateTests(Base):
    """The gate in front of the review dispatch."""

    def test_a_pr_without_the_mapping_section_is_only_hinted_at(self):
        """An old PR has no mapping section: compatibility path, never a send-back."""
        candidate = pr(30, 130)
        candidate["createdAt"] = "2026-11-02T00:00:00Z"
        dispatcher = self.make(self.rules([candidate]))
        self.assertEqual([p["number"] for p in dispatcher.review_candidates({}, {}, [])], [30])
        self.assertEqual(self.runner.called("issue edit 130"), [])
        item = dispatcher.review_item(candidate)
        self.assertIn("兼容路径", item["body"])
        self.assertEqual(item["map_gap"], [])

    def test_missing_section_on_new_pr_is_sent_back_and_failed_write_retries(self):
        candidate = pr(40, 140)
        attempts = []
        def edit(_):
            attempts.append(1)
            return (1, "", "write failed") if len(attempts) == 1 else (0, "", "")
        dispatcher = self.make([("issue edit 140", edit)] + self.rules([candidate]))
        self.assertEqual(dispatcher.review_candidates({}, {}, []), [])
        self.assertNotIn("map_sha", dispatcher.load_reviews().get("40", {}))
        self.assertIn("write failed", self.log_text(dispatcher))
        self.assertEqual(dispatcher.review_candidates({}, {}, []), [])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(dispatcher.load_reviews()["40"]["map_sha"], SHA)
        self.assertNotIn("rounds", dispatcher.load_reviews()["40"])

    def test_colon_list_gap_is_reported(self):
        card = "验收：\n- 第一条验收\n- 第二条验收"
        items = M.card_items(card)
        self.assertEqual([i["text"] for i in items], ["第一条验收", "第二条验收"])
        entries = M.parse_entries("1. 第一条验收 证据：test_one PASS")
        gap = M.coverage(items, entries)["missing"]
        self.assertEqual(gap, [2])
        self.assertIn("BLOCKER", M.prompt_block(items, entries, gap))

    def test_the_gap_of_the_issue_card_is_reported(self):
        """The card of this issue: a mapping that covers one item leaves three open."""
        card = ("# #1730 [质量门]\n\n预算：90\n验收：\n"
                "- 样例 PR 缺映射段被打回且不消耗 review 轮次，有日志证据\n"
                "- 样例 PR 带映射段正常进 review，review 产出引用验收编号\n"
                "- 映射漏项样例被 reviewer 标 BLOCKER\n"
                "- 相关单元测试全绿\n")
        items = M.card_items(card)
        entries = M.parse_entries("1. 样例 PR 缺映射段被打回且不消耗 review 轮次，有日志证据 证据：test_one PASS")
        gap = M.coverage(items, entries)["missing"]
        self.assertEqual(gap, [2, 3, 4])
        block = M.prompt_block(items, entries, gap)
        self.assertIn("A2", block)
        self.assertIn("BLOCKER", block)

    def test_a_pr_without_evidence_is_sent_back_before_the_review(self):
        """The send-back happens before the review and does not consume a review round."""
        candidate = pr(31, 131, body=mapping_body(131))
        dispatcher = self.make(self.rules([candidate], {131: {"state": "OPEN", "body": CARD}}))
        self.assertEqual(dispatcher.review_candidates({}, {}, []), [])
        edit = self.runner.called("issue edit 131")
        self.assertEqual(len(edit), 1)
        self.assertIn("--add-label lane-ready", edit[0])
        self.assertIn("补齐验收映射", edit[0])
        self.assertIn(".lane-acceptance.md", edit[0])
        review = dispatcher.load_reviews().get("31", {})
        self.assertNotIn("rounds", review)              # no review round was consumed
        self.assertEqual(review.get("map_sha"), SHA)
        log = self.log_text(dispatcher)
        self.assertIn("验收映射缺失", log)
        self.assertIn("不计审查轮次", log)

    def test_the_send_back_happens_once_per_head(self):
        candidate = pr(32, 132, body=mapping_body(132))
        dispatcher = self.make(self.rules([candidate], {132: {"state": "OPEN", "body": CARD}}))
        dispatcher.review_candidates({}, {}, [])
        self.runner.calls.clear()
        dispatcher.review_candidates({}, {}, [])
        self.assertEqual(self.runner.called("issue edit 132"), [])

    def test_a_pr_with_the_mapping_goes_to_review_with_the_table(self):
        candidate = pr(33, 133, body=mapping_body(133, MAPPING))
        dispatcher = self.make(self.rules([candidate], {133: {"state": "OPEN", "body": CARD}}))
        self.assertEqual([p["number"] for p in dispatcher.review_candidates({}, {}, [])], [33])
        self.assertEqual(self.runner.called("issue edit 133"), [])
        item = dispatcher.review_item(candidate)
        self.assertIn("`python3 -m unittest discover -s tests`", item["body"])
        self.assertIn("`tests/test_x.py::test_y` PASS", item["body"])
        self.assertIn("A1", item["body"])
        self.assertEqual(item["map_gap"], [])


class MappingReviewTests(Base):
    """The reviewer verifies the mapping and an uncovered item is a BLOCKER."""

    VERDICT = ("VERDICT: CHANGES\nSUMMARY: 验收项 2 没有证据\n"
               "## Findings\n- [A2] 验收映射没有覆盖第二条\n"
               "## Acceptance check\n- A1: OK (evidence: `tests/test_x.py::test_one` PASS)\n"
               "- A2: BLOCKER (the acceptance mapping does not cover this item)\n")

    def review(self, number, issue_n, verdict, gap=None, body=None):
        dispatcher = self.make([("git -C", (0, verdict, "")),
                                (f"pr view {number}", (0, json.dumps(
                                    {"headRefOid": SHA, "state": "OPEN",
                                     "headRefName": f"fleet/M5-issue{issue_n}-r1",
                                     "body": body or f"Closes #{issue_n}"}), ""))])
        state = {"issue": f"PR{number}", "run": "r1", "review_pr": number, "review_sha": SHA}
        if gap is not None:
            state["map_gap"] = gap
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        self.assertTrue(dispatcher.finish_review("M5", {"host": "box", "backend": "pi"}, state, info, {}))
        return dispatcher

    def test_a_mapping_gap_is_named_in_the_review_task(self):
        candidate = pr(34, 134, body=mapping_body(134, MAPPING_TWO))
        candidate["issue"] = 134
        dispatcher = self.make(self.rules([candidate], {134: {"state": "OPEN", "body": CARD_TWO}}))
        item = dispatcher.review_item(candidate)
        self.assertEqual(item["map_gap"], [2])
        self.assertIn("A2", item["body"])
        self.assertIn("BLOCKER", item["body"])

    def test_a_blocker_verdict_is_published_and_counts_as_a_round(self):
        dispatcher = self.review(35, 135, self.VERDICT, gap=[2])
        comment = self.runner.called("pr comment 35")[0]
        self.assertIn("BLOCKER", comment)
        self.assertIn("[A2]", comment)
        review = dispatcher.load_reviews()["35"]
        self.assertEqual(review["verdict"], "CHANGES")
        self.assertEqual(review["rounds"], 1)

    def test_an_approval_over_a_mapping_gap_is_downgraded(self):
        verdict = "VERDICT: APPROVE\nSUMMARY: 看起来没问题\n## Findings\n- [A1] ok\n"
        dispatcher = self.review(36, 136, verdict, gap=[2])
        review = dispatcher.load_reviews()["36"]
        self.assertEqual(review["verdict"], "CHANGES")
        comment = self.runner.called("pr comment 36")[0]
        self.assertIn("BLOCKER", comment)
        self.assertIn("A2", comment)

    def test_an_approval_without_a_gap_stays_an_approval(self):
        verdict = "VERDICT: APPROVE\nSUMMARY: ok\n## Findings\n- [A1] tests/test_x.py::test_y PASS\n"
        dispatcher = self.review(37, 137, verdict, gap=[])
        self.assertEqual(dispatcher.load_reviews()["37"]["verdict"], "APPROVE")


class LegacyBoundaryTests(Base):
    """Old PR and new PR: the boundary is the moment the gate went live.

    The boundary is not a date written into the code - the code that implements the
    gate is newer than any date one could hard-code, so a hard-coded date would send
    back the old PRs the compatibility path exists for.  It is also not something the
    PR body decides: the body is written by the author and can be emptied at any time.
    """

    def test_a_pr_created_before_the_gate_went_live_takes_the_compatibility_path(self):
        candidate = pr(50, 150)
        candidate["createdAt"] = "2026-11-02T00:00:00Z"      # before the gate went live
        dispatcher = self.make(self.rules([candidate]))
        self.assertEqual([p["number"] for p in dispatcher.review_candidates({}, {}, [])], [50])
        self.assertEqual(self.runner.called("issue edit 150"), [])
        item = dispatcher.review_item(candidate)
        self.assertIn("兼容路径", item["body"])
        self.assertEqual(item["map_gap"], [])

    def test_a_pr_created_after_the_gate_went_live_is_sent_back(self):
        candidate = pr(51, 151)                                # created after the gate went live
        dispatcher = self.make(self.rules([candidate]))
        self.assertEqual(dispatcher.review_candidates({}, {}, []), [])
        self.assertEqual(len(self.runner.called("issue edit 151")), 1)
        self.assertNotIn("rounds", dispatcher.load_reviews().get("51", {}))

    def test_a_pr_created_at_the_gate_moment_is_a_new_pr(self):
        candidate = pr(52, 152)
        candidate["createdAt"] = GATE_LIVE
        dispatcher = self.make(self.rules([candidate]))
        self.assertEqual(dispatcher.review_candidates({}, {}, []), [])
        self.assertEqual(len(self.runner.called("issue edit 152")), 1)

    def test_a_pr_created_after_the_fix_but_before_the_gate_went_live_stays_old(self):
        """The old PRs the compatibility path is for: created before the gate was live."""
        self.cfg["review"]["acceptance_map"]["enabled_since"] = "2026-11-05T00:00:00Z"
        candidate = pr(53, 153)
        candidate["createdAt"] = "2026-11-04T12:00:00Z"        # after the fix, before the gate went live
        dispatcher = self.make(self.rules([candidate]))
        self.assertEqual([p["number"] for p in dispatcher.review_candidates({}, {}, [])], [53])
        self.assertEqual(self.runner.called("issue edit 153"), [])

    def test_the_boundary_is_the_go_live_moment_and_not_a_date_in_the_code(self):
        """Without a configured moment the gate records when it went live, once."""
        self.cfg["review"]["acceptance_map"].pop("enabled_since", None)
        dispatcher = self.make(self.rules([]))
        stamp = dispatcher.map_enabled_since()
        self.assertEqual(stamp, "2026-11-05T00:00:00Z")        # the moment the dispatcher ran
        with open(os.path.join(self.cfg["state_dir"], E.MAP_ENABLED_FILE), encoding="utf-8") as stream:
            self.assertEqual(stream.read().strip(), stamp)
        again = self.make(self.rules([]))                      # a restart keeps the same boundary
        self.assertEqual(again.map_enabled_since(), stamp)

    def test_a_new_pr_whose_mapping_section_was_deleted_is_still_sent_back(self):
        """The body cannot decide: a deleted section does not turn a new PR into an old one."""
        candidate = pr(54, 154, body="Closes #154\n\n（正文，映射段被删了）\n")
        dispatcher = self.make(self.rules([candidate]))
        self.assertEqual(dispatcher.review_candidates({}, {}, []), [])
        self.assertEqual(len(self.runner.called("issue edit 154")), 1)


class HarvestTemplateTests(Base):
    """The harvest writes the mapping section into the PR body it creates."""

    def harvest(self, rules, mapping=True):
        # the harvest filters the fetched lane branch (Refs #1835): the local clone is a real repo
        base = self.init_repo()
        files = {"code.py": "print('lane')\n"}
        if mapping:
            files[".lane-acceptance.md"] = MAPPING
        self.lane_commit(files)
        dispatcher = self.make(rules)
        state = {"issue": 140, "run": "r1", "base": base}
        info = {"branch": "lane/x", "commit": "c", "status": "done", "repo": "/w", "summary": "done"}
        self.assertTrue(dispatcher.finish("M5", {"host": "box", "backend": "pi"}, state, info, state))
        return dispatcher

    def test_the_pr_body_carries_the_mapping_of_the_lane(self):
        dispatcher = self.harvest([
            ("issue view", (0, json.dumps({"state": "OPEN", "labels": [], "body": CARD}), "")),
            (".lane-acceptance.md", (0, MAPPING, "")),
            ("diff --numstat", (0, "40\t5\topenswarm/lib/dispatch/engine.py\n", "")),
        ])
        created = self.runner.called("pr create")
        self.assertEqual(len(created), 1)
        self.assertIn("## 验收映射", created[0])
        self.assertIn(M.MARKER, created[0])
        self.assertIn("`tests/test_x.py::test_y` PASS", created[0])

    def test_a_pr_without_a_mapping_file_still_carries_the_section(self):
        dispatcher = self.harvest([
            ("issue view", (0, json.dumps({"state": "OPEN", "labels": [], "body": CARD}), "")),
            ("diff --numstat", (0, "40\t5\topenswarm/lib/dispatch/engine.py\n", "")),
        ], mapping=False)
        created = self.runner.called("pr create")
        self.assertIn("## 验收映射", created[0])
        self.assertIn("未给出", created[0])

    def test_the_task_asks_the_lane_for_the_mapping(self):
        dispatcher = self.make()
        text = dispatcher.task_text({"n": 141, "title": "卡", "body": CARD})
        self.assertIn("验收映射", text)
        self.assertIn(".lane-acceptance.md", text)
        self.assertIn("1. `python3 -m unittest discover -s tests`", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
