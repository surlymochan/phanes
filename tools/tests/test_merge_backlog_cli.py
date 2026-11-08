"""Offline CLI contracts for merge-backlog.py; the fixtures never touch GitHub.

The CLI is the health check section "合并积压": it prints the queue, names the
rows that are over their threshold and can append those names to a brain log
(Refs #1696).
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "merge-backlog.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "merge-backlog-2026-11-02"
NOW = "2026-11-02T21:05:00+08:00"


class MergeBacklogCliTests(unittest.TestCase):
    def run_tool(self, *options, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("gh-lane-prs.json", "gh-lane-issues.json", "reviews.json"):
                (root / name).write_text((FIXTURE / name).read_text(encoding="utf-8"),
                                         encoding="utf-8")
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            command = [sys.executable, str(SCRIPT),
                       "--prs", str(root / "gh-lane-prs.json"),
                       "--issues", str(root / "gh-lane-issues.json"),
                       "--reviews", str(root / "reviews.json"),
                       "--now", NOW, "--tz", "+08:00", *options]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            after = {path.name: path.read_bytes() for path in root.iterdir()}
            self.assertEqual(before, after, "the tool must not write its inputs")
            return result, root

    def test_the_queue_is_printed_with_its_stages(self):
        result, _root = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stderr)   # rows over the threshold
        self.assertEqual(result.stderr, "")
        for text in ("#1632", "等人工裁决（薄卡豁免）", "停留26h02m", "#1633", "已批待合并"):
            self.assertIn(text, result.stdout)

    def test_the_merged_pull_request_is_not_in_the_queue(self):
        result, _root = self.run_tool()
        self.assertNotIn("#1635", result.stdout)

    def test_the_rows_over_the_threshold_are_named(self):
        result, _root = self.run_tool()
        self.assertIn("合并积压点名 #1632 等人工裁决（薄卡豁免） 停留26h02m（阈值 24h00m）",
                      result.stdout)
        self.assertIn("合并积压点名 #1633 已批待合并 停留3h00m（阈值 2h00m）", result.stdout)

    def test_a_pull_request_that_just_turned_green_is_not_named(self):
        """#1639 was approved three hours before the checks turned green."""
        result, _root = self.run_tool()
        self.assertIn("#1639", result.stdout)
        self.assertNotIn("合并积压点名 #1639", result.stdout)

    def test_the_json_output_carries_the_rows(self):
        result, _root = self.run_tool("--json")
        rows = json.loads(result.stdout)
        numbers = [row["number"] for row in rows]
        self.assertEqual(numbers, [1632, 1633, 1634, 1637, 1638, 1639])
        self.assertEqual(rows[0]["stage"], "等人工裁决")
        self.assertEqual(rows[0]["issue"], 1626)

    def test_the_brain_log_names_the_rows_over_the_threshold(self):
        """The naming lines are what the brain log gets."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("gh-lane-prs.json", "gh-lane-issues.json", "reviews.json"):
                (root / name).write_text((FIXTURE / name).read_text(encoding="utf-8"),
                                         encoding="utf-8")
            log = root / "brain.log"
            result = subprocess.run(
                [sys.executable, str(SCRIPT),
                 "--prs", str(root / "gh-lane-prs.json"),
                 "--issues", str(root / "gh-lane-issues.json"),
                 "--reviews", str(root / "reviews.json"),
                 "--now", NOW, "--tz", "+08:00",
                 "--brain-log", str(log)],
                capture_output=True, text=True, check=False)
            written = log.read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("合并积压 @ 2026-11-02T13:05:00+00:00 count=6 超时=3", written)
        self.assertIn("合并积压点名 #1632 等人工裁决（薄卡豁免） 停留26h02m（阈值 24h00m）",
                      written)
        self.assertIn("合并积压点名 #1633 已批待合并 停留3h00m（阈值 2h00m）", written)
        self.assertNotIn("#1639", written)

    def test_a_missing_input_is_an_input_error(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--prs", "/nope.json",
                                 "--issues", "/nope.json"], capture_output=True, text=True,
                                check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("nope.json", result.stderr)

    def test_the_self_test(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--self-test"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("merge backlog self-test", result.stdout)

    def test_the_self_test_does_not_depend_on_the_host_timezone(self):
        """The fleet host runs on UTC+8: the self test has to hold there too."""
        result = subprocess.run([sys.executable, str(SCRIPT), "--self-test"],
                                capture_output=True, text=True, check=False,
                                env={**os.environ, "TZ": "Asia/Shanghai"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("merge backlog self-test", result.stdout)


if __name__ == "__main__":
    unittest.main()
