"""Offline acceptance coverage for ready-pool-report.py (Refs #1560)."""

import importlib.util
import json
from pathlib import Path
import tempfile
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
            card(4, "lane-ready", "backend:codex", body="Refs #1587\nDepends on: #99"),
            card(5, "lane-ready", body="Refs #1234"),
            card(6, "lane-ready", "backend:codex", "lane-pr"),
            card(7, "lane-ready", "backend:codex", "lane-failed", body=None),
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

    def test_real_snapshot_exclusions_and_dependency_line(self):
        status = {"P1": "busy #980", "P2": "idle", "C1": "?", "_busy": 1}
        result = MODULE.report("owner/repo", self.fixture(), status, self.fake_gh)
        self.assertEqual(result["backends"]["pi"]["busy"], 1)
        self.assertEqual(result["backends"]["pi"]["idle"], 1)
        self.assertEqual(result["backends"]["pi"]["gap"], 0)
        self.assertEqual(result["backends"]["pi"]["in-progress"], 1)
        self.assertEqual(result["backends"]["pi"]["external"], 1)
        self.assertEqual(result["backends"]["pi"]["dispatchable"], 2)

        self.assertEqual(result["backends"]["codex"]["busy"], 0)
        self.assertEqual(result["backends"]["codex"]["idle"], 0)
        self.assertEqual(result["backends"]["codex"]["gap"], 0)
        self.assertEqual(result["backends"]["codex"]["unknown"], 1)
        self.assertEqual(result["backends"]["codex"]["dependency_open"], 1)
        self.assertEqual(result["backends"]["codex"]["in-progress"], 2)
        self.assertEqual(result["backends"]["codex"]["external"], 1)
        self.assertEqual(result["backends"]["codex"]["dispatchable"], 1)

        self.assertEqual(result["dispatchable"], 2)
        self.assertEqual(sum(call[:2] == ["issue", "view"] for call in self.calls), 1)
        listing = next(call for call in self.calls if call[:2] == ["issue", "list"])
        self.assertIn("--label", listing)
        self.assertIn("lane-ready", listing)

    def test_claimed_excluded_and_drain_or_error_not_idle(self):
        issues = [
            card(10, "lane-ready", "lane-claimed"),
            card(11, "lane-ready", "backend:chatgpt-agent"),
            card(12, "lane-ready", "backend:pi"),
        ]

        def fake_gh(args, timeout=60):
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps(issues), ""
            raise AssertionError(args)

        inventory = {"hosts": {"h": {"lanes": [
            {"name": "P1", "backend": "pi", "drain": True},
            {"name": "P2", "backend": "pi"},
            {"name": "C1", "backend": "codex"},
        ]}}}
        status = {"P1": "idle", "P2": "ssh status timeout", "C1": "unknown"}
        result = MODULE.report("owner/repo", inventory, status, fake_gh)

        # lane-claimed and external agent card excluded as external for all backends
        self.assertEqual(result["backends"]["pi"]["external"], 2)
        self.assertEqual(result["backends"]["codex"]["external"], 2)
        self.assertEqual(result["dispatchable"], 1)
        self.assertEqual(result["backends"]["pi"]["dispatchable"], 1)
        self.assertEqual(result["backends"]["codex"]["dispatchable"], 0)

        # P1 has drain=True so not idle; P2 has ssh timeout so not idle; C1 is unknown so not idle
        self.assertEqual(result["backends"]["pi"]["idle"], 0)
        self.assertEqual(result["backends"]["pi"]["busy"], 0)
        self.assertEqual(result["backends"]["pi"]["gap"], 0)
        self.assertEqual(result["backends"]["codex"]["idle"], 0)
        self.assertEqual(result["backends"]["codex"]["busy"], 0)
        self.assertEqual(result["backends"]["codex"]["gap"], 0)

    def test_status_omitted_reports_unknown(self):
        result = MODULE.report("owner/repo", self.fixture(), None, self.fake_gh)
        self.assertEqual(result["backends"]["pi"]["busy"], "未知")
        self.assertEqual(result["backends"]["pi"]["idle"], "未知")
        self.assertEqual(result["backends"]["pi"]["gap"], "未知")
        self.assertEqual(result["backends"]["codex"]["busy"], "未知")
        self.assertEqual(result["backends"]["codex"]["idle"], "未知")
        self.assertEqual(result["backends"]["codex"]["gap"], "未知")
        self.assertEqual(result["backends"]["pi"]["unknown"], "未知")

    def test_inventory_backend_is_internal_and_paused_lane_is_not_idle(self):
        issues = [card(20, "lane-ready", "backend:cursor")]
        def fake_gh(args, timeout=60):
            return (0, json.dumps(issues), "")

        inventory = {"hosts": {"h": {"lanes": [
            {"name": "U1", "backend": "cursor"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "PAUSE-U1").touch()
            result = MODULE.report("owner/repo", inventory, {"U1": "idle"},
                                   fake_gh, Path(directory))
        cursor = result["backends"]["cursor"]
        self.assertEqual(cursor["dispatchable"], 1)
        self.assertEqual(cursor["external"], 0)
        self.assertEqual(cursor["idle"], 0)
        self.assertEqual(cursor["gap"], 0)

    def test_host_pause_and_mint_marker(self):
        inventory = {"hosts": {"h": {"ssh": "alias", "lanes": [
            {"name": "U1", "backend": "cursor"},
            {"name": "MINT", "backend": "cursor"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "PAUSE-MINT").touch()
            result = MODULE.report("owner/repo", inventory, {"U1": "idle", "MINT": "idle"},
                                   self.fake_gh, state)
            self.assertEqual(result["backends"]["cursor"]["idle"], 2)
            for host in ("h", "alias"):
                marker = state / ("PAUSE-HOST-" + host)
                marker.touch()
                result = MODULE.report("owner/repo", inventory, {"U1": "idle", "MINT": "idle"},
                                       self.fake_gh, state)
                self.assertEqual(result["backends"]["cursor"]["idle"], 0)
                self.assertEqual(result["backends"]["cursor"]["gap"], 0)
                marker.unlink()

    def test_absent_internal_backend_can_fall_back(self):
        self.issues = [card(21, "lane-ready", "backend:cursor")]
        result = MODULE.report("owner/repo", self.fixture(), {}, self.fake_gh)
        self.assertEqual(result["dispatchable"], 1)
        self.assertEqual(result["backends"]["pi"]["external"], 0)
        self.assertEqual(result["backends"]["pi"]["dispatchable"], 1)

    def test_exit_code_below_target_and_at_target(self):
        args = ["--repo", "owner/repo", "--lanes", "unused", "--target", "4", "--json"]
        original = Path.read_text
        try:
            Path.read_text = lambda path, **kw: json.dumps(self.fixture())
            self.assertEqual(MODULE.main(args, self.fake_gh), 3)
            self.assertEqual(MODULE.main(args[:-2] + ["2", "--json"], self.fake_gh), 0)
            args_text = ["--repo", "owner/repo", "--lanes", "unused", "--target", "2"]
            self.assertEqual(MODULE.main(args_text, self.fake_gh), 0)
        finally:
            Path.read_text = original


if __name__ == "__main__":
    unittest.main()
