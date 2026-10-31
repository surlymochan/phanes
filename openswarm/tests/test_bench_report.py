"""bench e2e report.py on an empty result set. Local files only, no model calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "bench" / "e2e"
REPORT = E2E / "report.py"
# Bar colours of report.py:ARM_COLORS. A chart without bars must not contain them.
ARM_COLORS = ("#3d5a80", "#ee6c4d", "#2a9d8f", "#e9c46a", "#6d597a")


class EmptyResultSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-bench-report-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def report(self, src, out_dir):
        return subprocess.run(
            [sys.executable, str(REPORT), str(src), "--out-dir", str(out_dir)],
            cwd=str(E2E),
            text=True,
            capture_output=True,
            check=False,
        )

    def write_jsonl(self, name, rows):
        path = self.root / name
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def test_empty_jsonl_reports_without_crashing(self):
        src = self.write_jsonl("empty.jsonl", [])
        out = self.root / "out"
        proc = self.report(src, out)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        for name in ("summary.md", "scores.csv", "chart.svg"):
            self.assertTrue((out / name).is_file(), name)

    def test_empty_jsonl_writes_empty_tables(self):
        src = self.write_jsonl("empty.jsonl", [])
        out = self.root / "out"
        self.assertEqual(self.report(src, out).returncode, 0)

        summary = (out / "summary.md").read_text(encoding="utf-8")
        self.assertIn("## Per arm", summary)
        self.assertIn("## Per task", summary)
        # No run was recorded, so no arm and no task may show up in the tables.
        for name in ("baseline", "superpowers", "openflow", "openswarm"):
            self.assertNotIn(name, summary)

        # Only the header row, no data rows.
        csv_lines = (out / "scores.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(csv_lines), 1)
        self.assertTrue(csv_lines[0].startswith("scope,id,n,success"))

        # Three empty panels, no bars.
        svg = (out / "chart.svg").read_text(encoding="utf-8")
        self.assertIn("Success %", svg)
        self.assertIn("Median minutes", svg)
        self.assertIn("Median total tokens", svg)
        for color in ARM_COLORS:
            self.assertNotIn(color, svg)

    def test_environment_only_jsonl_keeps_the_environment_section(self):
        src = self.write_jsonl(
            "env-only.jsonl",
            [{"type": "environment", "date": "2026-10-31", "os": "linux"}],
        )
        out = self.root / "out"
        proc = self.report(src, out)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        summary = (out / "summary.md").read_text(encoding="utf-8")
        self.assertIn("## Environment", summary)
        self.assertIn("- Date: 2026-10-31", summary)
        self.assertNotIn("baseline", summary)
        self.assertEqual(len((out / "scores.csv").read_text(encoding="utf-8").splitlines()), 1)

    def test_skipped_rows_only_jsonl_is_an_empty_result_set(self):
        src = self.write_jsonl(
            "skipped-only.jsonl",
            [{"type": "run", "task": "csv-quoting", "arm": "baseline", "skipped": True}],
        )
        out = self.root / "out"
        proc = self.report(src, out)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        summary = (out / "summary.md").read_text(encoding="utf-8")
        self.assertNotIn("baseline", summary)
        self.assertNotIn("csv-quoting", summary)
        self.assertEqual(len((out / "scores.csv").read_text(encoding="utf-8").splitlines()), 1)

    def test_missing_input_is_reported_without_a_traceback(self):
        out = self.root / "out"
        proc = self.report(self.root / "does-not-exist.jsonl", out)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("not a file", proc.stderr)
        self.assertIn("does-not-exist.jsonl", proc.stderr)

    def test_empty_results_directory_is_reported_without_a_traceback(self):
        empty_dir = self.root / "results"
        empty_dir.mkdir()
        out = self.root / "out"
        proc = self.report(empty_dir, out)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("not a file", proc.stderr)
        self.assertIn("results", proc.stderr)

    def test_empty_result_set_says_that_it_is_empty(self):
        """report.py must state that the result set was empty.

        Current behaviour (2026-10-31): report.py exits 0 and writes empty
        tables, but neither stdout/stderr nor summary.md mentions that there
        were no runs at all. Expected failure until the report says so.
        """
        src = self.write_jsonl("empty.jsonl", [])
        out = self.root / "out"
        proc = self.report(src, out)
        summary = (out / "summary.md").read_text(encoding="utf-8")
        said = (proc.stdout + proc.stderr + summary).lower()
        self.assertRegex(said, r"no runs|no results|empty result|nothing to report")


if __name__ == "__main__":
    unittest.main()
