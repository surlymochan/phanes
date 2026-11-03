"""Offline tests for the quota failure requeue utility."""
import importlib.util
import json
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


class FakeGh:
    """A `gh` stand-in that records the calls and answers the open PR query."""

    def __init__(self, issue='{"state":"OPEN","labels":[{"name":"lane-failed"}]}', prs=None, pr_error=None):
        self.issue = issue
        self.prs = prs or []
        self.pr_error = pr_error
        self.calls = []

    def __call__(self, args, payload=None):
        self.calls.append(list(args))
        result = type("Result", (), {})()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        if args[:2] == ["issue", "view"]:
            result.stdout = self.issue
        elif args[:3] == ["api", "graphql", "--input"]:
            if self.pr_error:
                result.returncode = 1
                result.stderr = self.pr_error
            else:
                result.stdout = json.dumps({"data": {"repository": {"pullRequests": {
                    "nodes": self.prs, "pageInfo": {"hasNextPage": False}}}}})
        return result

    def edits(self):
        return [call for call in self.calls if call[:2] == ["issue", "edit"]]


def added_labels(edit):
    """The labels an `issue edit` call adds."""
    labels = []
    for index, arg in enumerate(edit):
        if arg == "--add-label" and index + 1 < len(edit):
            labels.append(edit[index + 1])
    return labels


class RequeueQuotaFailedTests(unittest.TestCase):
    def test_latest_dispatch_and_supported_errors(self):
        rows = tool.plan(TODAY)
        self.assertEqual([row["number"] for row in rows], [1500])
        self.assertEqual(tool.plan(TODAY, "06:13")[0]["number"], 1500)

    def test_dry_run_verifies_but_never_edits(self):
        gh = FakeGh()

        tool.execute("owner/repo", tool.plan(TODAY), runner=gh, output=lambda _: None)

        self.assertEqual(len(gh.edits()), 0, "the dry run must not edit the labels")
        self.assertTrue(any(call[:2] == ["issue", "view"] for call in gh.calls))
        self.assertTrue(any(call[:3] == ["api", "graphql", "--input"] for call in gh.calls),
                        "the dry run must read the open PRs")

    def test_closed_and_relabelled_issues_are_skipped(self):
        fixtures = ['{"state":"CLOSED","labels":[{"name":"lane-failed"}]}',
                    '{"state":"OPEN","labels":[{"name":"lane-ready"}]}']
        calls = []

        class Result:
            returncode = 0

        def runner(args, payload=None):
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


class OpenPullRequestGuardTests(unittest.TestCase):
    """Refs #1772: a card with an open pull request is delivered and is not requeued."""

    PR = {"number": 1768, "title": "#1500 fix", "body": "Closes #1500",
          "headRefName": "fleet/M5-issue1500-run-b",
          "closingIssuesReferences": {"nodes": [{"number": 1500}]}}

    def test_a_card_with_an_open_pr_is_not_requeued(self):
        gh = FakeGh(prs=[self.PR])
        messages = []

        changed = tool.execute("owner/repo", tool.plan(TODAY), apply=True, runner=gh, output=messages.append)

        self.assertEqual(changed, [], "a card with an open PR must not be requeued")
        for edit in gh.edits():
            self.assertNotIn("lane-ready", added_labels(edit),
                             "no edit may put the card back into lane-ready")
        self.assertTrue(any("open PR #1768" in message for message in messages),
                        f"the skip reason must be visible: {messages}")

    def test_the_delivery_state_is_repaired_to_lane_pr(self):
        class Runner(FakeGh):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.edited = False

            def __call__(self, args, payload=None):
                if args[:2] == ["issue", "edit"]:
                    self.edited = True
                if args[:2] == ["issue", "view"]:
                    self.calls.append(list(args))
                    result = type("Result", (), {})()
                    result.returncode = 0
                    result.stderr = ""
                    result.stdout = ('{"state":"OPEN","labels":[{"name":"lane-pr"}]}' if self.edited else
                                      '{"state":"OPEN","labels":[{"name":"lane-failed"},{"name":"lane-ready"}]}')
                    return result
                return super().__call__(args, payload)

        runner = Runner(prs=[self.PR])
        tool.execute("owner/repo", tool.plan(TODAY), apply=True, runner=runner, output=lambda _: None)

        edits = runner.edits()
        self.assertTrue(edits, "the lane-pr terminal state must be repaired")
        self.assertIn("lane-pr", added_labels(edits[0]))
        self.assertNotIn("lane-ready", added_labels(edits[0]))

    def test_a_failed_open_pr_lookup_blocks_the_requeue(self):
        gh = FakeGh(pr_error="gh: connection reset")

        changed = tool.execute("owner/repo", tool.plan(TODAY), apply=True, runner=gh, output=lambda _: None)

        self.assertEqual(changed, [], "an unknown open PR state must not requeue the card")
        self.assertEqual(gh.edits(), [], "no label edit may happen without a readable open PR state")


if __name__ == "__main__":
    unittest.main()
