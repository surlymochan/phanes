"""Offline CLI contracts for merge-backlog.py; the fixtures never touch GitHub."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "merge-backlog.py"
FIXTURE = Path(__file__).resolve().parents[2] / "openswarm" / "tests" / "fixtures" / \
    "merge-backlog-2026-11-02"
NOW = "2026-11-02T21:05:00+08:00"


class MergeBacklogCliTests(unittest.TestCase):
    def run_tool(self, *options):
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
        return result

    def test_the_queue_is_printed_with_its_stages(self):
        result = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stderr)   # rows over the threshold
        self.assertEqual(result.stderr, "")
        for text in ("#1632", "等人工裁决（薄卡豁免）", "停留26h02m", "#1633", "已批待合并"):
            self.assertIn(text, result.stdout)

    def test_the_merged_pull_request_is_not_in_the_queue(self):
        result = self.run_tool()
        self.assertNotIn("#1635", result.stdout)

    def test_the_rows_over_the_threshold_are_named(self):
        result = self.run_tool()
        self.assertIn("合并积压点名 #1632 等人工裁决（薄卡豁免） 停留26h02m（阈值 24h00m）",
                      result.stdout)
        self.assertIn("合并积压点名 #1633 已批待合并 停留3h00m（阈值 2h00m）", result.stdout)

    def test_a_pull_request_that_just_turned_green_is_not_named(self):
        """#1639 was approved three hours before the checks turned green."""
        result = self.run_tool()
        self.assertIn("#1639", result.stdout)
        self.assertNotIn("合并积压点名 #1639", result.stdout)

    def test_the_json_output_carries_the_rows(self):
        result = self.run_tool("--json")
        rows = json.loads(result.stdout)
        numbers = [row["number"] for row in rows]
        self.assertEqual(numbers, [1632, 1633, 1634, 1637, 1638, 1639])
        self.assertEqual(rows[0]["stage"], "等人工裁决")

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


if __name__ == "__main__":
    unittest.main()
