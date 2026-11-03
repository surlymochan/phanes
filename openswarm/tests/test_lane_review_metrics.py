"""Lane review metrics: the rebase exclusion and the empty data path (Refs #1733).

The integration points of the card: a rebase re-review is not a formal round and it
never rewrites the verdict of the first round, and an empty review set is a report
without numbers, never an error.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import _load

xs = _load.load()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                          # the openswarm directory
SPEC = importlib.util.spec_from_file_location(
    "dispatch_review_metrics_test", os.path.join(ROOT, "lib", "dispatch", "review_metrics.py"))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
SCRIPT = os.path.join(ROOT, os.pardir, "tools", "lane-review-metrics.py")


def record(**fields):
    base = {"sha": "a" * 40, "verdict": "APPROVE", "lane": "M5", "backend": "pi",
            "at": "2026-11-03 10:00:00", "review_rounds": 1, "first_verdict": "APPROVE"}
    base.update(fields)
    return base


class RebaseExclusion(unittest.TestCase):
    """A rebase re-review is not a formal round (Refs #1733)."""

    def test_a_rebase_re_review_is_not_a_formal_round(self):
        # the dispatcher moved the head itself: the code did not change, only its base
        row = record(review_rounds=1, first_verdict="CHANGES", rebase_reviews=1, verdict="APPROVE")
        self.assertEqual(M.formal_rounds(row), (1, "record"))
        report = M.compute({"1746": row})
        self.assertEqual(report["totals"]["avg_rounds"], 1.0)
        self.assertEqual(report["totals"]["rebase_reviews"], 1)

    def test_a_rebase_re_review_does_not_turn_a_failed_first_round_into_a_pass(self):
        row = record(review_rounds=1, first_verdict="CHANGES", rebase_reviews=1, verdict="APPROVE")
        self.assertFalse(M.first_round_pass(row))
        report = M.compute({"1746": row})
        self.assertEqual(report["totals"]["first_round_pass_rate"], 0.0)
        self.assertEqual(report["lanes"]["M5"]["first_round_pass"], 0)

    def test_a_rebase_re_review_of_an_approved_pull_request_stays_a_pass(self):
        row = record(review_rounds=1, first_verdict="APPROVE", rebase_reviews=1)
        self.assertTrue(M.first_round_pass(row))
        self.assertEqual(M.compute({"1746": row})["totals"]["first_round_pass_rate"], 1.0)


class EmptyData(unittest.TestCase):
    """An empty review set is a report without numbers, never an error (Refs #1733)."""

    def test_an_empty_review_set_is_a_report_without_numbers(self):
        report = M.compute({})
        self.assertEqual(report["totals"]["reviews"], 0)
        self.assertIsNone(report["totals"]["first_round_pass_rate"])
        self.assertIsNone(report["totals"]["avg_rounds"])
        self.assertEqual(report["lanes"], {})
        self.assertEqual(report["weeks"], [])
        self.assertEqual(report["bottom_lanes"], [])
        self.assertEqual(report["skipped_total"], 0)
        self.assertIn("first_round_pass", report["definitions"])

    def test_a_missing_review_set_is_an_empty_review_set(self):
        self.assertEqual(M.compute(None)["totals"]["reviews"], 0)
        self.assertEqual(M.compute([])["totals"]["reviews"], 0)

    def test_the_script_runs_on_an_empty_review_file_and_writes_the_metrics(self):
        with tempfile.TemporaryDirectory(prefix="lane-review-metrics-") as tmp:
            reviews = os.path.join(tmp, "reviews.json")
            with open(reviews, "w", encoding="utf-8") as stream:
                json.dump({}, stream)
            result = subprocess.run([sys.executable, SCRIPT, "--reviews", reviews],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("first-round pass", result.stdout)
            out = os.path.join(tmp, M.METRICS_FILE)
            self.assertTrue(os.path.exists(out))
            with open(out, encoding="utf-8") as stream:
                report = json.load(stream)
            self.assertEqual(report["totals"]["reviews"], 0)
            self.assertEqual(report["targets"]["first_round_pass_rate"], 0.6)


class DailyBrief(unittest.TestCase):
    """The metrics are part of the daily brief (Refs #1733)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lane-review-brief-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name, data):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as stream:
            json.dump(data, stream)

    def test_the_brief_computes_the_metrics_when_the_metrics_file_is_missing(self):
        self.write("reviews.json", {"1746": record(lane="M5", backend="pi")})
        report = xs.lane_review_metrics_report(home=self.tmp)
        self.assertEqual(report["lanes"]["M5"]["reviews"], 1)
        self.assertEqual(report["lanes"]["M5"]["first_round_pass_rate"], 1.0)
        text = xs.lane_review_metrics_text(report)
        self.assertIn("first-round pass", text)
        self.assertIn("M5", text)

    def test_the_brief_reads_the_metrics_file_the_dispatcher_wrote(self):
        self.write("reviews.json", {"1746": record(lane="M5", backend="pi")})
        metrics = M.compute({"1746": record(lane="M5", backend="pi")})
        self.write(M.METRICS_FILE, metrics)
        report = xs.lane_review_metrics_report(home=self.tmp)
        self.assertEqual(report["source"], os.path.join(self.tmp, M.METRICS_FILE))
        self.assertEqual(report["lanes"]["M5"]["reviews"], 1)

    def test_the_brief_says_so_when_there_is_no_review_at_all(self):
        report = xs.lane_review_metrics_report(home=self.tmp)
        self.assertIn("no review records", xs.lane_review_metrics_text(report))

    def test_the_brief_reads_the_dispatch_state_dir_by_default(self):
        # the metrics live next to lanes-status.json: in the dispatch state dir, not
        # in the lane home
        self.write("reviews.json", {"1746": record(lane="M5", backend="pi")})
        old = os.environ.get("OPENSWARM_DISPATCH_STATE_DIR")
        os.environ["OPENSWARM_DISPATCH_STATE_DIR"] = self.tmp
        try:
            report = xs.lane_review_metrics_report()
        finally:
            if old is None:
                os.environ.pop("OPENSWARM_DISPATCH_STATE_DIR", None)
            else:
                os.environ["OPENSWARM_DISPATCH_STATE_DIR"] = old
        self.assertEqual(report["lanes"]["M5"]["reviews"], 1)
        self.assertEqual(report["source"], os.path.join(self.tmp, "reviews.json"))


if __name__ == "__main__":
    unittest.main()
