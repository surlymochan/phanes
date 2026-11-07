"""Tests for the merge backlog of the health check (Refs #1696).

Four halves are checked here:

* the pure module ``openswarm/lib/dispatch/merge_backlog.py``: the stage of a
  pull request, the dwell time of that stage and the thresholds;
* the stage transitions: a pull request that moves from one stage to the next
  is counted from the moment it entered the new stage, not from the moment it
  was opened;
* the replay of the 2026-11-02 night data: the external lane card #1632 waits
  for a human, the merged pull request is not in the queue;
* the collector ``tools/fleet-status-page.py``: the section shows up in the
  health check and the rows that are over their threshold are named in the
  brain log.
"""

import importlib.util
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
MODULE_PATH = ROOT / "openswarm" / "lib" / "dispatch" / "merge_backlog.py"
TOOL_PATH = ROOT / "tools" / "fleet-status-page.py"
FIXTURE = HERE / "fixtures" / "merge-backlog-2026-11-02"

spec = importlib.util.spec_from_file_location("dispatch_merge_backlog", MODULE_PATH)
merge_backlog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge_backlog)

tool_spec = importlib.util.spec_from_file_location("fleet_status_page", TOOL_PATH)
tool = importlib.util.module_from_spec(tool_spec)
tool_spec.loader.exec_module(tool)

# 2026-11-02 21:05:00 UTC+8, the moment of the night shift.
NOW = 1793624700.0
# The fleet timezone: the review ledger writes local wall time without a zone.
TZ = 28800


def pr(number=1, body=None, **extra):
    """One pull request of a test.

    ``body=None`` means "no body at all"; the default body closes the card of
    the same number.
    """
    row = {"number": number, "state": "OPEN", "title": "pr %s" % number}
    row["body"] = "Closes #%s" % number if body is None else body
    row.update(extra)
    return row


def card(number=1, *labels):
    """One card of a test."""
    return {"number": number, "state": "OPEN",
            "labels": [{"name": name} for name in labels]}


def green_rollup(completed="2026-11-02T10:06:00Z"):
    return [{"__typename": "CheckRun", "name": "Phanes components",
             "status": "COMPLETED", "conclusion": "SUCCESS",
             "startedAt": "2026-11-02T10:00:00Z", "completedAt": completed}]


def red_rollup(completed="2026-11-02T10:06:00Z"):
    return [{"__typename": "CheckRun", "name": "Phanes components",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "startedAt": "2026-11-02T10:00:00Z", "completedAt": completed}]


def lane_review_rollup():
    """The rollup of a pull request whose only check is the lane verdict."""
    return [{"__typename": "StatusContext", "context": "lane-review", "state": "SUCCESS"}]


class StageTests(unittest.TestCase):
    """The stage of a pull request of the merge queue."""

    def test_the_card_carries_the_lane_pr_label(self):
        """The lane-pr label is on the card, never on the pull request."""
        rows = merge_backlog.backlog(
            [pr(1, labels=[{"name": "lane-pr"}])], [card(1)], {}, NOW, offset=TZ)
        self.assertEqual(rows, [], "a pull request label is not the queue signal")

        rows = merge_backlog.backlog([pr(1)], [card(1, "lane-pr")], {}, NOW, offset=TZ)
        self.assertEqual([row["number"] for row in rows], [1])

    def test_the_link_is_read_from_the_body_or_the_branch(self):
        branch = pr(2, headRefName="fleet/M2-issue8-20261102-000000")
        branch["body"] = ""
        rows = merge_backlog.backlog(
            [pr(1, body="Closes #7"), branch],
            [card(7, "lane-pr"), card(8, "lane-pr")], {}, NOW, offset=TZ)
        self.assertEqual([row["issue"] for row in rows], [7, 8])

    def test_a_pull_request_without_a_link_is_not_in_the_queue(self):
        pull = pr(1)
        pull["body"] = ""
        rows = merge_backlog.backlog([pull], [card(1, "lane-pr")], {}, NOW)
        self.assertEqual(rows, [])

    def test_a_merged_pull_request_is_not_in_the_queue(self):
        rows = merge_backlog.backlog(
            [pr(1, state="MERGED", mergedAt="2026-11-02T10:00:00Z")],
            [card(1, "lane-pr")], {}, NOW)
        self.assertEqual(rows, [])

    def test_a_draft_is_not_in_the_queue(self):
        rows = merge_backlog.backlog([pr(1, isDraft=True)], [card(1, "lane-pr")], {}, NOW)
        self.assertEqual(rows, [])

    def test_the_thin_card_waits_for_a_human(self):
        """A red contract gate plus the needs-human label: a thin card exemption."""
        pull = pr(1, labels=[{"name": "needs-human"}], statusCheckRollup=[
            {"__typename": "StatusContext", "context": "external-lane/contract",
             "state": "FAILURE"},
            {"__typename": "StatusContext", "context": "lane-review", "state": "FAILURE"}])
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_HUMAN)
        self.assertEqual(row["stage_text"], "等人工裁决（薄卡豁免）")
        self.assertTrue(any("薄卡" in signal for signal in row["signals"]), row["signals"])

    def test_a_plain_human_arbitration_has_no_thin_card_reason(self):
        pull = pr(1, labels=[{"name": "needs-human"}], statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_HUMAN)
        self.assertEqual(row["stage_text"], "等人工裁决")

    def test_changes_requested_is_a_stage_of_its_own(self):
        pull = pr(1, labels=[{"name": "needs-changes"}], statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_CHANGES)

    def test_a_red_check_is_its_own_stage(self):
        pull = pr(1, statusCheckRollup=red_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_CI_RED)
        self.assertTrue(any("Phanes components" in signal for signal in row["signals"]),
                        row["signals"])

    def test_an_approved_green_pull_request_waits_for_the_merge_loop(self):
        pull = pr(1, labels=[{"name": "lane-reviewed"}], statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_APPROVED)

    def test_a_moved_head_waits_for_a_re_review(self):
        """The verdict was written for another head: the review is due again."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "APPROVE", "at": "2026-11-02 10:00:00"}}
        pull = pr(1, headRefOid="b" * 40, statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_RE_REVIEW)

    def test_without_a_review_record_the_stage_is_not_guessed(self):
        """No review source at all: the absence of a verdict is not evidence."""
        pull = pr(1, statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), None, NOW, offset=TZ,
                                     review_source=False)
        self.assertEqual(row["stage"], merge_backlog.STAGE_UNKNOWN)
        self.assertIn("review 记录缺失", row["signals"])

    def test_a_pr_without_a_rollup_and_without_a_record_is_unknown(self):
        pull = pr(1, createdAt="2026-11-02T10:00:00Z")
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_UNKNOWN)
        self.assertIn("CI 状态缺失", row["signals"])
        self.assertIsNone(row["dwell_s"])

    def test_an_approved_pull_request_without_ci_data_is_unknown(self):
        pull = pr(1, labels=[{"name": "lane-reviewed"}])
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_UNKNOWN)
        self.assertIn("CI 状态缺失", row["signals"])

    def test_an_approved_pull_request_without_any_check_is_unknown(self):
        """An empty rollup is no evidence of a green run."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "APPROVE", "at": "2026-11-02 10:00:00"}}
        pull = pr(1, headRefOid="a" * 40, labels=[{"name": "lane-reviewed"}],
                  statusCheckRollup=[])
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_UNKNOWN)
        self.assertIn("CI 无检查项", row["signals"])
        self.assertIsNone(row["dwell_s"])

    def test_a_rollup_with_only_the_lane_verdict_is_not_green(self):
        """The lane verdict is not a CI check: without one there is no CI evidence."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "APPROVE", "at": "2026-11-02 10:00:00"}}
        pull = pr(1, headRefOid="a" * 40, labels=[{"name": "lane-reviewed"}],
                  statusCheckRollup=lane_review_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_UNKNOWN)
        self.assertIn("CI 无检查项", row["signals"])
        self.assertIsNone(row["dwell_s"])

    def test_a_red_check_without_a_time_has_no_dwell_time(self):
        pull = pr(1, statusCheckRollup=[{"__typename": "StatusContext",
                                          "context": "external-lane/contract",
                                          "state": "FAILURE"}])
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_CI_RED)
        self.assertIsNone(row["dwell_s"])
        self.assertIn("CI 检查时间缺失", row["signals"])


class StageTransitionTests(unittest.TestCase):
    """A pull request moves from one stage to the next; the stage follows."""

    def test_an_approved_pull_request_whose_checks_break_waits_for_the_ci(self):
        """已批待合并 → CI 未绿: the checks of the approved head went red."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "APPROVE", "at": "2026-11-02 18:00:00"}}
        pull = pr(1, headRefOid="a" * 40, labels=[{"name": "lane-reviewed"}],
                  statusCheckRollup=red_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_CI_RED)
        self.assertEqual(row["dwell_source"], merge_backlog.SOURCE_CI)

    def test_an_approved_pull_request_whose_head_moved_waits_for_a_re_review(self):
        """已批待合并 → 等复审: the head moved, the approval is about another commit."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "APPROVE", "at": "2026-11-02 18:00:00"}}
        pull = pr(1, headRefOid="b" * 40, updatedAt="2026-11-02T11:00:00Z",
                  statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_RE_REVIEW)
        self.assertEqual(row["dwell_source"], merge_backlog.SOURCE_UPDATED)

    def test_a_verdict_about_another_head_waits_for_a_re_review(self):
        """A CHANGES verdict about an older commit does not hold the fix stage."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "CHANGES", "at": "2026-11-02 18:00:00"}}
        pull = pr(1, headRefOid="b" * 40, updatedAt="2026-11-02T11:00:00Z",
                  statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_RE_REVIEW)

    def test_a_reviewed_pull_request_that_is_asked_for_changes_waits_for_the_fix(self):
        """等首审 → 审查 CHANGES 待修复: the lane review asked for changes."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "CHANGES", "at": "2026-11-02 18:00:00"}}
        pull = pr(1, headRefOid="a" * 40, labels=[{"name": "needs-changes"}],
                  statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_CHANGES)

    def test_a_pull_request_asked_for_a_human_stops_waiting_for_a_review(self):
        """审查 CHANGES 待修复 → 等人工裁决: the rounds are over."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "CHANGES", "at": "2026-11-02 18:00:00",
                        "rounds": 4, "needs_human_reason": "rounds",
                        "needs_human_at": "2026-11-02 18:05:00"}}
        pull = pr(1, headRefOid="a" * 40,
                  labels=[{"name": "needs-changes"}, {"name": "needs-human"}],
                  statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_HUMAN)

    def test_the_checks_that_turn_green_after_the_approval_start_the_merge_stage(self):
        """CI 未绿 → 已批待合并: the stage starts when the checks turn green.

        The approval came three hours before the checks turned green: the pull
        request did not wait for the merge loop for three hours, it waited for
        the checks.
        """
        ledger = {"1": {"sha": "a" * 40, "verdict": "APPROVE", "at": "2026-11-02 18:05:00"}}
        pull = pr(1, headRefOid="a" * 40, labels=[{"name": "lane-reviewed"}],
                  createdAt="2026-11-02T02:00:00Z",
                  statusCheckRollup=green_rollup(completed="2026-11-02T13:00:00Z"))
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_APPROVED)
        self.assertEqual(row["dwell_source"], merge_backlog.SOURCE_APPROVED)
        self.assertEqual(row["dwell_s"], 300.0)      # five minutes, not three hours
        self.assertFalse(row["overdue"])


class DwellTests(unittest.TestCase):
    """The dwell time is counted from the start of the stage, not from the PR."""

    def test_an_old_pull_request_that_was_just_approved_is_not_overdue(self):
        ledger = {"1": {"sha": "a" * 40, "verdict": "APPROVE", "at": "2026-11-02 19:50:00"}}
        pull = pr(1, headRefOid="a" * 40, labels=[{"name": "lane-reviewed"}],
                  createdAt="2026-11-01T10:00:00Z", updatedAt="2026-11-02T11:50:00Z",
                  statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_APPROVED)
        self.assertEqual(row["dwell_source"], merge_backlog.SOURCE_APPROVED)
        self.assertLess(row["dwell_s"], merge_backlog.APPROVED_DWELL_LIMIT_S)
        self.assertEqual(merge_backlog.brain_log_lines([row]), [])

    def test_the_dwell_of_a_first_review_starts_at_the_creation(self):
        pull = pr(1, createdAt="2026-11-01T10:00:00Z", updatedAt="2026-11-01T10:06:00Z",
                  statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertEqual(row["dwell_source"], merge_backlog.SOURCE_CREATED)
        self.assertEqual(row["dwell_s"], NOW - merge_backlog.epoch("2026-11-01T10:00:00Z"))

    def test_the_dwell_of_a_red_ci_starts_at_the_failing_check(self):
        pull = pr(1, createdAt="2026-11-01T10:00:00Z", statusCheckRollup=red_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertEqual(row["dwell_source"], merge_backlog.SOURCE_CI)
        self.assertEqual(row["dwell_s"],
                         NOW - merge_backlog.epoch("2026-11-02T10:06:00Z"))

    def test_the_dwell_of_the_human_stage_starts_at_the_label(self):
        """等人工裁决 counts from the moment the label went on."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "CHANGES", "at": "2026-11-01 18:34:11",
                        "needs_human_reason": "fails", "needs_human_at": "2026-11-01 19:02:47"}}
        pull = pr(1, headRefOid="a" * 40, labels=[{"name": "needs-human"}],
                  statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_HUMAN)
        self.assertEqual(row["dwell_source"], merge_backlog.SOURCE_NEEDS_HUMAN)
        self.assertEqual(row["dwell_s"],
                         NOW - merge_backlog.epoch("2026-11-01 19:02:47", TZ))

    def test_without_the_label_time_the_human_stage_has_no_dwell(self):
        """The verdict before the escalation is not the moment of the label."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "CHANGES", "at": "2026-11-01 18:34:11"}}
        pull = pr(1, headRefOid="a" * 40, labels=[{"name": "needs-human"}],
                  updatedAt="2026-11-02T09:00:00Z", statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_HUMAN)
        self.assertIsNone(row["dwell_s"])
        self.assertIn("人工裁决标记时间缺失", row["signals"])
        self.assertEqual(merge_backlog.brain_log_lines([row]), [])

    def test_a_missing_clock_leaves_the_dwell_unknown(self):
        pull = pr(1, statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertIsNone(row["dwell_s"])
        self.assertEqual(merge_backlog.brain_log_lines([row]), [])

    def test_an_approved_pull_request_without_a_verdict_time_has_no_dwell(self):
        """The lane-reviewed label says "approved", but not when."""
        pull = pr(1, labels=[{"name": "lane-reviewed"}], statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_APPROVED)
        self.assertIsNone(row["dwell_s"])
        self.assertIn("批准时间缺失", row["signals"])

    def test_an_approved_pull_request_without_a_green_time_has_no_dwell(self):
        """Green checks without a completion time do not say when they turned green."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "APPROVE", "at": "2026-11-02 10:00:00"}}
        pull = pr(1, headRefOid="a" * 40, labels=[{"name": "lane-reviewed"}],
                  statusCheckRollup=[{"__typename": "CheckRun", "name": "Phanes components",
                                      "status": "COMPLETED", "conclusion": "SUCCESS"}])
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_APPROVED)
        self.assertIsNone(row["dwell_s"])
        self.assertIn("CI 转绿时间缺失", row["signals"])


class ThresholdTests(unittest.TestCase):
    """The thresholds of the naming."""

    def test_an_approved_pull_request_over_two_hours_is_named(self):
        ledger = {"1": {"sha": "a" * 40, "verdict": "APPROVE", "at": "2026-11-02 17:00:00"}}
        pull = pr(1, headRefOid="a" * 40, labels=[{"name": "lane-reviewed"}],
                  statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertTrue(row["overdue"])
        self.assertEqual(merge_backlog.brain_log_lines([row]),
                         ["合并积压点名 #1 已批待合并 停留2h59m（阈值 2h00m）"])

    def test_a_stage_over_a_day_is_named(self):
        pull = pr(1, createdAt="2026-11-01T10:00:00Z", statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertTrue(row["overdue"])
        self.assertEqual(merge_backlog.brain_log_lines([row]),
                         ["合并积压点名 #1 等首审 停留27h05m（阈值 24h00m）"])

    def test_a_fresh_pull_request_is_not_named(self):
        pull = pr(1, createdAt="2026-11-02T12:30:00Z", statusCheckRollup=green_rollup())
        row = merge_backlog.classify(pull, card(1, "lane-pr"), {}, NOW, offset=TZ)
        self.assertFalse(row["overdue"])
        self.assertEqual(merge_backlog.brain_log_lines([row]), [])

    def test_a_pull_request_that_just_turned_green_is_not_named(self):
        """The merge stage started five minutes ago: it is not two hours old."""
        ledger = {"1": {"sha": "a" * 40, "verdict": "APPROVE", "at": "2026-11-02 18:05:00"}}
        pull = pr(1, headRefOid="a" * 40, labels=[{"name": "lane-reviewed"}],
                  statusCheckRollup=green_rollup(completed="2026-11-02T13:00:00Z"))
        row = merge_backlog.classify(pull, card(1, "lane-pr"), ledger, NOW, offset=TZ)
        self.assertEqual(row["stage"], merge_backlog.STAGE_APPROVED)
        self.assertFalse(row["overdue"])
        self.assertEqual(merge_backlog.brain_log_lines([row]), [])


class NightReplayTests(unittest.TestCase):
    """The replay of the 2026-11-02 night data."""

    def setUp(self):
        self.prs = json.loads((FIXTURE / "gh-lane-prs.json").read_text(encoding="utf-8"))
        self.cards = json.loads((FIXTURE / "gh-lane-issues.json").read_text(encoding="utf-8"))
        self.ledger = json.loads((FIXTURE / "reviews.json").read_text(encoding="utf-8"))
        self.rows = merge_backlog.backlog(self.prs, self.cards, self.ledger, NOW, offset=TZ)
        self.by_number = {row["number"]: row for row in self.rows}

    def test_the_external_lane_card_waits_for_a_human(self):
        row = self.by_number[1632]
        self.assertEqual(row["stage"], merge_backlog.STAGE_HUMAN)
        self.assertEqual(row["stage_text"], "等人工裁决（薄卡豁免）")
        self.assertEqual(row["dwell_source"], merge_backlog.SOURCE_NEEDS_HUMAN)
        self.assertGreater(row["dwell_s"], merge_backlog.STAGE_DWELL_LIMIT_S)

    def test_the_merged_pull_request_is_not_in_the_queue(self):
        self.assertNotIn(1635, self.by_number)

    def test_a_card_without_the_lane_pr_label_is_not_in_the_queue(self):
        self.assertNotIn(1636, self.by_number)

    def test_the_queue_of_that_night(self):
        self.assertEqual(sorted(self.by_number), [1632, 1633, 1634, 1637, 1638, 1639])
        self.assertEqual(self.by_number[1633]["stage"], merge_backlog.STAGE_APPROVED)
        self.assertEqual(self.by_number[1634]["stage"], merge_backlog.STAGE_FIRST_REVIEW)
        self.assertEqual(self.by_number[1637]["stage"], merge_backlog.STAGE_UNKNOWN)
        self.assertEqual(self.by_number[1638]["stage"], merge_backlog.STAGE_CI_RED)
        self.assertEqual(self.by_number[1639]["stage"], merge_backlog.STAGE_APPROVED)

    def test_the_named_rows_of_that_night(self):
        self.assertEqual(merge_backlog.brain_log_lines(self.rows), [
            "合并积压点名 #1632 等人工裁决（薄卡豁免） 停留26h02m（阈值 24h00m）",
            "合并积压点名 #1633 已批待合并 停留3h00m（阈值 2h00m）",
            "合并积压点名 #1634 等首审 停留30h05m（阈值 24h00m）"])

    def test_the_pull_request_whose_checks_turned_green_late_is_not_named(self):
        """#1639 was approved three hours before the checks turned green."""
        row = self.by_number[1639]
        self.assertEqual(row["stage"], merge_backlog.STAGE_APPROVED)
        self.assertEqual(row["dwell_source"], merge_backlog.SOURCE_APPROVED)
        self.assertEqual(row["dwell_s"], 300.0)
        self.assertFalse(row["overdue"])

    def test_the_pull_request_without_readings_is_unknown(self):
        row = self.by_number[1637]
        self.assertEqual(row["stage"], merge_backlog.STAGE_UNKNOWN)
        self.assertIsNone(row["dwell_s"])
        self.assertIn("CI 状态缺失", row["signals"])


class FixtureShapeTests(unittest.TestCase):
    """The shape of the replay data: the signals have to be the real ones."""

    def setUp(self):
        self.prs = json.loads((FIXTURE / "gh-lane-prs.json").read_text(encoding="utf-8"))
        self.cards = json.loads((FIXTURE / "gh-lane-issues.json").read_text(encoding="utf-8"))
        self.ledger = json.loads((FIXTURE / "reviews.json").read_text(encoding="utf-8"))

    def test_a_pull_request_and_its_card_have_different_numbers(self):
        """An issue and a pull request share one number space in a repository."""
        numbers = {card["number"] for card in self.cards}
        for pull in self.prs:
            issue = merge_backlog.linked_issue(pull)
            self.assertIsNotNone(issue, pull)
            self.assertNotEqual(issue, pull["number"],
                                "PR #%s cannot close issue #%s" % (pull["number"], issue))
            self.assertIn(issue, numbers, pull)

    def test_the_body_and_the_branch_point_at_the_same_card(self):
        """The two real signals of the link have to agree."""
        for pull in self.prs:
            body = merge_backlink_issue(pull.get("body") or "")
            branch = merge_backlog.linked_issue({"body": "", "headRefName":
                                                 pull.get("headRefName")})
            self.assertEqual(body, branch, pull["number"])

    def test_the_cards_of_the_queue_carry_the_lane_pr_label(self):
        cards = {card["number"]: card for card in self.cards}
        for pull in self.prs:
            card = cards[merge_backlog.linked_issue(pull)]
            labels = merge_backlog.label_names(card)
            if pull["number"] == 1636:
                self.assertNotIn("lane-pr", labels, "the card of #1636 never entered the queue")
            else:
                self.assertIn("lane-pr", labels, pull["number"])

    def test_the_head_shas_are_real_commits_of_this_repository(self):
        """The head of a lane pull request is a commit of this repository."""
        if not shutil.which("git"):
            self.skipTest("git is not available")
        for pull in self.prs:
            sha = pull.get("headRefOid")
            self.assertRegex(sha, r"^[0-9a-f]{40}$", pull["number"])
            result = subprocess.run(["git", "cat-file", "-e", sha + "^{commit}"],
                                    cwd=str(ROOT), capture_output=True)
            self.assertEqual(result.returncode, 0,
                             "%s is not a commit of this repository" % sha)

    def test_the_ledger_knows_the_head_of_every_verdict(self):
        for number, entry in self.ledger.items():
            pull = [row for row in self.prs if row["number"] == int(number)]
            self.assertEqual(len(pull), 1, number)
            self.assertEqual(entry["sha"], pull[0]["headRefOid"], number)


def merge_backlink_issue(body):
    """The card a body closes (``Closes #N``)."""
    match = merge_backlog.CLOSES_RE.search(body or "")
    return int(match.group(1)) if match else None


class HealthCheckTests(unittest.TestCase):
    """The section shows up in the health check and the brain log names it."""

    def args_for(self, root, **overrides):
        base = pathlib.Path(root)
        values = {
            "lanes_status": None, "gh_issues": None, "gh_prs": None,
            "capacity": None, "capacity_db": None, "capacity_dir": None,
            "hosts": None, "broker_log": None, "pauses": None, "markers": None,
            "gh_lane_prs": str(base / "gh-lane-prs.json"),
            "gh_lane_issues": str(base / "gh-lane-issues.json"),
            "reviews": str(base / "reviews.json"),
            "ci_policy": None,
            "gh_live": False, "max_age": 300.0, "tz": TZ,
        }
        values.update(overrides)
        return type("Args", (), values)()

    def test_the_section_is_collected_and_shown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name in ("gh-lane-prs.json", "gh-lane-issues.json", "reviews.json"):
                (root / name).write_text((FIXTURE / name).read_text(encoding="utf-8"),
                                         encoding="utf-8")
            report = tool.build_page(self.args_for(root), NOW)
        section = [row for row in report["sections"] if row["key"] == "merge_backlog"]
        self.assertEqual(len(section), 1, report["sections"])
        data = section[0]["data"]
        self.assertEqual(data["count"], 6)
        self.assertEqual(data["overdue"], [1632, 1633, 1634])
        self.assertIn("[积压]", report["text"])
        self.assertIn("#1632 等人工裁决（薄卡豁免）", report["text"])

    def test_the_brain_log_names_the_rows_over_the_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name in ("gh-lane-prs.json", "gh-lane-issues.json", "reviews.json"):
                (root / name).write_text((FIXTURE / name).read_text(encoding="utf-8"),
                                         encoding="utf-8")
            report = tool.build_page(self.args_for(root), NOW)
            log = root / "brain.log"
            tool.append_brain_log(report, str(log))
            written = log.read_text(encoding="utf-8")
        self.assertIn("fleet status @", written)
        self.assertIn("合并积压点名 #1632 等人工裁决（薄卡豁免） 停留26h02m（阈值 24h00m）", written)
        self.assertIn("合并积压点名 #1633 已批待合并 停留3h00m（阈值 2h00m）", written)
        self.assertNotIn("#1639", written)

    def test_without_a_source_the_section_does_not_show_up(self):
        """A merge queue that was not read says nothing: it is not a failed reading."""
        report = tool.build_page(self.args_for(".", gh_lane_prs=None, gh_lane_issues=None,
                                               reviews=None), NOW)
        self.assertEqual([row["key"] for row in report["sections"]
                          if row["key"] == "merge_backlog"], [])
        self.assertEqual(report["summary"]["total"], 7)

    def test_a_broken_backlog_source_does_not_take_the_page_down(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            report = tool.build_page(
                self.args_for(root, gh_lane_prs=str(root / "missing.json")), NOW)
        section = [row for row in report["sections"] if row["key"] == "merge_backlog"]
        self.assertEqual(len(section), 1)
        self.assertFalse(section[0]["verified"])
        self.assertIn("未核实", report["text"])


if __name__ == "__main__":
    unittest.main()
