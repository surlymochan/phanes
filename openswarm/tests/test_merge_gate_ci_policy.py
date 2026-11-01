"""Merge-gate CI policy stays aligned with dispatch and rerun classification."""
import json
from unittest import mock

from test_merge_gate import (HEAD_SHA, REPO, FakeGh, MergeGateTests, check, pr_payload, xs)


class MergeGateCiPolicyTests(MergeGateTests):
    def payload(self, checks):
        return pr_payload(7, checks=checks)

    def run_checks(self, checks, ci_policy):
        self.write_reviews({f"{REPO}#7": {"sha": HEAD_SHA, "verdict": "approve"}})
        gh = FakeGh({7: self.payload(checks)})
        with mock.patch.object(xs, "load_policy", return_value={"ci": ci_policy}):
            code, results = self.run_gate(gh)
        return code, results[0]

    def test_ignored_drift_red_does_not_block_after_review(self):
        code, result = self.run_checks(
            [check("upstream drift", "FAILURE"), check("Linux", "SUCCESS"),
             {"context": "lane-review", "state": "FAILURE"}],
            {"ignore_red_re": "upstream"})
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "mergeable")
        self.assertEqual(result["checks"]["ignored_names"], ["upstream drift"])
        self.assertEqual(result["checks"]["total"], 2)

    def test_ignored_pending_macos_check_does_not_block(self):
        code, result = self.run_checks(
            [check("Linux", "SUCCESS"), check("macOS-14 tests", "", "IN_PROGRESS")],
            {"ignore_red_re": "macos"})
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "mergeable")

    def test_non_ignored_red_check_blocks(self):
        code, result = self.run_checks(
            [check("Linux", "SUCCESS"), check("required build", "FAILURE")],
            {"ignore_red_re": "macos|upstream"})
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "blocked:ci")
        self.assertEqual(result["checks"]["failed_names"], ["required build"])

    def test_missing_policy_keeps_legacy_check_counts(self):
        counts = xs.merge_gate_check_counts([
            check("drift", "FAILURE"), check("waiting", "", "IN_PROGRESS")], {})
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(counts["ignored_names"], [])

