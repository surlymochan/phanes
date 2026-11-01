"""Repeat aggregation and single-repeat report compatibility from local fixtures."""

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import report


def row(instance="a", rep=1, resolved=True, wall=10, arm="baseline"):
    return {"instance_id": instance, "arm": arm, "rep": rep, "resolved": resolved,
            "wall_s": wall,
            "tokens": {"input": 100, "output": 20, "cache_read": 0, "cache_write": 0}}


def repeated_rows():
    return [row("a", 1, True, 10), row("b", 1, False, 30),
            row("a", 2, True, 100), row("b", 2, True, 300),
            row("a", 3, False, 1000), row("b", 3, False, 3000)]


# Captured from the unmodified report with the same rows and fixed fixture prices.
SINGLE_REP_MARKDOWN = """| Arm | Resolved (official tests) | Median wall-clock (IQR) | Median $ per task (fixture) | Total $ all runs (fixture) | Median tokens in+out | Median tokens incl. cache | n |
| --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | **1/2 (50%)** | 20 s (15–25) | $0.0001 | $0.00 | 120 | 120 | 2 |

Same token counts at other list prices (computed, not billed):

| Arm | Fixture: median / total |
| --- | --- |
| baseline | $0.000 / $0.00 |

Per instance (resolved / runs):

| Instance | baseline |
| --- | --- |
| `a` | 1/1 |
| `b` | 0/1 |

Timed out (20 min cap): baseline 0
Guard blocks: baseline 0
Rate-limit (429) retries inside Pi: baseline 0 in 0 runs
Runs whose last model call ended in an error: baseline 0
Harness errors excluded from n: 0
Leak scan (tool calls touching upstream mirrors, dataset or other results): none
"""


class RepeatBreakdownTests(unittest.TestCase):
    def test_three_repeats_have_correct_resolved_distribution(self):
        result = report.rep_breakdown(repeated_rows())["baseline"]
        self.assertEqual(result["resolved_per_rep"], {1: 1, 2: 2, 3: 0})
        self.assertEqual(result["resolved_q1_med_q3"], (0.5, 1, 1.5))
        self.assertEqual(result["per_instance"], {"a": [2, 3], "b": [1, 3]})

    def test_wall_distribution_uses_each_rep_median_not_pooled_rows(self):
        rows = [row(str(n), 1, n < 2, n) for n in range(1, 6)]
        rows += [row("a", 2, True, 100), row("a", 3, False, 1000)]
        result = report.rep_breakdown(rows)["baseline"]
        self.assertEqual(result["wall_s_per_rep_median"], {1: 3, 2: 100, 3: 1000})
        self.assertEqual(result["wall_s_q1_med_q3"], (51.5, 100, 550))
        self.assertNotEqual(result["wall_s_q1_med_q3"][1], 4)  # pooled median

    def test_missing_rep_is_grouped_with_explicit_one(self):
        missing = row("a", resolved=True)
        del missing["rep"]
        result = report.rep_breakdown([missing, row("b", 1, False), row("a", 2, False)])
        self.assertEqual(result["baseline"]["resolved_per_rep"], {1: 1, 2: 0})
        self.assertEqual(result["baseline"]["per_instance"], {"a": [1, 2], "b": [0, 1]})

    def test_arms_only_include_their_observed_repeats(self):
        rows = repeated_rows() + [row("a", 2, True, arm="other")]
        result = report.rep_breakdown(rows)
        self.assertEqual(result["other"]["resolved_per_rep"], {2: 1})
        self.assertEqual(result["other"]["resolved_q1_med_q3"], (1, 1, 1))

    def test_harness_errors_are_not_runs_or_instances(self):
        rows = [row(), {"arm": "baseline", "rep": 9, "instance_id": "broken",
                        "harness_error": "fixture failure"}]
        result = report.rep_breakdown(rows)["baseline"]
        self.assertEqual(result["resolved_per_rep"], {1: 1})
        self.assertNotIn("broken", result["per_instance"])
        self.assertEqual(report.rep_breakdown([]), {})


class ReportMarkdownTests(unittest.TestCase):
    def render(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            price = {"id": "fixture", "label": "Fixture", "in": 1, "out": 1,
                     "cache_read": 1, "cache_write": 1, "computed_not_billed": True}
            (root / "prices.json").write_text(json.dumps({"rows": [price]}))
            source = root / "runs.jsonl"
            source.write_text("".join(json.dumps(record) + "\n" for record in rows))
            out = root / "out"
            output = io.StringIO()
            argv = ["report.py", str(source), "--out", str(out), "--raw", str(root / "raw"),
                    "--price", "fixture"]
            with mock.patch.object(report, "ROOT", root), mock.patch.object(sys, "argv", argv), \
                    redirect_stdout(output):
                report.main()
            return output.getvalue().encode(), (out / "tables.md").read_bytes(), \
                json.loads((out / "summary.json").read_text())

    def test_single_rep_stdout_and_markdown_are_byte_identical_to_original(self):
        stdout, markdown, summary = self.render([row("a", 1, True, 10), row("b", 1, False, 30)])
        self.assertEqual(stdout, SINGLE_REP_MARKDOWN.encode())
        self.assertEqual(markdown, SINGLE_REP_MARKDOWN.encode())
        self.assertEqual(set(summary), {"source", "harness_errors", "arms", "per_instance", "leak_scan"})
        self.assertNotIn(b"### Repeats", stdout)

    def test_omitted_rep_has_the_same_single_rep_output(self):
        rows = [row("a", 1, True, 10), row("b", 1, False, 30)]
        for record in rows:
            del record["rep"]
        stdout, markdown, _ = self.render(rows)
        self.assertEqual(stdout, SINGLE_REP_MARKDOWN.encode())
        self.assertEqual(markdown, SINGLE_REP_MARKDOWN.encode())

    def test_repeats_section_contains_distributions_and_instance_arm_matrix(self):
        rows = repeated_rows() + [row("a", rep, True, arm="other") for rep in (1, 2, 3)]
        stdout, markdown, _ = self.render(rows)
        self.assertEqual(stdout, markdown)
        text = stdout.decode()
        self.assertEqual(text.count("### Repeats"), 1)
        start = text.index("### Repeats")
        end = text.index("Same token counts at other list prices")
        section = text[start:end]
        self.assertIn("resolved per rep", section.lower())
        self.assertIn("0.5", section)
        self.assertIn("1.5", section)
        self.assertIn("| Instance | baseline | other |", section)
        self.assertIn("| `a` | 2/3 | 3/3 |", section)
        self.assertIn("| `b` | 1/3 | 0/0 |", section)
        self.assertLess(text.index("| baseline | **3/6"), start)


if __name__ == "__main__":
    unittest.main()
