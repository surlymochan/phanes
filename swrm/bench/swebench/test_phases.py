"""Planner / executor split of a run's cost, as recorded and reported (backlog #4, P1-1)."""

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import phases
import report


def call(label, wall, tokens):
    return {"label": label, "wall_s": wall, "usage": tokens}


PLANNER_CALL = call("planner", 30.0, {"input": 1000, "output": 200, "cache_read": 500, "cache_write": 0})
EXECUTOR_CALL = call("executor", 90.0, {"input": 5000, "output": 900, "cache_read": 4000, "cache_write": 0})


class PhaseSplitTests(unittest.TestCase):
    def test_planner_and_executor_are_split(self):
        split = phases.phase_split([PLANNER_CALL, EXECUTOR_CALL])

        self.assertEqual(split["planner"]["wall_s"], 30.0)
        self.assertEqual(split["executor"]["wall_s"], 90.0)
        self.assertEqual(split["planner"]["tokens_in_out"], 1200)
        self.assertEqual(split["planner"]["tokens_total"], 1700)
        self.assertEqual(split["executor"]["tokens_in_out"], 5900)

    def test_a_single_agent_call_is_executor_work(self):
        split = phases.phase_split([call("agent", 10.0, {"input": 5, "output": 5})])

        self.assertNotIn("planner", split)
        self.assertEqual(split["executor"]["wall_s"], 10.0)

    def test_no_calls_gives_no_phases(self):
        self.assertEqual(phases.phase_split([]), {})
        self.assertEqual(phases.phase_split(None), {})


class PlannerReportTests(unittest.TestCase):
    def rows(self):
        return [{"instance_id": "a", "arm": "openswarm", "rep": 1, "resolved": True, "wall_s": 120.0,
                 "tokens": {"input": 6000, "output": 1100, "cache_read": 4500, "cache_write": 0},
                 "calls": [PLANNER_CALL, EXECUTOR_CALL]}]

    def test_planner_share_is_reported_as_a_median(self):
        stats = report.phase_stats(self.rows())

        self.assertEqual(stats["planner"]["runs_with_planner"], 1)
        self.assertEqual(stats["planner"]["wall_s_median"], 30.0)
        self.assertAlmostEqual(stats["planner"]["wall_s_share_median"], 0.25)
        self.assertEqual(stats["planner"]["tokens_in_out_median"], 1200)
        self.assertEqual(stats["executor"]["wall_s_median"], 90.0)

    def test_arms_without_a_planner_report_no_planner_numbers(self):
        rows = [{"instance_id": "a", "arm": "baseline", "rep": 1, "resolved": True, "wall_s": 10.0,
                 "tokens": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0},
                 "calls": [call("agent", 10.0, {"input": 1, "output": 1})]}]

        stats = report.phase_stats(rows)

        self.assertEqual(stats["planner"]["runs_with_planner"], 0)
        self.assertIsNone(stats["planner"]["wall_s_median"])
        self.assertEqual(stats["executor"]["wall_s_median"], 10.0)

    def test_per_cell_split_is_available_for_the_table(self):
        cells = report.phase_cells(self.rows())

        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0]["instance_id"], "a")
        self.assertEqual(cells[0]["planner_wall_s"], 30.0)
        self.assertEqual(cells[0]["executor_wall_s"], 90.0)
        self.assertAlmostEqual(cells[0]["planner_share"], 0.25)

    def test_summary_and_tables_carry_the_planner_split(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            price = {"id": "fixture", "label": "Fixture", "in": 1, "out": 1,
                     "cache_read": 1, "cache_write": 1, "computed_not_billed": True}
            (root / "prices.json").write_text(json.dumps({"rows": [price]}))
            source = root / "runs.jsonl"
            source.write_text("".join(json.dumps(record) + "\n" for record in self.rows()))
            out = root / "out"
            argv = ["report.py", str(source), "--out", str(out), "--raw", str(root / "raw"),
                    "--price", "fixture"]
            original = sys.argv
            sys.argv = argv
            try:
                with mock.patch.object(report, "ROOT", root), redirect_stdout(StringIO()):
                    report.main()
            finally:
                sys.argv = original
            markdown = (out / "tables.md").read_text()
            summary = json.loads((out / "summary.json").read_text())

        self.assertIn("### Planner vs executor", markdown)
        self.assertIn("| openswarm | 1 | 30 s | 25% | 1,200 | 90 s |", markdown)
        self.assertIn("| `a` | openswarm | 1 | 30 s | 90 s | 25% | 1,200 |", markdown)
        self.assertEqual(summary["arms"]["openswarm"]["planner"]["runs_with_planner"], 1)
        self.assertEqual(summary["planner_cells"][0]["planner_wall_s"], 30.0)

    def test_report_handles_planner_without_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            price = {"id": "fixture", "label": "Fixture", "in": 1, "out": 1,
                     "cache_read": 1, "cache_write": 1, "computed_not_billed": True}
            (root / "prices.json").write_text(json.dumps({"rows": [price]}))
            row = self.rows()[0]
            row.update(resolved=False, calls=[PLANNER_CALL])
            source = root / "runs.jsonl"
            source.write_text(json.dumps(row) + "\n")
            out = root / "out"
            argv = ["report.py", str(source), "--out", str(out), "--raw", str(root / "raw"),
                    "--price", "fixture"]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(report, "ROOT", root), redirect_stdout(StringIO()):
                report.main()
            markdown = (out / "tables.md").read_text()
            self.assertIn("| openswarm | 1 | 30 s | 25% | 1,200 | - |", markdown)
            self.assertIn("| `a` | openswarm | 1 | 30 s | - | 25% | 1,200 |", markdown)


if __name__ == "__main__":
    unittest.main()
