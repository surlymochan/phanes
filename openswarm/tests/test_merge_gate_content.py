"""The merge gate checks the content of a pull request (Refs #1814).

PR #1799 was merged with green checks, but the merged head carried only the
review record `.lane-review/PR-1799.md`: the functional commits stayed on a
side branch, so the merge was not a delivery.  The gate now judges the file
list of a pull request as well: a pull request without a functional change
(and without a declared artifact path) is blocked, labelled for a human and
explained in a comment.  The pull request is never closed by the gate.

The blockers of the review rounds and what answers them (B-number, changed
file:line, test, result):

B1 2nd round (CHANGES): the gate owns the needs-human label only when it
   added the label itself; a label that was set before stays.
   openswarm/bin/openswarm:15879 -> openswarm/tests/test_merge_gate_content.py:500 test_a_label_the_gate_did_not_add_is_not_removed -> pass
B2 1st round: a file list that is missing, null or unusable blocks the merge
   and warns; the gate never merges on a guess.
   openswarm/bin/openswarm:15755 -> openswarm/tests/test_merge_gate_content.py:381 test_a_null_file_list_blocks_the_merge -> pass
B3 1st round: a file without an extension in a nested code, test or tool
   directory is a delivery (`openswarm/bin/openswarm`).
   openswarm/lib/merge/content.py:167 -> openswarm/tests/test_merge_content.py:94 test_a_tool_without_a_file_extension_is_a_delivery -> pass
B4 1st round: the label and the comment are confirmed separately; a failed
   step warns and is tried again in the next round.
   openswarm/bin/openswarm:15827 -> openswarm/tests/test_merge_gate_content.py:225 test_a_failed_comment_is_tried_again_in_the_next_round -> pass
B5 1st round: the gate lifts only the label it added itself and keeps the
   escalation of another owner.
   openswarm/bin/openswarm:15891 -> openswarm/tests/test_merge_gate_content.py:468 test_the_label_of_another_owner_is_not_removed -> pass
B6 this round: a content check that cannot run blocks the merge instead of
   passing it.
   openswarm/bin/openswarm:15751 -> openswarm/tests/test_merge_gate_content.py:395 test_a_content_check_that_cannot_run_blocks_the_merge -> pass
B7 this round: `--no-content-check` is a switch: the flag took a value before,
   so the command line could not use it.
   openswarm/bin/openswarm:16666 -> openswarm/tests/test_merge_gate_content.py:430 test_the_no_content_check_flag_is_a_switch -> pass
"""
import argparse
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _load
xs = _load.load()

REPO = "acme/project"
PR = 7
HEAD_SHA = "a" * 40
OTHER_SHA = "b" * 40
ISSUE = 1787


def check(name="build", conclusion="SUCCESS"):
    """One green CI check of a pull request."""
    return {"__typename": "CheckRun", "name": name, "status": "COMPLETED",
            "conclusion": conclusion}


def pr_payload(files=None, body="", head=HEAD_SHA, labels=(), checks=None):
    """The `gh pr view --json` payload of one pull request."""
    return {"number": PR, "headRefOid": head, "headRefName": "topic",
            "labels": [{"name": name} for name in labels],
            "mergeable": "MERGEABLE", "body": body,
            "files": [{"path": path} for path in (files or [])],
            "statusCheckRollup": list(checks if checks is not None else [check()])}


class FakeGh:
    """A fake gh CLI: pr view / issue view / pr edit / pr comment / pr merge."""

    def __init__(self, payload=None, issue_body="", fail_pr_view=False,
                 fail_label=False, fail_comment=False):
        self.payload = pr_payload() if payload is None else payload
        self.issue_body = issue_body
        self.fail_pr_view = fail_pr_view
        self.fail_label = fail_label
        self.fail_comment = fail_comment
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["pr", "view"]:
            if self.fail_pr_view:
                return 1, "", "gh: could not read the pull request"
            return 0, json.dumps(self.payload), ""
        if argv[:2] == ["issue", "view"]:
            return 0, json.dumps({"body": self.issue_body}), ""
        if argv[:2] == ["pr", "edit"]:
            if self.fail_label:
                return 1, "", "gh: the label could not be added"
            return 0, "", ""
        if argv[:2] == ["pr", "comment"]:
            if self.fail_comment:
                return 1, "", "gh: the comment could not be posted"
            return 0, "", ""
        if argv[:2] == ["pr", "merge"]:
            return 0, "merged", ""
        if argv[:2] == ["pr", "list"]:
            return 0, json.dumps([{"number": PR}]), ""
        return 1, "", f"unexpected gh call: {argv}"

    def calls_of(self, prefix):
        """Every call that starts with `prefix`."""
        return [call for call in self.calls if call[:len(prefix)] == prefix]

    def merge_calls(self):
        return self.calls_of(["pr", "merge"])

    def edit_calls(self):
        return self.calls_of(["pr", "edit"])

    def comment_calls(self):
        return self.calls_of(["pr", "comment"])


class MergeGateContentTests(unittest.TestCase):
    """The gate blocks a pull request that does not deliver the card."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openswarm-merge-gate-content-")
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name, "home")
        self.home.mkdir(parents=True)
        patcher = mock.patch.object(xs, "DATA", str(self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.home / "reviews.json").write_text(json.dumps(
            {f"{REPO}#{PR}": {"sha": HEAD_SHA, "verdict": "approved"}}))

    # ------------------------------------------------------------------ utils

    def run_gate(self, gh, **flags):
        """Run the gate with the fake gh runner; returns (exit code, payload)."""
        args = argparse.Namespace(repo=REPO, pr=PR, dry_run=False, json=True)
        for key, value in flags.items():
            setattr(args, key, value)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = xs.cmd_merge_gate(args, None, runner=gh)
        return code, json.loads(buffer.getvalue())

    # ------------------------------------------------------- empty delivery

    def test_a_pull_request_with_only_a_review_record_is_blocked(self):
        """Only `.lane-review/` files: the merge is blocked and a human is asked."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 1)
        record = payload["results"][0]
        self.assertEqual(record["status"], "blocked:empty_delivery")
        self.assertIn("empty_delivery", record["reasons"])
        self.assertEqual(record["content_check"]["status"], "empty")
        self.assertEqual(record["content_check"]["artifacts"], [".lane-review/PR-7.md"])
        self.assertEqual(gh.merge_calls(), [])

    def test_a_pull_request_with_only_evidence_files_is_blocked(self):
        """Evidence artifacts are artifacts too: they are not a delivery."""
        gh = FakeGh(pr_payload(files=["docs/evidence/report.json"]))
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(payload["results"][0]["status"], "blocked:empty_delivery")
        self.assertEqual(gh.merge_calls(), [])

    def test_the_blocked_pull_request_is_labelled_and_explained(self):
        """The gate labels the PR needs-human and says why in a comment."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        self.run_gate(gh)
        edits = gh.edit_calls()
        self.assertEqual(len(edits), 1)
        self.assertIn("--add-label", edits[0])
        self.assertIn("needs-human", edits[0])
        comments = gh.comment_calls()
        self.assertEqual(len(comments), 1)
        self.assertIn("疑似空交付", comments[0][comments[0].index("--body") + 1])
        self.assertIn(".lane-review/PR-7.md", comments[0][comments[0].index("--body") + 1])

    def test_the_gate_never_closes_the_pull_request(self):
        """The check only blocks: it does not close the PR."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        self.run_gate(gh)
        self.assertEqual(gh.calls_of(["pr", "close"]), [])
        self.assertEqual(gh.merge_calls(), [])

    def test_the_empty_delivery_is_reported_once_per_head(self):
        """A second round on the same head does not comment again."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        self.run_gate(gh)
        self.run_gate(gh)
        self.assertEqual(len(gh.comment_calls()), 1)
        self.assertEqual(len(gh.edit_calls()), 1)

    def test_a_dry_run_does_not_label_and_does_not_comment(self):
        """A shadow run reports the verdict without touching the repository."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        code, payload = self.run_gate(gh, dry_run=True)
        self.assertEqual(code, 1)
        self.assertEqual(payload["results"][0]["status"], "blocked:empty_delivery")
        self.assertEqual(gh.edit_calls(), [])
        self.assertEqual(gh.comment_calls(), [])
        self.assertEqual(gh.merge_calls(), [])

    # ------------------------------------------------- escalation failures

    def test_a_failed_label_is_tried_again_in_the_next_round(self):
        """The label did not work out: the head is not remembered as reported."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        gh.fail_label = True
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 1)
        escalation = payload["results"][0]["escalation"]
        self.assertFalse(escalation["escalated"])
        self.assertFalse(escalation["label_added"])
        gh.fail_label = False
        self.run_gate(gh)
        self.assertEqual(len(gh.edit_calls()), 2)
        self.assertEqual(len(gh.comment_calls()), 2)
        self.run_gate(gh)
        # The escalation is complete now: the head is not reported again.
        self.assertEqual(len(gh.comment_calls()), 2)

    def test_a_failed_comment_is_tried_again_in_the_next_round(self):
        """The comment did not work out: the next round posts it again."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        gh.fail_comment = True
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 1)
        escalation = payload["results"][0]["escalation"]
        self.assertFalse(escalation["escalated"])
        self.assertFalse(escalation["commented"])
        gh.fail_comment = False
        self.run_gate(gh)
        self.assertEqual(len(gh.comment_calls()), 2)
        self.assertEqual(len(gh.edit_calls()), 2)

    def test_a_failed_escalation_warns_on_stderr(self):
        """A half done escalation is not silent."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        gh.fail_label = True
        buffer = io.StringIO()
        args = argparse.Namespace(repo=REPO, pr=PR, dry_run=False, json=True)
        with contextlib.redirect_stderr(buffer):
            with contextlib.redirect_stdout(io.StringIO()):
                xs.cmd_merge_gate(args, None, runner=gh)
        self.assertIn("not reported completely", buffer.getvalue())

    # -------------------------------------------------------------- delivery

    def test_a_pull_request_with_a_code_change_is_merged(self):
        """A normal pull request is merged as before."""
        gh = FakeGh(pr_payload(files=["openswarm/lib/merge/content.py"]))
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 0)
        record = payload["results"][0]
        self.assertEqual(record["status"], "mergeable")
        self.assertEqual(record["action"], "merged")
        self.assertEqual(record["content_check"]["status"], "delivery")
        self.assertEqual(len(gh.merge_calls()), 1)
        self.assertEqual(gh.edit_calls(), [])
        self.assertEqual(gh.comment_calls(), [])

    def test_a_pull_request_with_a_test_change_is_merged(self):
        """A test is a delivery too."""
        gh = FakeGh(pr_payload(files=["openswarm/tests/test_merge_gate_content.py"]))
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["action"], "merged")
        self.assertEqual(len(gh.merge_calls()), 1)

    def test_a_pull_request_with_a_tool_change_is_merged(self):
        """A tool is a delivery too."""
        gh = FakeGh(pr_payload(files=["tools/check-merge-idle-heartbeat.py"]))
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["action"], "merged")

    def test_a_tool_without_a_file_extension_is_merged(self):
        """`openswarm/bin/openswarm` is the tool of this repository: a delivery."""
        gh = FakeGh(pr_payload(files=["openswarm/bin/openswarm"]))
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["action"], "merged")
        self.assertEqual(payload["results"][0]["content_check"]["status"], "delivery")

    def test_a_nested_tool_without_a_file_extension_is_merged(self):
        """A tool of a subdirectory counts as well."""
        gh = FakeGh(pr_payload(files=["openswarm/bin/fleet-capacity"]))
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["action"], "merged")

    # ------------------------------------------------------- card declaration

    def test_a_documentation_card_is_released_by_its_declaration(self):
        """The card declares its artifact path: the pull request is merged."""
        gh = FakeGh(pr_payload(files=["docs/architecture-review-brief.md"],
                               body=f"Closes #{ISSUE}"),
                    issue_body=f"产物路径：docs/architecture-review-brief.md")
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 0)
        record = payload["results"][0]
        self.assertEqual(record["status"], "mergeable")
        self.assertEqual(record["action"], "merged")
        self.assertEqual(record["content_check"]["expected"],
                         ["docs/architecture-review-brief.md"])
        self.assertEqual(len(gh.merge_calls()), 1)
        self.assertEqual(gh.comment_calls(), [])

    def test_a_declaration_may_deliver_an_evidence_file(self):
        """The declaration wins over the artifact classification."""
        gh = FakeGh(pr_payload(files=["docs/evidence/report.json"], body=f"Closes #{ISSUE}"),
                    issue_body="产物路径：docs/evidence/report.json")
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["action"], "merged")

    def test_a_documentation_card_without_a_declaration_is_blocked(self):
        """Without a declaration there is no proof of a delivery."""
        gh = FakeGh(pr_payload(files=["docs/architecture-review-brief.md"],
                               body=f"Closes #{ISSUE}"), issue_body="改一份文档。")
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(payload["results"][0]["status"], "blocked:empty_delivery")
        self.assertEqual(payload["results"][0]["content_check"]["issue"], ISSUE)
        self.assertEqual(gh.merge_calls(), [])

    def test_an_unreadable_issue_does_not_hide_the_verdict(self):
        """A failing issue read is reported, the pull request stays blocked."""
        gh = FakeGh(pr_payload(files=["docs/architecture-review-brief.md"],
                               body=f"Closes #{ISSUE}"))
        gh.issue_body = None      # the fake answers an unreadable issue
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(payload["results"][0]["status"], "blocked:empty_delivery")

    # -------------------------------------------------------- failure modes

    def test_an_unreadable_file_list_blocks_the_merge(self):
        """The file list could not be read: the merge stays blocked and says so."""
        gh = FakeGh(fail_pr_view=True)
        buffer = io.StringIO()
        args = argparse.Namespace(repo=REPO, pr=PR, dry_run=False, json=True)
        with contextlib.redirect_stderr(buffer):
            with contextlib.redirect_stdout(io.StringIO()):
                code = xs.cmd_merge_gate(args, None, runner=gh)
        self.assertEqual(code, 1)
        self.assertIn("file list could not be read", buffer.getvalue())
        self.assertEqual(gh.merge_calls(), [])

    def test_a_file_list_that_is_not_a_list_blocks_the_merge(self):
        """An unusable file list is a failure, not an empty delivery."""
        payload = pr_payload(files=[])
        payload["files"] = "not-a-list"
        gh = FakeGh(payload)
        buffer = io.StringIO()
        args = argparse.Namespace(repo=REPO, pr=PR, dry_run=False, json=True)
        with contextlib.redirect_stderr(buffer):
            with contextlib.redirect_stdout(io.StringIO()):
                code = xs.cmd_merge_gate(args, None, runner=gh)
        self.assertEqual(code, 1)
        self.assertIn("content_unreadable", buffer.getvalue())
        self.assertEqual(gh.merge_calls(), [])

    def test_a_file_list_that_the_api_does_not_deliver_blocks_the_merge(self):
        """No file list: the gate cannot judge, so the merge stays blocked."""
        payload = pr_payload(files=[])
        payload.pop("files")
        gh = FakeGh(payload)
        buffer = io.StringIO()
        args = argparse.Namespace(repo=REPO, pr=PR, dry_run=False, json=True)
        with contextlib.redirect_stderr(buffer):
            with contextlib.redirect_stdout(io.StringIO()):
                code = xs.cmd_merge_gate(args, None, runner=gh)
        self.assertEqual(code, 1)
        self.assertIn("content_unreadable", buffer.getvalue())
        self.assertEqual(gh.merge_calls(), [])

    def test_a_null_file_list_blocks_the_merge(self):
        """A null file list is as good as no file list: the merge stays blocked."""
        payload = pr_payload(files=[])
        payload["files"] = None
        gh = FakeGh(payload)
        buffer = io.StringIO()
        args = argparse.Namespace(repo=REPO, pr=PR, dry_run=False, json=True)
        with contextlib.redirect_stderr(buffer):
            with contextlib.redirect_stdout(io.StringIO()):
                code = xs.cmd_merge_gate(args, None, runner=gh)
        self.assertEqual(code, 1)
        self.assertIn("content_unreadable", buffer.getvalue())
        self.assertEqual(gh.merge_calls(), [])

    def test_a_content_check_that_cannot_run_blocks_the_merge(self):
        """A broken content module blocks the merge instead of passing it."""
        gh = FakeGh(pr_payload(files=["openswarm/lib/merge/content.py"]))
        buffer = io.StringIO()
        args = argparse.Namespace(repo=REPO, pr=PR, dry_run=False, json=True)
        with mock.patch.object(xs, "merge_gate_content_module",
                               side_effect=OSError("no such file")):
            with contextlib.redirect_stderr(buffer):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = xs.cmd_merge_gate(args, None, runner=gh)
        self.assertEqual(code, 1)
        self.assertIn("content_unreadable", buffer.getvalue())
        self.assertEqual(gh.merge_calls(), [])

    # ------------------------------------------------------------ regressions

    def test_the_other_gate_conditions_still_block(self):
        """A red check still blocks the merge, the content check changes nothing."""
        payload = pr_payload(files=["openswarm/lib/merge/content.py"],
                             checks=[check(conclusion="FAILURE")])
        gh = FakeGh(payload)
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(payload["results"][0]["status"], "blocked:ci")
        self.assertEqual(gh.merge_calls(), [])

    def test_the_content_check_can_be_turned_off(self):
        """`--no-content-check` keeps the old behaviour of the gate."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        code, payload = self.run_gate(gh, no_content_check=True)
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["action"], "merged")
        self.assertEqual(len(gh.merge_calls()), 1)
        self.assertEqual(gh.comment_calls(), [])

    def test_the_no_content_check_flag_is_a_switch(self):
        """The command line flag turns the check off without a value."""
        captured = {}

        def fake_gate(args, policy=None, runner=None):
            captured["args"] = args
            return 0

        original = sys.argv
        try:
            sys.argv = ["openswarm", "merge-gate", "--repo", REPO, "--no-content-check"]
            with mock.patch.object(xs, "cmd_merge_gate", fake_gate), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                try:
                    xs.main()
                except SystemExit as exit_code:
                    self.assertEqual(exit_code.code, 0)
        finally:
            sys.argv = original
        self.assertTrue(captured["args"].no_content_check)

    def test_the_label_is_cleared_when_the_delivery_arrives(self):
        """A new head with the delivery lifts the label the gate added."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        self.run_gate(gh)
        self.assertEqual(len(gh.edit_calls()), 1)
        (self.home / "reviews.json").write_text(json.dumps(
            {f"{REPO}#{PR}": {"sha": OTHER_SHA, "verdict": "approved"}}))
        gh.payload = pr_payload(files=["openswarm/lib/merge/content.py"],
                                head=OTHER_SHA, labels=["needs-human"])
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["action"], "merged")
        removals = [call for call in gh.edit_calls() if "--remove-label" in call]
        self.assertEqual(len(removals), 1)
        self.assertIn("needs-human", removals[0])

    def test_the_label_of_another_owner_is_not_removed(self):
        """A CHANGES-round escalation of the dispatcher stays untouched."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        self.run_gate(gh)
        self.assertEqual(len(gh.edit_calls()), 1)
        (self.home / "reviews.json").write_text(json.dumps(
            {f"{REPO}#{PR}": {"sha": OTHER_SHA, "verdict": "approved",
                              "needs_human_reason": "rounds"}}))
        gh.payload = pr_payload(files=["openswarm/lib/merge/content.py"],
                                head=OTHER_SHA, labels=["needs-human"])
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["action"], "merged")
        removals = [call for call in gh.edit_calls() if "--remove-label" in call]
        self.assertEqual(removals, [])
        self.assertIn("another owner",
                      payload["results"][0]["escalation"]["reason"])

    def test_the_escalation_of_the_dispatcher_is_kept(self):
        """The dispatcher keys its reviews by the bare PR number: that counts too."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"]))
        self.run_gate(gh)
        (self.home / "reviews.json").write_text(json.dumps({
            f"{REPO}#{PR}": {"sha": OTHER_SHA, "verdict": "approved"},
            str(PR): {"sha": OTHER_SHA, "needs_human_reason": "rounds"}}))
        gh.payload = pr_payload(files=["openswarm/lib/merge/content.py"],
                                head=OTHER_SHA, labels=["needs-human"])
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 0)
        removals = [call for call in gh.edit_calls() if "--remove-label" in call]
        self.assertEqual(removals, [])

    def test_a_label_the_gate_did_not_add_is_not_removed(self):
        """Without its own escalation the gate never lifts the label."""
        gh = FakeGh(pr_payload(files=[".lane-review/PR-7.md"], labels=["needs-human"]))
        code, result = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertTrue(result["results"][0]["escalation"]["escalated"])
        self.assertEqual(gh.edit_calls(), [])
        self.assertEqual(len(gh.comment_calls()), 1)
        self.run_gate(gh)
        self.assertEqual(len(gh.comment_calls()), 1)
        # the delivery arrives on a new head
        (self.home / "reviews.json").write_text(json.dumps(
            {f"{REPO}#{PR}": {"sha": OTHER_SHA, "verdict": "approved"}}))
        gh.payload = pr_payload(files=["openswarm/lib/merge/content.py"],
                                head=OTHER_SHA, labels=["needs-human"])
        code, payload = self.run_gate(gh)
        self.assertEqual(code, 0)
        removals = [call for call in gh.edit_calls() if "--remove-label" in call]
        self.assertEqual(removals, [])
        self.assertIn("not added by this gate",
                      payload["results"][0]["escalation"]["reason"])


if __name__ == "__main__":
    unittest.main()
