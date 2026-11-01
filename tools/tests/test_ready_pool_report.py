"""Offline acceptance coverage for ready-pool-report.py (Refs #1560)."""

import importlib.util
import json
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "ready-pool-report.py"
SPEC = importlib.util.spec_from_file_location("ready_pool_report", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def card(number, *tags, body=""):
    return {"number": number, "title": f"#{number}", "body": body,
            "labels": [{"name": tag} for tag in tags]}


class ReadyPoolReportTests(unittest.TestCase):
    def setUp(self):
        self.issues = [
            card(1, "lane-ready", "backend:pi"),
            card(2, "lane-ready", "backend:pi", "in-progress"),
            card(3, "lane-ready", "backend:external"),
            card(4, "lane-ready", "backend:codex", body="依赖：#99"),
            card(5, "lane-ready"),
        ]
        self.calls = []

    def fake_gh(self, args, timeout=60):
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps(self.issues), ""
        if args[:2] == ["issue", "view"]:
            return 0, json.dumps({"state": "OPEN" if args[2] == "99" else "CLOSED"}), ""
        raise AssertionError(args)

    def fixture(self):
        return {"hosts": {"h": {"lanes": [
            {"name": "P1", "backend": "pi"}, {"name": "P2", "backend": "pi"},
            {"name": "C1", "backend": "codex"}]}}}

    def test_counts_exclusions_and_lanes_per_backend(self):
        result = MODULE.report("owner/repo", self.fixture(), {"lanes": {
            "P1": {"issue": 55}, "P2": {}, "C1": {"issue": None}}}, self.fake_gh)
        self.assertEqual(result["backends"]["pi"], {
            "dispatchable": 2, "busy": 1, "idle": 1, "gap": 0,
            "in-progress": 1, "external": 1, "dependency_open": 0})
        self.assertEqual(result["backends"]["codex"]["dispatchable"], 1)
        self.assertEqual(result["backends"]["codex"]["external"], 1)
        self.assertEqual(result["backends"]["codex"]["dependency_open"], 1)
        self.assertEqual(sum(call[:2] == ["issue", "view"] for call in self.calls), 1)

    def test_exit_code_below_target_and_at_target(self):
        args = ["--repo", "owner/repo", "--lanes", "unused", "--target", "4", "--json"]
        # Supply file input through Path.read_text without creating a fixture outside the worktree.
        original = Path.read_text
        try:
            Path.read_text = lambda path, **kw: json.dumps(self.fixture())
            self.assertEqual(MODULE.main(args, self.fake_gh), 3)
            self.assertEqual(MODULE.main(args[:-2] + ["2", "--json"], self.fake_gh), 0)
        finally:
            Path.read_text = original


if __name__ == "__main__":
    unittest.main()
