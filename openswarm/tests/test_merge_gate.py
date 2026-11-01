"""The merge gate merges only pull requests whose reviewed head sha is still current."""
import argparse
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import _load
xs = _load.load()

REPO = "acme/project"
HEAD_SHA = "a" * 40
OTHER_SHA = "b" * 40


def check(name, conclusion="SUCCESS", status="COMPLETED"):
    """One CI check of a pull request."""
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def pr_payload(number, head=HEAD_SHA, labels=(), checks=()):
    """The `gh pr view --json` payload of one pull request."""
    return {"number": number, "headRefOid": head,
            "labels": [{"name": name} for name in labels],
            "mergeable": "MERGEABLE",
            "statusCheckRollup": list(checks)}


class FakeGh:
    """A fake gh CLI: answers pr list / pr view and records every call."""

    def __init__(self, prs, listed=None, errors=None):
        self.prs = prs
        self.listed = sorted(prs) if listed is None else listed
        self.errors = errors or {}
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["pr", "list"]:
            return 0, json.dumps([{"number": number} for number in self.listed]), ""
        if argv[:2] == ["pr", "view"]:
            number = int(argv[2])
            if number in self.errors:
                return 1, "", self.errors[number]
            payload = self.prs.get(number)
            if payload is None:
                return 1, "", f"no pull request #{number}"
            return 0, json.dumps(payload), ""
        if argv[:2] == ["pr", "merge"]:
            return 0, "merged", ""
        return 1, "", f"unexpected gh call: {argv}"

    def merge_calls(self):
        return [call for call in self.calls if call[:2] == ["pr", "merge"]]


FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["GH_CALL_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
if argv[:2] == ["pr", "view"]:
    print(json.dumps(json.loads(os.environ["GH_PR_VIEW"])))
elif argv[:2] == ["pr", "list"]:
    print(json.dumps(json.loads(os.environ["GH_PR_LIST"])))
else:
    print("merged")
"""


class MergeGateTests(unittest.TestCase):
    """A pull request is merged only when the reviewed head sha, CI, and labels agree."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openswarm-merge-gate-")
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name, "home")
        self.home.mkdir(parents=True)
        patcher = mock.patch.object(xs, "DATA", str(self.home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_reviews(self, records):
        (self.home / "reviews.json").write_text(json.dumps(records))

    def run_gate(self, gh, **flags):
        """Run the gate with the fake gh runner and return (exit code, results)."""
        args = argparse.Namespace(repo=REPO, pr=None, dry_run=False, json=True)
        for key, value in flags.items():
            setattr(args, key, value)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = xs.cmd_merge_gate(args, None, runner=gh)
        return code, json.loads(buffer.getvalue())["results"]

    def test_reviewed_sha_differs_from_the_head_sha_blocks_the_merge(self):
        """The lane reviewed another commit: the PR head moved on."""
        self.write_reviews({f"{REPO}#7": {"sha": OTHER_SHA}})
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:stale_review")
        self.assertEqual(results[0]["head_sha"], HEAD_SHA)
        self.assertEqual(results[0]["reviewed_sha"], OTHER_SHA)
        self.assertEqual(gh.merge_calls(), [])

    def test_a_failed_ci_check_blocks_the_merge(self):
        """CI is not green, so the PR stays open."""
        self.write_reviews({f"{REPO}#7": {"sha": HEAD_SHA}})
        gh = FakeGh({7: pr_payload(7, checks=[check("build"), check("test", conclusion="FAILURE")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:ci")
        self.assertEqual(results[0]["checks"]["failed"], 1)
        self.assertEqual(gh.merge_calls(), [])

    def test_a_pending_ci_check_blocks_the_merge(self):
        """A still running check is not a green check."""
        self.write_reviews({f"{REPO}#7": {"sha": HEAD_SHA}})
        gh = FakeGh({7: pr_payload(7, checks=[check("build", conclusion="", status="IN_PROGRESS")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:ci_pending")
        self.assertEqual(gh.merge_calls(), [])

    def test_a_needs_changes_label_blocks_the_merge(self):
        """The reviewer asked for changes, so the PR is not merged."""
        self.write_reviews({f"{REPO}#7": {"sha": HEAD_SHA}})
        gh = FakeGh({7: pr_payload(7, labels=["needs-changes"], checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:label")
        self.assertEqual(results[0]["blocking_labels"], ["needs-changes"])
        self.assertEqual(gh.merge_calls(), [])

    def test_a_needs_changes_label_blocks_the_merge_without_a_review_record(self):
        """An explicit needs-changes label blocks the merge on its own."""
        gh = FakeGh({7: pr_payload(7, labels=["Needs Changes"], checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:label")
        self.assertEqual(gh.merge_calls(), [])

    def test_the_reasons_are_reason_codes_and_the_failed_checks_are_named(self):
        """The status names the first reason, the reasons list names all of them."""
        gh = FakeGh({7: pr_payload(7, labels=["needs-changes"],
                                   checks=[check("build"), check("test", conclusion="FAILURE")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:label")
        self.assertEqual(results[0]["reasons"], ["label", "ci", "no_review"])
        self.assertEqual(results[0]["checks"]["failed_names"], ["test"])
        self.assertEqual(results[0]["checks"]["total"], 2)

    def test_a_missing_review_blocks_the_merge(self):
        """Without a review record there is no reviewed head sha at all."""
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:no_review")
        self.assertEqual(gh.merge_calls(), [])

    def test_a_changes_verdict_blocks_the_merge(self):
        """A reviewed but not approved PR is not merged."""
        self.write_reviews({f"{REPO}#7": {"sha": HEAD_SHA, "verdict": "changes"}})
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:verdict")
        self.assertEqual(results[0]["reasons"], ["verdict"])
        self.assertEqual(results[0]["review_verdict"], "changes")
        self.assertEqual(gh.merge_calls(), [])

    def test_an_approved_verdict_keeps_the_pr_mergeable(self):
        """An approved review of the current head sha merges as usual."""
        self.write_reviews({f"{REPO}#7": {"sha": HEAD_SHA, "verdict": "approve"}})
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 0)
        self.assertEqual(results[0]["status"], "mergeable")
        self.assertEqual(len(gh.merge_calls()), 1)

    def test_the_review_of_another_repository_does_not_release_the_pr(self):
        """A record of another repository never releases this PR."""
        self.write_reviews({"7": {"sha": HEAD_SHA, "repo": "other/project"}})
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:no_review")
        self.assertEqual(gh.merge_calls(), [])

    def test_a_bare_number_key_without_a_repository_does_not_release_the_pr(self):
        """A bare number key is ambiguous: without a repository it counts for nobody."""
        self.write_reviews({"7": {"sha": HEAD_SHA}})
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:no_review")
        self.assertEqual(gh.merge_calls(), [])

    def test_a_bare_number_key_of_the_same_repository_is_read(self):
        """A bare number key that names this repository is a valid record."""
        self.write_reviews({"7": {"sha": HEAD_SHA, "repo": REPO}})
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 0)
        self.assertEqual(results[0]["status"], "mergeable")
        self.assertEqual(len(gh.merge_calls()), 1)

    def test_a_review_record_of_another_repository_in_a_list_does_not_release_the_pr(self):
        """The list form is checked against the repository as well."""
        self.write_reviews([{"repo": "other/project", "pr": 7, "sha": HEAD_SHA}])
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:no_review")
        self.assertEqual(gh.merge_calls(), [])

    def test_a_list_record_without_a_repository_does_not_release_the_pr(self):
        """A list record must name the repository: without it the number is ambiguous."""
        self.write_reviews([{"pr": 7, "sha": HEAD_SHA}])
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:no_review")
        self.assertEqual(gh.merge_calls(), [])

    def test_a_pr_without_any_check_is_not_green(self):
        """No check at all is not a green CI run."""
        self.write_reviews({f"{REPO}#7": {"sha": HEAD_SHA}})
        gh = FakeGh({7: pr_payload(7, checks=[])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["status"], "blocked:no_checks")
        self.assertEqual(results[0]["checks"]["total"], 0)
        self.assertEqual(gh.merge_calls(), [])

    def test_a_gh_failure_on_one_pr_does_not_hide_the_other_verdicts(self):
        """One unreadable PR is blocked on its own; the other PRs still get a verdict."""
        self.write_reviews({f"{REPO}#8": {"sha": HEAD_SHA}})
        gh = FakeGh({8: pr_payload(8, checks=[check("build")])}, listed=[7, 8],
                    errors={7: "ghp_secrettoken12345678 not allowed"})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual([record["status"] for record in results],
                         ["blocked:gh_error", "mergeable"])
        self.assertIn("[REDACTED]", results[0]["error"])
        self.assertNotIn("ghp_", results[0]["error"])
        self.assertEqual(len(gh.merge_calls()), 1)

    def test_a_reviewed_green_pr_is_merged_with_the_reviewed_head_commit(self):
        """Everything agrees: the PR is squash merged, pinned to the reviewed sha."""
        self.write_reviews({f"{REPO}#7": {"sha": HEAD_SHA}})
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 0)
        self.assertEqual(results[0]["status"], "mergeable")
        self.assertEqual(results[0]["action"], "merged")
        merges = gh.merge_calls()
        self.assertEqual(len(merges), 1)
        self.assertIn("--squash", merges[0])
        self.assertIn("--match-head-commit", merges[0])
        self.assertEqual(merges[0][merges[0].index("--match-head-commit") + 1], HEAD_SHA)

    def test_the_dry_run_reports_the_verdict_without_merging(self):
        """A shadow run must not touch the repository."""
        self.write_reviews({f"{REPO}#7": {"sha": HEAD_SHA}})
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(results[0]["status"], "mergeable")
        self.assertEqual(results[0]["action"], "dry_run")
        self.assertEqual(gh.merge_calls(), [])

    def test_every_open_pr_of_the_repo_is_judged(self):
        """Without --pr the gate judges every open pull request of the repo."""
        self.write_reviews({f"{REPO}#7": {"sha": HEAD_SHA},
                            f"{REPO}#8": {"sha": OTHER_SHA}})
        gh = FakeGh({7: pr_payload(7, checks=[check("build")]),
                     8: pr_payload(8, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 1)
        self.assertEqual([record["status"] for record in results],
                         ["mergeable", "blocked:stale_review"])
        self.assertEqual(len(gh.merge_calls()), 1)

    def test_a_review_record_may_use_a_list_of_records(self):
        """The review file may hold a list of records instead of a keyed object."""
        self.write_reviews([{"repo": REPO, "pr": 7, "sha": HEAD_SHA}])
        gh = FakeGh({7: pr_payload(7, checks=[check("build")])})
        code, results = self.run_gate(gh)
        self.assertEqual(code, 0)
        self.assertEqual(results[0]["status"], "mergeable")
        self.assertEqual(len(gh.merge_calls()), 1)

    def test_the_cli_merges_through_the_gh_binary_and_reports_blocked_prs(self):
        """The command line path uses the gh binary and reports the exit code."""
        fake_gh = pathlib.Path(self.temp.name, "gh")
        fake_gh.write_text(FAKE_GH)
        fake_gh.chmod(0o755)
        call_log = pathlib.Path(self.temp.name, "gh-calls.json")
        (self.home / "reviews.json").write_text(json.dumps({f"{REPO}#7": {"sha": OTHER_SHA}}))
        env = dict(os.environ, OPENSWARM_HOME=str(self.home), OPENSWARM_GH_BIN=str(fake_gh),
                   GH_CALL_LOG=str(call_log),
                   GH_PR_VIEW=json.dumps(pr_payload(7, checks=[check("build")])),
                   GH_PR_LIST=json.dumps([{"number": 7}]))
        run = subprocess.run([sys.executable, str(ROOT / "bin" / "openswarm"), "merge-gate",
                              "--repo", REPO, "--pr", "7", "--json"],
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        results = json.loads(run.stdout)["results"]
        self.assertEqual(results[0]["status"], "blocked:stale_review")
        calls = [json.loads(line) for line in call_log.read_text().splitlines()]
        self.assertFalse([call for call in calls if call[:2] == ["pr", "merge"]])


if __name__ == "__main__":
    unittest.main()
