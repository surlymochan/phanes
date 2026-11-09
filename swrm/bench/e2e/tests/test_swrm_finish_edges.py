"""CLI edge cases for swrm_finish."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FINISH = ROOT / "harness" / "swrm_finish.py"


class SwrmFinishEdgeTests(unittest.TestCase):
    def test_creates_missing_nested_events_directory_before_failure_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            tempdir = Path(temp)
            stdout = tempdir / "stdout.txt"
            stdout.write_text("no result here\n", encoding="utf-8")
            events = tempdir / "nested" / "events"
            workdir = tempdir / "worktree"
            workdir.mkdir()

            proc = subprocess.run(
                [sys.executable, str(FINISH), str(stdout), str(events), str(workdir)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertTrue(events.is_dir())
            self.assertEqual(json.loads((events / "executor-usage.json").read_text(encoding="utf-8")), {"calls": []})
            self.assertEqual((events / "merge.txt").read_text(encoding="utf-8"), "no swrm result object on stdout\n")

    def test_creates_no_extra_structure_for_existing_events_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            tempdir = Path(temp)
            stdout = tempdir / "stdout.txt"
            stdout.write_text("no result here\n", encoding="utf-8")
            events = tempdir / "events"
            events.mkdir()
            workdir = tempdir / "worktree"
            workdir.mkdir()

            proc = subprocess.run(
                [sys.executable, str(FINISH), str(stdout), str(events), str(workdir)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual(
                {path.name for path in events.iterdir()},
                {"executor-usage.json", "merge.txt"},
            )


if __name__ == "__main__":
    unittest.main()
