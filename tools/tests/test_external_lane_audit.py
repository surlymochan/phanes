"""Offline CLI contracts for external-lane-audit.py; fixtures never invoke gh."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "external-lane-audit.py"
NOW = 1767254400  # 2026-01-01T08:00:00Z
AGENT = "chatgpt-agent"


def issue(number=1, stage="ready", updated="2026-01-01T00:00:00Z", **extra):
    return {"number": number, "labels": [{"name": f"backend:{AGENT}"},
                                        {"name": f"lane-{stage}"}],
            "updated_at": updated, **extra}


def pr(number=10, body=None, head="fleet/chatgpt-agent-issue1-20260101-000000", **extra):
    return {"number": number, "body": body, "head": {"ref": head}, **extra}


class ExternalLaneAuditTests(unittest.TestCase):
    def run_audit(self, issues, prs, as_json=True, now=NOW):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            issues_path, prs_path = root / "issues.json", root / "prs.json"
            issues_path.write_text(json.dumps(issues), encoding="utf-8")
            prs_path.write_text(json.dumps(prs), encoding="utf-8")
            before = (issues_path.read_bytes(), prs_path.read_bytes())
            command = [sys.executable, str(SCRIPT), "--issues", str(issues_path),
                       "--prs", str(prs_path), "--agent", AGENT, "--now", str(now)]
            if as_json:
                command.append("--json")
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(before, (issues_path.read_bytes(), prs_path.read_bytes()))
        self.assertEqual(result.stderr, "")
        return result, json.loads(result.stdout) if as_json else result.stdout

    def test_normal_stages_and_sorted_rows(self):
        result, report = self.run_audit(
            [issue(4, "failed"), issue(2, "pr"), issue(1),
             issue(3, "claimed", "2026-01-01T07:00:00Z")], [pr(head="feature/test", body="Closes #2")])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["agent"], AGENT)
        self.assertEqual([row["number"] for row in report["issues"]], [1, 2, 3, 4])
        self.assertEqual([row["stage"] for row in report["issues"]],
                         ["ready", "pr", "claimed", "failed"])
        self.assertEqual(report["issues"][1]["prs"], [10])
        self.assertEqual(report["issues"][2]["idle_hours"], 1)
        self.assertEqual(report["alerts"], [])

    def test_stale_claim(self):
        result, report = self.run_audit([issue(stage="claimed")], [])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["issues"][0]["idle_hours"], 8)
        self.assertEqual(report["alerts"], [{"kind": "stale-claim", "issue": 1}])

    def test_six_hour_boundary_and_fractional_age(self):
        for age, expected in ((6, 0), (6 + 1 / 3600, 1)):
            with self.subTest(age=age):
                result, _ = self.run_audit([issue(stage="claimed")], [], now=NOW - 8 * 3600 + age * 3600)
                self.assertEqual(result.returncode, expected)

    def test_label_lag_replaces_stale_alert(self):
        result, report = self.run_audit([issue(stage="claimed")], [pr()])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["alerts"], [{"kind": "label-lag", "issue": 1, "prs": [10]}])

    def test_unlinked_pr(self):
        result, report = self.run_audit([], [pr(head="fleet/chatgpt-agent-no-issue")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["alerts"][0]["kind"], "unlinked-pr")
        self.assertEqual(report["alerts"][0]["pr"], 10)

    def test_body_wins_over_branch(self):
        result, report = self.run_audit([issue(1), issue(2, "pr")],
                                        [pr(body="Details\n\ncLoSeS #2\n")])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["issues"][0]["prs"], [])
        self.assertEqual(report["issues"][1]["prs"], [10])

    def test_missing_body_uses_branch(self):
        result, report = self.run_audit([issue(stage="pr")], [pr()])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["issues"][0]["prs"], [10])

    def test_reference_to_missing_issue_is_unlinked(self):
        result, report = self.run_audit([issue(1)], [pr(body="Closes #999")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["issues"][0]["prs"], [])
        self.assertEqual(report["alerts"][0]["kind"], "unlinked-pr")

    def test_other_backends_closed_issues_and_issue_api_prs_are_excluded(self):
        other = issue(2)
        other["labels"] = [{"name": "backend:other"}, {"name": "lane-claimed"}]
        result, report = self.run_audit(
            [other, issue(3, state="closed"), issue(4, pull_request={})],
            [pr(head="fleet/other-unknown"), pr(11, head="feature/unrelated")])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["alerts"], [])

    def test_closed_pr_does_not_hide_stale_claim(self):
        result, report = self.run_audit([issue(stage="claimed")], [pr(state="closed")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["alerts"][0]["kind"], "stale-claim")

    def test_multiple_prs_are_sorted(self):
        result, report = self.run_audit([issue(stage="pr")], [pr(12), pr(10)])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["issues"][0]["prs"], [10, 12])

    def test_table_contains_issue_age_pr_and_warning(self):
        result, output = self.run_audit([issue(stage="claimed")], [pr()], as_json=False)
        self.assertEqual(result.returncode, 1)
        for value in ("ISSUE", "STAGE", "IDLE_HOURS", "#1", "claimed", "8.00", "#10", "label-lag"):
            self.assertIn(value, output)

    def test_empty_snapshot_is_healthy(self):
        result, report = self.run_audit([], [])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report, {"agent": AGENT, "issues": [], "alerts": []})

    def test_updated_at_timezone_and_future_timestamp(self):
        result, report = self.run_audit([issue(updated="2026-01-01T16:00:00+08:00")], [])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["issues"][0]["idle_hours"], 0)
        _, report = self.run_audit([issue(updated="2026-01-02T00:00:00Z")], [])
        self.assertEqual(report["issues"][0]["idle_hours"], 0)


if __name__ == "__main__":
    unittest.main()
