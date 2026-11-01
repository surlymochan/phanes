"""Offline tests for the quota failure requeue utility."""
import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "requeue-quota-failed.py"
spec = importlib.util.spec_from_file_location("requeue_quota_failed", SCRIPT)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

TODAY = """06:13 M5 ended #1497 without a commit (run run-a, failed/rate_limit) → labelled lane-failed
06:13 M6 ended #1500 without a commit (run run-b, failed/quota) → labelled lane-failed
06:20 dispatched #1497 → M5 (host) run run-c
06:30 operator repaired #1497
07:00 M7 ended #1501 without a commit (run run-d, failed/server_error) → labelled lane-failed
"""


class RequeueQuotaFailedTests(unittest.TestCase):
    def test_latest_dispatch_and_supported_errors(self):
        rows = tool.plan(TODAY)
        self.assertEqual([row["number"] for row in rows], [1500])
        self.assertEqual(tool.plan(TODAY, "06:13")[0]["number"], 1500)

    def test_dry_run_verifies_but_never_edits(self):
        calls = []

        class Result:
            returncode = 0
            stdout = '{"state":"OPEN","labels":[{"name":"lane-failed"}]}'

        def runner(args):
            calls.append(args)
            return Result()

        row = tool.plan(TODAY)[0]
        tool.execute("owner/repo", [row], runner=runner, output=lambda _: None)
        self.assertEqual(len(calls), 1)
        self.assertIn("view", calls[0])

    def test_closed_and_relabelled_issues_are_skipped(self):
        fixtures = [('{"state":"CLOSED","labels":[{"name":"lane-failed"}]}'),
                    ('{"state":"OPEN","labels":[{"name":"lane-ready"}]}')]
        calls = []

        class Result:
            returncode = 0

        def runner(args):
            calls.append(args)
            result = Result()
            result.stdout = fixtures.pop(0)
            return result

        row = tool.plan(TODAY)[0]
        for _ in range(2):
            tool.execute("owner/repo", [row], apply=True, runner=runner, output=lambda _: None)
        self.assertEqual(len(calls), 2)

    def test_since_filters_earlier_events(self):
        self.assertEqual(tool.plan(TODAY, "06:14"), [])

    def test_latest_non_quota_failure_replaces_quota_failure(self):
        log = """06:13 M5 ended #1502 without a commit (run run-a, failed/rate_limit) → labelled lane-failed
06:20 M5 ended #1502 without a commit (run run-b, failed/server_error) → labelled lane-failed
"""
        self.assertEqual(tool.plan(log), [])


if __name__ == "__main__":
    unittest.main()
