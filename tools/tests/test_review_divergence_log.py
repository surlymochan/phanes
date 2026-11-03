"""Offline tests of the review divergence log: append, backfill, statistics, bad input."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "review-divergence-log.py"
HEAD_ONE = "a" * 40
HEAD_TWO = "b" * 40


def load_tool():
    """Import the tool as a module so its validator can be exercised directly."""
    spec = importlib.util.spec_from_file_location("review_divergence_log", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReviewDivergenceLogTests(unittest.TestCase):
    """The log records both verdicts per round, backfills the outcome and scores both sides."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.log = Path(self.directory.name) / "divergence-log.jsonl"

    def run_tool(self, *arguments, expect=0):
        result = subprocess.run([sys.executable, str(SCRIPT), "--log", str(self.log), *arguments],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, expect, result.stderr or result.stdout)
        return result

    def append(self, *extra, pr=1751, head=HEAD_ONE, lane="CHANGES", tester="FAIL", expect=0):
        return self.run_tool("append", "--pr", str(pr), "--head", head, "--lane", lane,
                             "--tester", tester, *extra, expect=expect)

    def backfill(self, *extra, pr=1751, head=HEAD_ONE, outcome="re_review", expect=0):
        return self.run_tool("backfill", "--pr", str(pr), "--head", head, "--outcome", outcome,
                             *extra, expect=expect)

    def events(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def stats(self, *extra):
        result = self.run_tool("stats", "--json", *extra)
        return json.loads(result.stdout)

    def pending(self, *extra):
        result = self.run_tool("pending", "--json", *extra)
        return json.loads(result.stdout)

    def test_append_writes_one_event_per_round_and_never_overwrites_an_earlier_round(self):
        self.append("--recorded-at", "2026-11-02T17:33:59Z", head=HEAD_ONE)
        self.append("--recorded-at", "2026-11-02T18:26:41Z", head=HEAD_TWO)
        events = self.events()
        self.assertEqual(len(events), 2)
        self.assertEqual([event["round"] for event in events], [1, 2])
        self.assertEqual({event["kind"] for event in events}, {"verdict"})
        self.assertEqual(events[0]["lane_verdict"], "CHANGES")
        self.assertEqual(events[0]["tester_verdict"], "FAIL")
        self.assertEqual(events[0]["recorded_at"], "2026-11-02T17:33:59Z")
        self.assertEqual(events[1]["head"], HEAD_TWO)
        rounds = self.run_tool("list", "--json")
        self.assertEqual(json.loads(rounds.stdout)["count"], 2)

    def test_a_second_verdict_of_the_same_head_needs_an_explicit_round(self):
        self.append(head=HEAD_ONE)
        self.append(head=HEAD_ONE, expect=2)
        self.append("--round", "2", head=HEAD_ONE)
        self.assertEqual([event["round"] for event in self.events()], [1, 2])

    def test_append_of_a_known_head_names_the_next_round_and_not_a_stale_one(self):
        self.append("--round", "1", head=HEAD_ONE)
        self.append("--round", "2", head=HEAD_ONE)
        result = self.append(head=HEAD_ONE, expect=2)
        self.assertIn("already has round 2", result.stderr)
        self.assertIn("--round 3", result.stderr)
        self.append("--round", "3", head=HEAD_ONE)
        self.assertEqual([event["round"] for event in self.events()], [1, 2, 3])

    def test_backfill_of_several_rounds_of_one_head_needs_an_explicit_round(self):
        self.append("--round", "1", head=HEAD_ONE)
        self.append("--round", "2", head=HEAD_ONE)
        result = self.backfill(head=HEAD_ONE, expect=2)
        self.assertIn("--round", result.stderr)
        self.backfill("--round", "2", head=HEAD_ONE)
        outcomes = [event for event in self.events() if event["kind"] == "outcome"]
        self.assertEqual([event["round"] for event in outcomes], [2])

    def test_backfill_of_a_head_whose_rounds_are_all_backfilled_says_so(self):
        self.append("--round", "1", head=HEAD_ONE)
        self.append("--round", "2", head=HEAD_ONE)
        self.backfill("--round", "1", head=HEAD_ONE)
        self.backfill("--round", "2", head=HEAD_ONE)
        result = self.backfill(head=HEAD_ONE, expect=2)
        self.assertIn("all of them are backfilled", result.stderr)

    def test_the_event_examples_of_the_module_docstring_pass_the_validator(self):
        module = load_tool()
        examples = re.findall(r"``(\{.*?\})``", module.__doc__, re.S)
        self.assertEqual(len(examples), 2, "the docstring must document one example per event kind")
        for example in examples:
            event = module.validate_event(json.loads(example))
            self.assertEqual(event["schema"], module.SCHEMA)
            self.assertEqual(event["pr"], 1751)
            self.assertEqual(event["head"], "a141b7b371bb3e3d4a158d82bdf067fc4c926aff")

    def test_bad_input_is_rejected_and_leaves_the_log_untouched(self):
        cases = [
            ("append", "--pr", "0", "--head", HEAD_ONE, "--lane", "APPROVE", "--tester", "PASS"),
            ("append", "--pr", "-3", "--head", HEAD_ONE, "--lane", "APPROVE", "--tester", "PASS"),
            ("append", "--pr", "abc", "--head", HEAD_ONE, "--lane", "APPROVE", "--tester", "PASS"),
            ("append", "--pr", "7", "--head", "zzz", "--lane", "APPROVE", "--tester", "PASS"),
            ("append", "--pr", "7", "--head", "abc", "--lane", "APPROVE", "--tester", "PASS"),
            ("append", "--pr", "7", "--head", HEAD_ONE, "--lane", "MAYBE", "--tester", "PASS"),
            ("append", "--pr", "7", "--head", HEAD_ONE, "--lane", "APPROVE", "--tester", "UNKNOWN"),
            ("append", "--pr", "7", "--head", HEAD_ONE, "--lane", "APPROVE", "--tester", "PASS",
             "--round", "0"),
            ("append", "--pr", "7", "--head", HEAD_ONE, "--lane", "APPROVE", "--tester", "PASS",
             "--recorded-at", "2026-11-02 17:33:59"),
            ("backfill", "--pr", "7", "--head", HEAD_ONE, "--outcome", "unknown"),
        ]
        for case in cases:
            with self.subTest(case=case):
                result = self.run_tool(*case, expect=2)
                self.assertTrue(result.stderr.strip())
                self.assertFalse(self.log.exists(), "a rejected command must not write the log")
        self.append()
        before = self.log.read_bytes()
        self.run_tool("append", "--pr", "1751", "--head", HEAD_ONE, "--lane", "NOPE",
                      "--tester", "PASS", expect=2)
        self.assertEqual(self.log.read_bytes(), before, "a rejected command must not change the log")

    def test_backfill_appends_the_outcome_and_names_who_was_right(self):
        self.append(lane="CHANGES", tester="FAIL")
        self.backfill("--outcome-at", "2026-11-02T18:19:28Z", outcome="re_review")
        events = self.events()
        self.assertEqual([event["kind"] for event in events], ["verdict", "outcome"])
        self.assertEqual(events[1]["outcome"], "re_review")
        self.assertEqual(events[1]["correct"], "both")
        self.assertEqual(events[1]["outcome_at"], "2026-11-02T18:19:28Z")
        listing = json.loads(self.run_tool("list", "--json").stdout)
        self.assertEqual(listing["rounds"][0]["outcome"], "re_review")

    def test_backfill_of_an_unknown_round_is_rejected_and_a_second_one_needs_force(self):
        self.append()
        self.backfill(outcome="merged")
        self.backfill(outcome="closed", expect=2)
        self.backfill("--force", outcome="closed")
        self.assertEqual([event["outcome"] for event in self.events() if event["kind"] == "outcome"],
                         ["merged", "closed"])
        listing = json.loads(self.run_tool("list", "--json").stdout)
        self.assertEqual(listing["rounds"][0]["outcome"], "closed", "the last event wins")

    def test_backfill_of_a_pr_without_a_recorded_verdict_is_rejected(self):
        self.backfill(pr=4242, expect=2)
        self.assertFalse(self.log.exists())

    def test_pending_lists_the_rounds_without_an_outcome_and_empties_after_the_backfill(self):
        self.append(head=HEAD_ONE)
        self.append(head=HEAD_TWO)
        waiting = self.pending()
        self.assertEqual(waiting["count"], 2)
        self.assertEqual([row["round"] for row in waiting["pending"]], [1, 2])
        self.backfill(head=HEAD_ONE)
        waiting = self.pending()
        self.assertEqual(waiting["count"], 1)
        self.assertEqual(waiting["pending"][0]["head"], HEAD_TWO)
        self.assertEqual(self.pending("--pr", "1751")["count"], 1)

    def test_statistics_count_the_backfilled_rounds_only(self):
        # Two backfilled rounds and one that still waits for its outcome.
        self.append(head=HEAD_ONE, lane="APPROVE", tester="PASS")
        self.backfill(head=HEAD_ONE, outcome="merged")
        self.append(head=HEAD_TWO, lane="CHANGES", tester="PASS")
        self.backfill(head=HEAD_TWO, outcome="re_review")
        self.append(head="c" * 40, lane="APPROVE", tester="PASS")
        report = self.stats()
        self.assertEqual(report["rounds"], {"total": 3, "backfilled": 2, "pending": 1, "incomplete": 0})
        self.assertEqual(report["sides"]["lane"]["samples"], 2)
        self.assertEqual(report["sides"]["lane"]["correct"], 2)
        self.assertEqual(report["sides"]["lane"]["accuracy"], 1.0)
        self.assertEqual(report["sides"]["tester"]["samples"], 2)
        self.assertEqual(report["sides"]["tester"]["correct"], 1)
        self.assertEqual(report["sides"]["tester"]["accuracy"], 0.5)
        self.assertEqual(report["agreement"], {"comparable": 2, "agreed": 1, "diverged": 1,
                                               "not_comparable": 0, "agreement_rate": 0.5})
        self.assertEqual(report["outcomes"], {"merged": 1, "closed": 0, "re_review": 1})
        self.assertEqual(len(report["pending"]), 1)
        self.assertEqual(report["pending"][0]["head"], "c" * 40)

    def test_an_insufficient_tester_verdict_abstains_instead_of_counting_as_wrong(self):
        self.append(lane="CHANGES", tester="INSUFFICIENT")
        self.backfill(outcome="closed")
        report = self.stats()
        self.assertEqual(report["sides"]["tester"]["samples"], 0)
        self.assertEqual(report["sides"]["tester"]["abstained"], 1)
        self.assertIsNone(report["sides"]["tester"]["accuracy"])
        self.assertEqual(report["sides"]["lane"]["samples"], 1)
        self.assertEqual(report["sides"]["lane"]["accuracy"], 1.0)
        self.assertEqual(report["agreement"]["not_comparable"], 1)
        self.assertEqual(report["agreement"]["comparable"], 0)
        self.assertIsNone(report["agreement"]["agreement_rate"])

    def test_the_attribution_follows_the_documented_rule_table(self):
        cases = [
            ("APPROVE", "PASS", "merged", "both"),
            ("APPROVE", "PASS", "closed", "neither"),
            ("APPROVE", "FAIL", "merged", "lane"),
            ("CHANGES", "PASS", "merged", "tester"),
            ("CHANGES", "FAIL", "re_review", "both"),
            ("CHANGES", "INSUFFICIENT", "closed", "undecided"),
        ]
        for index, (lane, tester, outcome, expected) in enumerate(cases):
            with self.subTest(lane=lane, tester=tester, outcome=outcome):
                head = "%040x" % (index + 1)
                self.append(pr=100 + index, head=head, lane=lane, tester=tester)
                self.backfill(pr=100 + index, head=head, outcome=outcome)
                events = [event for event in self.events()
                          if event["pr"] == 100 + index and event["kind"] == "outcome"]
                self.assertEqual(events[0]["correct"], expected)

    def test_the_attribution_breakdown_counts_who_was_right(self):
        cases = [
            ("APPROVE", "PASS", "merged", "both"),
            ("APPROVE", "FAIL", "merged", "lane"),
            ("CHANGES", "PASS", "merged", "tester"),
            ("CHANGES", "FAIL", "closed", "both"),
            ("APPROVE", "PASS", "closed", "neither"),
            ("CHANGES", "INSUFFICIENT", "closed", "undecided"),
        ]
        for index, (lane, tester, outcome, expected) in enumerate(cases):
            with self.subTest(lane=lane, tester=tester, outcome=outcome):
                head = "%040x" % (index + 1)
                self.append(pr=200 + index, head=head, lane=lane, tester=tester)
                self.backfill(pr=200 + index, head=head, outcome=outcome)
        report = self.stats()
        self.assertEqual(report["attribution"], {"both": 2, "lane": 1, "tester": 1,
                                                 "neither": 1, "undecided": 1})
        self.assertEqual(report["rounds"]["backfilled"], 6)

    def test_an_outcome_without_a_pair_of_verdicts_is_incomplete_and_never_a_sample(self):
        # A hand written outcome without its verdict cannot be scored: it is listed
        # as incomplete and it does not enter any denominator.
        event = {"schema": "phanes.review-divergence/1", "kind": "outcome", "pr": 1751,
                 "head": HEAD_ONE, "round": 1, "outcome": "merged",
                 "outcome_at": "2026-11-02T18:19:28Z", "correct": "both",
                 "backfilled_at": "2026-11-02T20:10:00Z"}
        self.log.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
        report = self.stats()
        self.assertEqual(report["rounds"], {"total": 1, "backfilled": 0, "pending": 0, "incomplete": 1})
        self.assertEqual(report["sides"]["lane"]["samples"], 0)
        self.assertEqual(report["sides"]["tester"]["samples"], 0)
        self.assertIsNone(report["sides"]["lane"]["accuracy"])
        self.assertEqual(len(report["incomplete"]), 1)
        self.assertEqual(report["incomplete"][0]["pr"], 1751)
        self.assertEqual(sum(report["attribution"].values()), 0)

    def test_a_malformed_line_is_reported_instead_of_being_skipped(self):
        self.append()
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        result = self.run_tool("stats", expect=2)
        self.assertIn("not JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_an_inconsistent_backfill_is_reported_by_the_statistics(self):
        self.append()
        self.backfill(outcome="merged")
        events = self.events()
        events[-1]["correct"] = "both"
        self.log.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
                            encoding="utf-8")
        report = self.stats()
        self.assertEqual(len(report["inconsistent_backfills"]), 1)
        self.assertEqual(report["inconsistent_backfills"][0]["recorded"], "both")
        self.assertEqual(report["inconsistent_backfills"][0]["computed"], "neither")

    def test_the_log_path_comes_from_the_environment_when_no_file_is_given(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ)
            environment["REVIEW_DIVERGENCE_LOG"] = str(Path(directory) / "env-log.jsonl")
            result = subprocess.run([sys.executable, str(SCRIPT), "append", "--pr", "5",
                                     "--head", HEAD_ONE, "--lane", "APPROVE", "--tester", "PASS"],
                                    capture_output=True, text=True, env=environment, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(directory) / "env-log.jsonl").exists())

    def test_the_built_in_self_test_passes(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--self-test"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
