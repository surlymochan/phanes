"""Offline contracts for the first-run gate of a paused lane (Refs #1747)."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "check-lane-first-run.py"
SPEC = importlib.util.spec_from_file_location("check_lane_first_run", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_run(root, run_id, lane, started, status="failed", error_class="exec_error", commit=None,
              error_tail=None):
    directory = Path(root) / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps({
        "id": run_id, "lane": lane, "status": status, "error_class": error_class, "commit": commit,
        "seconds": 118.4, "attempts": [{"error_class": error_class, "error_tail": error_tail}]}))
    (directory / "status.json").write_text(json.dumps({
        "lane": lane, "started": started, "finished": started + 118.4, "state": "finished"}))
    return directory


class SinceTests(unittest.TestCase):
    def test_an_iso_timestamp_and_an_epoch_are_both_read(self):
        self.assertAlmostEqual(MODULE.parse_since("2026-11-03T01:10:00Z"), 1793668200.0, places=0)
        self.assertEqual(MODULE.parse_since("1793662200"), 1793662200.0)
        self.assertIsNone(MODULE.parse_since("not a timestamp"))
        self.assertIsNone(MODULE.parse_since(None))


class GateTests(unittest.TestCase):
    def test_a_failed_run_without_a_commit_does_not_pass(self):
        passed, problems = MODULE.verdict({"status": "failed", "error_class": "exec_error"})
        self.assertFalse(passed)
        self.assertIn("status=failed (expected done)", problems)
        self.assertIn("no commit was produced", problems)
        self.assertIn("error_class=exec_error", problems)

    def test_a_done_run_with_a_commit_passes(self):
        passed, problems = MODULE.verdict({"status": "done", "error_class": None,
                                           "commit": "abc123", "error_tail": None})
        self.assertTrue(passed, problems)

    def test_the_first_run_after_the_point_is_the_one_that_is_checked(self):
        runs = [{"run_id": "a", "started": 10}, {"run_id": "b", "started": 20}, {"run_id": "c", "started": 30}]
        self.assertEqual(MODULE.first_run(runs, 15)["run_id"], "b")
        self.assertEqual(MODULE.first_run(runs, None)["run_id"], "a")
        self.assertIsNone(MODULE.first_run(runs, 40))


class EndToEndTests(unittest.TestCase):
    """The gate as the operator runs it."""

    def run_tool(self, home, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--home", str(home), *args],
                              capture_output=True, text=True, timeout=60)

    def test_a_lane_whose_first_run_after_the_point_failed_is_reported_as_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_run(root, "20261103-004712-abc123", "M9", 1000.0)
            write_run(root, "20261103-010000-def456", "M9", 2000.0, status="done", error_class=None,
                      commit="c0ffee", error_tail="ok")
            result = self.run_tool(root, "--lane", "M9", "--since", "1500")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("commit      c0ffee", result.stdout)
            self.assertIn("gate        PASS", result.stdout)

    def test_a_lane_whose_first_run_after_the_point_failed_is_reported_as_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_run(root, "20261103-010000-def456", "M9", 2000.0)
            result = self.run_tool(root, "--lane", "M9", "--since", "1500")
            self.assertEqual(result.returncode, 1)
            self.assertIn("gate        FAIL", result.stdout)
            self.assertIn("no commit was produced", result.stdout)


if __name__ == "__main__":
    unittest.main()
