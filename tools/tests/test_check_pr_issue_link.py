"""Offline API and CLI contracts for the PR issue-link audit."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "check-pr-issue-link.py"
SPEC = importlib.util.spec_from_file_location("check_pr_issue_link", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def pr(number=1, body=None, head="fleet/test-work", **extra):
    return {"number": number, "body": body, "head": {"ref": head}, **extra}


class LinkTests(unittest.TestCase):
    def test_closing_keywords_any_line_and_case(self):
        for keyword in ("Closes", "fIxEs", "RESOLVES"):
            with self.subTest(keyword=keyword):
                self.assertEqual(MODULE.link(pr(body=f"Details\n\nThis {keyword} #123.\n")), 123)

    def test_branch_only(self):
        for lane in ("a", "chatgpt-agent", "custom_lane-2"):
            with self.subTest(lane=lane):
                self.assertEqual(MODULE.link(pr(head=f"fleet/{lane}-issue42-20261101-180000")), 42)

    def test_body_has_priority_over_conflicting_branch(self):
        self.assertEqual(MODULE.link(pr(body="Closes #8", head="fleet/test-issue9-stamp")), 8)

    def test_first_closing_reference_wins(self):
        self.assertEqual(MODULE.link(pr(body="Fixes #5\nResolves #6")), 5)

    def test_missing_and_invalid_references(self):
        for item in ({}, pr(), pr(body="Refs #7"), pr(body="Encloses #7"),
                     pr(body="Closes #0"), pr(body="Closes #7abc"),
                     pr(head="fleet/test-issue7"), pr(head="feature/test-issue7-stamp"),
                     pr(head="brain/test-issue7-stamp"), {"body": None, "head": None}):
            with self.subTest(item=item):
                self.assertIsNone(MODULE.link(item))

    def test_audit_filters_drafts_other_branches_and_closed_prs(self):
        self.assertEqual(MODULE.audit([
            pr(1, draft=True), pr(2, head="brain/draft", draft=True),
            pr(3, head="feature/unlinked"), pr(4, state="closed"),
            pr(5, body="Closes #7"), pr(6, head="fleet/test-issue7-stamp"),
            pr(7, head="brain/work", body="Resolves #7"),
        ]), [])

    def test_audit_reports_both_branch_families_in_number_order(self):
        self.assertEqual(MODULE.audit([pr(9, head="brain/no-link"), pr(2)]), [
            {"number": 2, "head": "fleet/test-work", "reason": MODULE.REASON},
            {"number": 9, "head": "brain/no-link", "reason": MODULE.REASON},
        ])


class CliTests(unittest.TestCase):
    def run_snapshot(self, snapshot, *options):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prs.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            before = path.read_bytes()
            result = subprocess.run([sys.executable, str(SCRIPT), "--prs", str(path), *options],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(path.read_bytes(), before)
            return result

    def test_json_problem_and_failure_exit(self):
        result = self.run_snapshot([pr(12)], "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), [
            {"number": 12, "head": "fleet/test-work", "reason": MODULE.REASON}])

    def test_json_healthy_and_success_exit(self):
        result = self.run_snapshot([pr(body="Fixes #9")], "--json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [])

    def test_empty_snapshot_is_healthy(self):
        result = self.run_snapshot([], "--json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [])

    def test_table_reports_number_head_and_reason(self):
        result = self.run_snapshot([pr(12, head="brain/no-link")])
        self.assertEqual(result.returncode, 1)
        for text in ("PR", "HEAD", "REASON", "#12", "brain/no-link", "missing issue link"):
            self.assertIn(text, result.stdout)

    def test_wrong_json_shape_is_input_error(self):
        result = self.run_snapshot({"number": 1}, "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("expected a JSON array", result.stderr)

    def test_self_test(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--self-test"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
