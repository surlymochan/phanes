"""Lane review metrics (lib/dispatch/review_metrics.py): the counting rules and the report (Refs #1733)."""

import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "dispatch_review_metrics_test", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "review_metrics.py"))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def record(**fields):
    base = {"sha": "a" * 40, "verdict": "APPROVE", "lane": "M5", "backend": "pi",
            "at": "2026-11-03 10:00:00"}
    base.update(fields)
    return base


class CountingRules(unittest.TestCase):
    def test_a_record_with_a_verdict_has_one_formal_round(self):
        self.assertEqual(M.formal_rounds(record(review_rounds=1)), (1, "record"))
        self.assertTrue(M.first_round_pass(record(review_rounds=1, first_verdict="APPROVE")))

    def test_the_current_verdict_decides_the_round_count(self):
        # a changes round is already inside the old counter
        self.assertEqual(M.formal_rounds(record(verdict="CHANGES", rounds=1)), (1, "legacy-rounds"))
        # the approving verdict is one round more than the changes rounds
        self.assertEqual(M.formal_rounds(record(verdict="APPROVE", rounds=1)), (2, "legacy-rounds"))
        self.assertFalse(M.first_round_pass(record(verdict="APPROVE", rounds=1)))

    def test_a_historical_approve_without_rounds_is_a_first_round_pass(self):
        rounds, source = M.formal_rounds(record(verdict="APPROVE"))
        self.assertEqual((rounds, source), (1, "legacy-inferred"))
        self.assertTrue(M.first_round_pass(record(verdict="APPROVE")))

    def test_a_record_without_a_usable_round_count_is_skipped(self):
        self.assertEqual(M.formal_rounds(record(verdict="CHANGES")), (None, "unknown-rounds"))
        self.assertIsNone(M.first_round_pass(record(verdict="CHANGES")))
        self.assertEqual(M.formal_rounds({"fails": 2}), (None, "no-verdict"))

    def test_the_first_verdict_is_kept_when_a_later_verdict_arrives(self):
        # the first round asked for changes; the rebase re-review approved the pull
        # request: the first verdict (and the first round) did not change
        row = record(review_rounds=1, first_verdict="CHANGES", verdict="APPROVE", rebase_reviews=1)
        self.assertEqual(M.first_verdict_of(row), "changes")
        self.assertFalse(M.first_round_pass(row))
        self.assertEqual(M.formal_rounds(row), (1, "record"))

    def test_a_historical_record_reads_its_first_verdict_from_the_rounds_counter(self):
        self.assertEqual(M.first_verdict_of(record(verdict="CHANGES", rounds=2)), "changes")
        self.assertEqual(M.first_verdict_of(record(verdict="APPROVE")), "approve")
        self.assertEqual(M.first_verdict_of(record(verdict="APPROVE", first_verdict="CHANGES")), "changes")

    def test_the_finding_counts_are_read_from_the_conclusion(self):
        counts = M.parse_counts("VERDICT: CHANGES\nBLOCKERS: 4\nFOLLOW-UPS: 2\nSUMMARY: x\n")
        self.assertEqual(counts, {"blockers": 4, "follow_ups": 2})
        self.assertEqual(M.parse_counts("VERDICT: APPROVE\n"), {"blockers": None, "follow_ups": None})
        self.assertEqual(M.parse_counts("BLOCKERS: 1\nFOLLOW-UP: 0\n"),
                         {"blockers": 1, "follow_ups": 0})

    def test_the_finding_entries_of_the_review_format_are_counted(self):
        text = ("VERDICT: CHANGES\nBLOCKERS: 4\nSUMMARY: x\n\n## Findings\n"
                "- [BLOCKER] `tools/a.py:1` wrong\n"
                "- [BLOCKER] `tools/b.py:2` broken\n"
                "- [FOLLOW-UP] add the missing test\n")
        self.assertEqual(M.parse_counts(text), {"blockers": 2, "follow_ups": 1})
        self.assertEqual(M.follow_up_entries(text), ["add the missing test"])

    def test_a_mention_of_a_marker_inside_a_sentence_is_not_a_finding(self):
        text = "VERDICT: APPROVE\nSUMMARY: the [FOLLOW-UP] word inside a sentence is not a finding\n"
        self.assertEqual(M.parse_counts(text), {"blockers": None, "follow_ups": None})
        self.assertEqual(M.follow_up_entries(text), [])

    def test_the_week_of_a_stamp_is_the_iso_week(self):
        self.assertEqual(M.week_of("2026-11-03 10:00:00"), "2026-W45")
        self.assertEqual(M.week_of("2026-10-27 10:00:00"), "2026-W44")
        self.assertEqual(M.week_of("2026-01-01 10:00:00"), "2026-W01")
        self.assertIsNone(M.week_of(""))
        self.assertIsNone(M.week_of("not a date"))


class Report(unittest.TestCase):
    def test_an_empty_review_set_is_a_report_without_numbers(self):
        report = M.compute({})
        self.assertEqual(report["totals"]["reviews"], 0)
        self.assertIsNone(report["totals"]["first_round_pass_rate"])
        self.assertIsNone(report["totals"]["avg_rounds"])
        self.assertEqual(report["lanes"], {})
        self.assertEqual(report["skipped_total"], 0)
        self.assertIn("first_round_pass", report["definitions"])

    def test_records_without_a_verdict_or_a_round_count_are_skipped(self):
        report = M.compute({"1": {"fails": 2, "lane": "M5"},
                             "2": {"verdict": "CHANGES", "lane": "M5"},
                             "3": "not a record",
                             "4": record(review_rounds=1)})
        self.assertEqual(report["totals"]["reviews"], 1)
        self.assertEqual(report["skipped"], {"no-verdict": 1, "not-a-record": 1, "unknown-rounds": 1})
        self.assertEqual(report["skipped_total"], 3)

    def test_the_lane_and_the_backend_are_counted_apart(self):
        report = M.compute({"1": record(review_rounds=1, lane="M5", backend="pi"),
                             "2": record(review_rounds=2, lane="M5", backend="pi", verdict="CHANGES"),
                             "3": record(review_rounds=1, lane="CX2", backend="codex")})
        self.assertEqual(report["lanes"]["M5"]["reviews"], 2)
        self.assertEqual(report["lanes"]["M5"]["first_round_pass_rate"], 0.5)
        self.assertEqual(report["lanes"]["M5"]["avg_rounds"], 1.5)
        self.assertEqual(report["lanes"]["CX2"]["first_round_pass_rate"], 1.0)
        self.assertEqual(report["backends"]["pi"]["reviews"], 2)
        self.assertEqual(report["backends"]["codex"]["reviews"], 1)

    def test_the_targets_are_compared_with_every_group(self):
        report = M.compute({"1": record(review_rounds=1, lane="M5")})
        self.assertTrue(report["lanes"]["M5"]["meets_first_round_target"])
        self.assertTrue(report["lanes"]["M5"]["meets_rounds_target"])
        self.assertEqual(report["targets"]["first_round_pass_rate"], 0.6)
        self.assertEqual(report["targets"]["avg_review_rounds"], 2.0)
        report = M.compute({"1": record(review_rounds=1, lane="M5", verdict="CHANGES")})
        self.assertFalse(report["lanes"]["M5"]["meets_first_round_target"])

    def test_the_blocker_and_follow_up_spread_is_bucketed(self):
        report = M.compute({"1": record(review_rounds=1, blockers=0, follow_ups=0),
                             "2": record(review_rounds=1, blockers=4, follow_ups=2),
                             "3": record(review_rounds=1)})
        self.assertEqual(report["totals"]["blockers"], {"0": 1, "1": 0, "2": 0, "3+": 1, "unknown": 1})
        self.assertEqual(report["totals"]["follow_ups"], {"0": 1, "1": 0, "2": 1, "3+": 0, "unknown": 1})

    def test_the_follow_up_cards_are_summed(self):
        report = M.compute({"1": record(review_rounds=1, followup_issues=[31, 32]),
                             "2": record(review_rounds=1, followup_tickets=1, lane="CX2")})
        self.assertEqual(report["totals"]["followup_tickets"], 3)
        self.assertEqual(report["lanes"]["M5"]["followup_tickets"], 2)

    def test_the_rework_task_of_a_changes_round_is_not_a_follow_up_card(self):
        report = M.compute({"1": record(review_rounds=1, verdict="CHANGES", rework_tasks=1)})
        self.assertEqual(report["totals"]["followup_tickets"], 0)
        self.assertEqual(report["totals"]["rework_tasks"], 1)
        self.assertEqual(report["lanes"]["M5"]["rework_tasks"], 1)

    def test_the_pull_request_data_fills_in_the_author_lane(self):
        prs = [{"number": 7, "headRefName": "fleet/CX10-issue1733-20261103"}]
        report = M.compute({"7": record(review_rounds=1)}, prs=prs)
        self.assertIn("CX10", report["author_lanes"])
        self.assertEqual(report["author_lanes"]["CX10"]["reviews"], 1)

    def test_the_pull_request_labels_fill_in_the_backend(self):
        prs = [{"number": 7, "headRefName": "fleet/CX10-issue1733-20261103",
                "labels": [{"name": "backend:codex"}]}]
        report = M.compute({"7": record(review_rounds=1, backend=None)}, prs=prs)
        self.assertEqual(report["backends"]["codex"]["reviews"], 1)

    def test_the_lanes_that_were_bottom_in_two_consecutive_weeks_are_listed(self):
        reviews = {
            # week 2026-W43: M5 is the bottom lane (one changes round, no pass)
            "1": record(review_rounds=1, lane="M5", verdict="CHANGES", at="2026-10-20 10:00:00"),
            "2": record(review_rounds=1, lane="CX2", at="2026-10-21 10:00:00"),
            # week 2026-W44: M5 is the bottom lane again
            "3": record(review_rounds=1, lane="M5", verdict="CHANGES", at="2026-10-27 10:00:00"),
            "4": record(review_rounds=1, lane="CX2", at="2026-10-28 10:00:00"),
        }
        report = M.compute(reviews)
        weeks = [row["week"] for row in report["weeks"]]
        self.assertEqual(weeks, ["2026-W43", "2026-W44"])
        self.assertEqual(report["weeks"][0]["bottom_lane"], "M5")
        self.assertEqual([row["lane"] for row in report["bottom_lanes"]], ["M5"])
        self.assertEqual(report["bottom_lanes"][0]["weeks"], ["2026-W44"])

    def test_the_two_lane_views_are_listed_apart(self):
        # the reviewing lanes and the author lanes are two views: a lane that is
        # only bottom in one of them is not in the other list
        reviews = {
            "1": record(review_rounds=1, lane="CX4", verdict="CHANGES", author_lane="M5",
                        at="2026-10-20 10:00:00"),
            "2": record(review_rounds=1, lane="M5", author_lane="CX4", at="2026-10-21 10:00:00"),
            "3": record(review_rounds=1, lane="CX4", verdict="CHANGES", author_lane="M5",
                        at="2026-10-27 10:00:00"),
            "4": record(review_rounds=1, lane="M5", author_lane="CX4", at="2026-10-28 10:00:00"),
        }
        report = M.compute(reviews)
        self.assertEqual([row["lane"] for row in report["bottom_lanes"]], ["CX4"])
        self.assertEqual([row["lane"] for row in report["bottom_author_lanes"]], ["M5"])

    def test_two_weeks_with_a_gap_are_not_a_streak(self):
        reviews = {
            "1": record(review_rounds=1, lane="M5", verdict="CHANGES", at="2026-10-13 10:00:00"),
            "2": record(review_rounds=1, lane="M5", verdict="CHANGES", at="2026-10-27 10:00:00"),
        }
        report = M.compute(reviews)
        self.assertEqual(report["bottom_lanes"], [])

    def test_the_definitions_write_the_measurement_rules(self):
        definitions = M.compute({"1": record(review_rounds=1)})["definitions"]
        self.assertIn("首轮通过率", definitions["first_round_pass"])
        self.assertIn("平均轮次", definitions["review_rounds"])
        self.assertIn("rebase", definitions["rebase_reviews"])
        self.assertIn("follow-up", definitions["followup_tickets"])

    def test_the_summary_names_the_targets(self):
        report = M.compute({"1": record(review_rounds=1)})
        text = M.render(report)
        self.assertIn("first-round pass", text)
        self.assertIn("target >= 60%", text)
        self.assertIn("M5", text)


if __name__ == "__main__":
    unittest.main()
