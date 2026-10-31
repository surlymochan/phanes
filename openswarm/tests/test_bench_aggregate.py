"""Unit tests for empty end-to-end bench aggregation."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

E2E = Path(__file__).resolve().parents[1] / "bench" / "e2e"
sys.path.insert(0, os.fspath(E2E))

import report  # noqa: E402


class EmptyBenchAggregationTests(unittest.TestCase):
    def test_empty_series_has_zero_counts_and_empty_statistics(self):
        stats = report.series([])

        self.assertEqual(
            {key: stats[key] for key in ("n", "success_n", "success")},
            {"n": 0, "success_n": 0, "success": 0},
        )
        self.assertIsNone(stats["success_pct"])
        empty = {"n": 0, "median": None, "q1": None, "q3": None,
                 "iqr": None, "min": None, "max": None}
        for key in ("wall_s", "input", "output", "cache_read", "total"):
            self.assertEqual(stats[key], empty)

    def test_empty_rows_produce_empty_groups_and_csv_table(self):
        self.assertEqual(report.group([], lambda row: row["arm"]), [])

        with tempfile.TemporaryDirectory(prefix="openswarm-bench-unit-") as tmp:
            path = Path(tmp) / "scores.csv"
            report.write_csv_all(path, [("arm", []), ("task", []), ("task_arm", [])])
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
