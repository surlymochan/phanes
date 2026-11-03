"""The needs-human backlog: a human-facing summary of what waits for a person (Refs #1695).

A PR whose review rounds are exhausted carries the ``needs-human`` label and waits for a
human, but nothing told the human about it.  The dispatcher now reports the backlog: every
labelled issue and PR with its number, title, review round, last verdict summary, waiting
time and a recommended action, sorted by the waiting time.

The tests replay the PR #1653 sample (issue #1362, four CHANGES rounds, escalated to
needs-human) and assert the summary names it, counts the rounds, measures the wait and
picks an action.  They also assert that an empty backlog is reported as an empty summary
without an error, that the same entry is not written to the brain log again on every tick
and that a backlog longer than one page is read completely.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location("dispatch_needs_human_test",
                                                os.path.join(HERE, "..", "lib", "dispatch", "needs_human.py"))
NH = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(NH)

_espec = importlib.util.spec_from_file_location("dispatch_engine_needs_human_test",
                                                 os.path.join(HERE, "..", "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(_espec)
_espec.loader.exec_module(E)

NOW = 1700000000.0
REPO = "acme/project"


def stamp(epoch):
    """A local ``%F %T`` stamp, the shape the dispatcher writes its review records in."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def gh_json(rows):
    return json.dumps(rows)


def issue_row(number, title, pull=False):
    """One row of the REST list endpoint (snake_case, PRs carry a pull_request link)."""
    row = {"number": number, "title": title,
           "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-02T00:00:00Z"}
    if pull:
        row["pull_request"] = {"url": "https://api.github.com/repos/%s/pulls/%d" % (REPO, number)}
    return row


class ActionTests(unittest.TestCase):
    """The recommended action follows the verdict text first, the round count second."""

    def test_missing_information_waits_for_the_maintainer(self):
        action, reason = NH.classify_action("缺少回归用例说明", 4)
        self.assertEqual(action, NH.ACTION_MAINTAINER)
        self.assertIn("缺少", reason)

    def test_a_decision_waits_for_the_user(self):
        action, _ = NH.classify_action("请确认采用哪种存储方案", 1)
        self.assertEqual(action, NH.ACTION_USER)

    def test_a_change_that_is_too_big_can_be_split(self):
        action, _ = NH.classify_action("改动太大，建议拆成两个 PR", 2)
        self.assertEqual(action, NH.ACTION_SPLIT)

    def test_without_a_verdict_text_the_rounds_decide(self):
        self.assertEqual(NH.classify_action("", 4)[0], NH.ACTION_SPLIT)
        self.assertEqual(NH.classify_action("", 1)[0], NH.ACTION_USER)
        self.assertEqual(NH.classify_action("", 0)[0], NH.ACTION_MANUAL)

    def test_a_verdict_without_a_direction_needs_a_human(self):
        action, reason = NH.classify_action("整体实现可以再打磨", 1)
        self.assertEqual(action, NH.ACTION_MANUAL)
        self.assertIn("未指明", reason)

    def test_repeated_rounds_point_at_a_work_item_that_is_too_big(self):
        action, reason = NH.classify_action("整体实现可以再打磨", 3)
        self.assertEqual(action, NH.ACTION_SPLIT)
        self.assertIn("3", reason)


class SummaryTests(unittest.TestCase):
    """The summary lists every entry and sorts it by the waiting time."""

    def items(self):
        return [
            {"kind": NH.KIND_PR, "number": 1653, "title": "审查轮次耗尽", "rounds": 4,
             "verdict": "CHANGES", "summary": "缺少回归用例说明", "text": "缺少回归用例说明",
             "since": NOW - 26 * 3600},
            {"kind": NH.KIND_ISSUE, "number": 1702, "title": "外部车道放弃", "rounds": 0,
             "verdict": "", "summary": "", "text": "", "since": NOW - 2 * 3600},
        ]

    def test_entries_carry_number_rounds_wait_and_action(self):
        entries = NH.summarize(self.items(), now=NOW)
        first = entries[0]
        self.assertEqual(first["number"], 1653)
        self.assertEqual(first["rounds"], 4)
        self.assertEqual(first["waiting_hours"], 26.0)
        self.assertEqual(first["action"], NH.ACTION_MAINTAINER)
        self.assertEqual(first["kind"], NH.KIND_PR)

    def test_the_longest_wait_comes_first(self):
        entries = NH.summarize(self.items(), now=NOW)
        self.assertEqual([entry["number"] for entry in entries], [1653, 1702])
        self.assertEqual(entries[1]["action"], NH.ACTION_MANUAL)

    def test_an_empty_backlog_renders_an_empty_summary(self):
        self.assertEqual(NH.summarize([], now=NOW), [])
        self.assertIn("无积压", NH.render([]))
        self.assertIn("无积压", NH.render_line([]))

    def test_the_rendered_summary_names_every_field(self):
        text = NH.render(NH.summarize(self.items(), now=NOW))
        self.assertIn("#1653", text)
        self.assertIn("轮次 4", text)
        self.assertIn("26.0h", text)
        self.assertIn("缺少回归用例说明", text)
        self.assertIn(NH.ACTION_MAINTAINER, text)

    def test_the_one_line_summary_names_every_entry(self):
        line = NH.render_line(NH.summarize(self.items(), now=NOW))
        self.assertIn("#1653", line)
        self.assertIn("#1702", line)
        self.assertNotIn("\n", line)

    def test_the_one_line_summary_carries_the_title_and_the_verdict_summary(self):
        line = NH.render_line(NH.summarize(self.items(), now=NOW))
        self.assertIn("审查轮次耗尽", line)
        self.assertIn("缺少回归用例说明", line)

    def test_the_one_line_summary_never_breaks_its_line(self):
        entries = NH.summarize([{"kind": NH.KIND_PR, "number": 1653, "rounds": 4,
                                 "title": "两个\n行", "summary": "第一行\n第二行",
                                 "text": "", "since": NOW}], now=NOW)
        line = NH.render_line(entries)
        self.assertNotIn("\n", line)
        self.assertIn("两个 行", line)


class DueTests(unittest.TestCase):
    """A new entry is reported once; a waiting one is repeated at most once a day."""

    def entries(self, waiting_h):
        return [{"kind": NH.KIND_PR, "number": 1653, "rounds": 4, "waiting_hours": waiting_h,
                 "action": NH.ACTION_MANUAL, "title": "t", "summary": "", "verdict": ""}]

    def test_a_new_entry_is_due(self):
        due, state = NH.due(self.entries(1.0), {"items": {}}, now=NOW)
        self.assertEqual(len(due), 1)
        self.assertIn("pr:1653", state)

    def test_the_same_entry_is_not_reported_again(self):
        _, state = NH.due(self.entries(1.0), {"items": {}}, now=NOW)
        due, state = NH.due(self.entries(1.0), {"items": state}, now=NOW + 60)
        self.assertEqual(due, [])

    def test_a_waiting_entry_is_reported_again_after_a_day(self):
        _, state = NH.due(self.entries(26.0), {"items": {}}, now=NOW)
        due, _ = NH.due(self.entries(26.0), {"items": state}, now=NOW + 25 * 3600)
        self.assertEqual(len(due), 1)

    def test_a_fresh_entry_is_not_repeated_only_because_it_waits(self):
        _, state = NH.due(self.entries(26.0), {"items": {}}, now=NOW)
        due, _ = NH.due(self.entries(26.0), {"items": state}, now=NOW + 3600)
        self.assertEqual(due, [])

    def test_an_entry_that_left_the_backlog_is_dropped(self):
        _, state = NH.due(self.entries(1.0), {"items": {}}, now=NOW)
        _, state = NH.due([], {"items": state}, now=NOW)
        self.assertEqual(state, {})

    def test_the_state_of_an_entry_that_is_not_due_is_kept(self):
        _, state = NH.due(self.entries(1.0), {"items": {}}, now=NOW)
        due, state = NH.due(self.entries(1.0), {"items": state}, now=NOW + 60)
        self.assertEqual(due, [])
        self.assertIn("pr:1653", state)
        self.assertEqual(state["pr:1653"]["reported_at"], NOW)

    def test_an_entry_is_not_reported_again_in_the_third_round(self):
        """The state survives the rounds: a backlog that does not move stays quiet."""
        _, state = NH.due(self.entries(1.0), {"items": {}}, now=NOW)
        for round_number in range(3):
            due, state = NH.due(self.entries(1.0), {"items": state}, now=NOW + round_number * 60)
            self.assertEqual(due, [])

    def test_an_entry_that_is_reported_again_is_not_reported_twice(self):
        _, state = NH.due(self.entries(26.0), {"items": {}}, now=NOW)
        due, state = NH.due(self.entries(50.0), {"items": state}, now=NOW + 24 * 3600)
        self.assertEqual(len(due), 1)
        due, state = NH.due(self.entries(50.0), {"items": state}, now=NOW + 24 * 3600 + 60)
        self.assertEqual(due, [])


class CollectTests(unittest.TestCase):
    """The collection reads GitHub and never lets a failing call break the summary."""

    def runner(self, rules):
        def run(argv):
            text = " ".join(str(part) for part in argv)
            for needle, result in rules.items():
                if needle in text:
                    return result
            return 0, "", ""
        return run

    def test_issues_and_prs_are_collected(self):
        runner = self.runner({
            "api repos/%s/issues" % REPO: (0, gh_json([issue_row(1702, "外部车道放弃"),
                                                         issue_row(1653, "审查轮次耗尽", pull=True)]), ""),
            "pr view 1653": (0, gh_json({"comments": [{"body": "VERDICT: CHANGES"}]}), ""),
        })
        items = NH.collect(runner, REPO, reviews={"1653": {"rounds": 4, "verdict": "CHANGES",
                                                            "summary": "缺少回归用例说明",
                                                            "at": "2026-11-02 00:00:00"}})
        self.assertEqual(sorted(item["number"] for item in items), [1653, 1702])
        pr = [item for item in items if item["number"] == 1653][0]
        self.assertEqual(pr["kind"], NH.KIND_PR)
        self.assertEqual(pr["rounds"], 4)
        self.assertEqual(pr["summary"], "缺少回归用例说明")
        issue = [item for item in items if item["number"] == 1702][0]
        self.assertEqual(issue["kind"], NH.KIND_ISSUE)

    def test_a_failing_call_contributes_no_items_and_raises_nothing(self):
        runner = self.runner({"api ": (1, "", "boom")})
        self.assertEqual(NH.collect(runner, REPO), [])

    def test_a_broken_answer_contributes_no_items(self):
        runner = self.runner({"api ": (0, "not json", "")})
        self.assertEqual(NH.collect(runner, REPO), [])

    def test_a_pr_without_a_review_record_is_read_from_its_last_review_comment(self):
        body = ("**Lane review — ❌ CHANGES REQUESTED** by M1\n\nVERDICT: CHANGES\n"
                "SUMMARY: 缺少回归用例说明\n\n<!-- lane-review pr=1653 sha=abc verdict=CHANGES -->")
        runner = self.runner({
            "api repos/%s/issues" % REPO: (0, gh_json([issue_row(1653, "审查轮次耗尽", pull=True)]), ""),
            "pr view 1653": (0, gh_json({"comments": [{"body": body}]}), ""),
        })
        items = NH.collect(runner, REPO)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["verdict"], "CHANGES")
        self.assertEqual(items[0]["summary"], "缺少回归用例说明")
        self.assertIn("VERDICT: CHANGES", items[0]["text"])

    def test_a_backlog_longer_than_one_page_is_read_completely(self):
        first = [issue_row(1000 + number, "issue %d" % number) for number in range(NH.PAGE_SIZE)]
        second = [issue_row(2000 + number, "issue %d" % number) for number in range(3)]

        def run(argv):
            page = " ".join(str(part) for part in argv).rsplit("page=", 1)[-1]
            if page == "1":
                return 0, gh_json(first), ""
            if page == "2":
                return 0, gh_json(second), ""
            return 0, gh_json([]), ""

        items = NH.collect(run, REPO)
        self.assertEqual(len(items), NH.PAGE_SIZE + 3)
        self.assertEqual(items[-1]["number"], 2002)

    def test_the_pages_stop_when_a_page_is_empty(self):
        runner = self.runner({
            "page=1": (0, gh_json([issue_row(1, "one")]), ""),
            "page=2": (0, gh_json([]), ""),
        })
        self.assertEqual(len(NH.collect(runner, REPO)), 1)


class DispatcherReportTests(unittest.TestCase):
    """The dispatcher reports the backlog into its status output and into the brain log."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-needs-human-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.brain = os.path.join(self.tmp, "brain.md")
        raw = {"repo": REPO, "local_repo": self.tmp,
               "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")},
               "local_hosts": ["box"], "brain_log": self.brain}
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        self.cfg = E.load_config(path)

    def make(self, rules, clock=None):
        self.calls = []

        def runner(cmd, **kwargs):
            text = " ".join(str(part) for part in cmd)
            self.calls.append(text)
            for needle, result in rules.items():
                if needle in text:
                    return result
            return 0, "", ""

        d = E.Dispatcher(self.cfg, runner=runner, out=open(os.devnull, "w"), clock=clock or (lambda: NOW))
        self.addCleanup(d.out.close)
        return d

    def pr_rules(self, number=1653, title="审查轮次耗尽"):
        body = ("**Lane review — ❌ CHANGES REQUESTED** by M1\n\nVERDICT: CHANGES\n"
                "SUMMARY: 缺少回归用例说明\n\n<!-- lane-review pr=%d sha=abc verdict=CHANGES -->" % number)
        return {
            "api repos/%s/issues" % REPO: (0, gh_json([issue_row(number, title, pull=True)]), ""),
            "pr view %d" % number: (0, gh_json({"comments": [{"body": body}]}), ""),
        }

    def brain_lines(self):
        if not os.path.exists(self.brain):
            return []
        with open(self.brain, encoding="utf-8") as f:
            return [line for line in f.read().splitlines() if line.strip()]

    def test_the_pr_1653_sample_is_reported_with_its_rounds_wait_and_action(self):
        d = self.make(self.pr_rules())
        d.put_review(1653, {"rounds": 4, "verdict": "CHANGES", "summary": "缺少回归用例说明",
                            "at": stamp(NOW - 26 * 3600)})
        report = d.needs_human_report()
        self.assertEqual(report["needs_human"]["count"], 1)
        entry = report["needs_human"]["entries"][0]
        self.assertEqual(entry["number"], 1653)
        self.assertEqual(entry["kind"], NH.KIND_PR)
        self.assertEqual(entry["rounds"], 4)
        self.assertEqual(entry["waiting_hours"], 26.0)
        self.assertEqual(entry["action"], NH.ACTION_MAINTAINER)
        self.assertEqual(entry["summary"], "缺少回归用例说明")

    def test_the_brain_log_line_names_the_title_and_the_verdict_summary(self):
        d = self.make(self.pr_rules())
        d.put_review(1653, {"rounds": 4, "verdict": "CHANGES", "summary": "缺少回归用例说明",
                            "at": stamp(NOW - 26 * 3600)})
        d.needs_human_report()
        line = self.brain_lines()[0]
        self.assertIn("#1653", line)
        self.assertIn("审查轮次耗尽", line)
        self.assertIn("缺少回归用例说明", line)

    def test_the_backlog_is_written_to_the_brain_log_once(self):
        d = self.make(self.pr_rules())
        d.put_review(1653, {"rounds": 4, "verdict": "CHANGES", "summary": "缺少回归用例说明",
                            "at": stamp(NOW - 26 * 3600)})
        d.needs_human_report()
        lines = self.brain_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("#1653", lines[0])
        d.needs_human_report()
        self.assertEqual(len(self.brain_lines()), 1)

    def test_the_backlog_is_not_written_again_in_the_next_rounds(self):
        clock = {"now": NOW}
        d = self.make(self.pr_rules(), clock=lambda: clock["now"])
        d.put_review(1653, {"rounds": 4, "verdict": "CHANGES", "summary": "缺少回归用例说明",
                            "at": stamp(NOW - 26 * 3600)})
        d.needs_human_report()
        self.assertEqual(len(self.brain_lines()), 1)
        for round_number in range(3):
            clock["now"] = NOW + 60 * (round_number + 1)
            d.needs_human_report()
        self.assertEqual(len(self.brain_lines()), 1)

    def test_a_waiting_entry_is_written_to_the_brain_log_again_after_a_day(self):
        clock = {"now": NOW}
        d = self.make(self.pr_rules(), clock=lambda: clock["now"])
        d.put_review(1653, {"rounds": 4, "verdict": "CHANGES", "summary": "缺少回归用例说明",
                            "at": stamp(NOW - 26 * 3600)})
        d.needs_human_report()
        self.assertEqual(len(self.brain_lines()), 1)
        clock["now"] = NOW + 25 * 3600
        d.needs_human_report()
        lines = self.brain_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("#1653", lines[1])

    def test_an_empty_backlog_is_an_empty_summary_and_writes_nothing(self):
        d = self.make({"api ": (0, gh_json([]), "")})
        report = d.needs_human_report()
        self.assertEqual(report["needs_human"], {"count": 0, "entries": []})
        self.assertEqual(report["due"], 0)
        self.assertEqual(self.brain_lines(), [])

    def test_a_failing_github_call_does_not_break_the_report(self):
        d = self.make({"api ": (1, "", "boom")})
        report = d.needs_human_report()
        self.assertEqual(report["needs_human"]["count"], 0)
        self.assertEqual(self.brain_lines(), [])

    def test_a_manual_report_neither_writes_the_brain_log_nor_marks_the_entries(self):
        d = self.make(self.pr_rules())
        d.put_review(1653, {"rounds": 4, "verdict": "CHANGES", "summary": "缺少回归用例说明",
                            "at": stamp(NOW - 26 * 3600)})
        report = d.needs_human_report(record=False)
        self.assertEqual(report["needs_human"]["count"], 1)
        self.assertEqual(self.brain_lines(), [])
        d.needs_human_report()
        self.assertEqual(len(self.brain_lines()), 1)

    def test_the_report_does_not_touch_a_label_or_a_round_counter(self):
        d = self.make(self.pr_rules())
        d.put_review(1653, {"rounds": 4, "verdict": "CHANGES", "summary": "缺少回归用例说明",
                            "at": stamp(NOW - 26 * 3600)})
        d.needs_human_report()
        writes = [call for call in self.calls
                  if "pr edit" in call or "issue edit" in call or "pr comment" in call]
        self.assertEqual(writes, [])
        review = d.load_reviews()["1653"]
        self.assertEqual(review["rounds"], 4)
        self.assertNotIn("needs_human_sha", review)

    def test_the_tick_publishes_the_backlog_in_the_lane_status_snapshot(self):
        d = self.make(self.pr_rules())
        d.put_review(1653, {"rounds": 4, "verdict": "CHANGES", "summary": "缺少回归用例说明",
                            "at": stamp(NOW - 26 * 3600)})
        d.probe = lambda _lanes, _state: {}
        d.harvest = lambda *_args: None
        d.sweep_leaked_inprogress = lambda *_args: None
        d.queue = lambda: []
        d.dispatch_reviews = lambda *_args: None
        d.assign_issues = lambda *_args: None
        d.replenish = lambda *_args: None
        d.tick({}, {})
        with open(d.path("lanes-status.json"), encoding="utf-8") as stream:
            status = json.load(stream)
        self.assertEqual(status["_needs_human"]["count"], 1)
        self.assertEqual(status["_needs_human"]["entries"][0]["number"], 1653)


if __name__ == "__main__":
    unittest.main()
