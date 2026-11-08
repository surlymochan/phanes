import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evalid_overhead", ROOT / "bin" / "evalid_overhead.py"
)
overhead = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = overhead
SPEC.loader.exec_module(overhead)


class EvalidOverheadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runs = pathlib.Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write_run(self, run_id, **status):
        run = self.runs / run_id
        run.mkdir()
        (run / "status.json").write_text(json.dumps(status), encoding="utf-8")

    def test_cross_midnight_queue_is_excluded_from_budget_time(self):
        self.write_run(
            "spec-1",
            stage="Spec",
            queued_at="2026-11-08T23:59:50Z",
            started="2026-11-09T00:00:10Z",
            finished="2026-11-09T00:01:10Z",
            queued_seconds=20,
            budget_minutes=1,
            usage={"input_tokens": 120, "output_tokens": 30},
        )

        report = overhead.build_report(self.runs)

        self.assertEqual(report["stages"]["Spec"], {
            "run_count": 1,
            "queued_seconds": 20.0,
            "execution_seconds": 60.0,
            "budget_seconds": 60.0,
            "usage": {"input_tokens": 120, "output_tokens": 30},
        })
        self.assertEqual(report["queue_percentiles_seconds"], {
            "p50": 20.0, "p90": 20.0, "p95": 20.0, "p99": 20.0,
        })
        self.assertEqual(report["data_quality_errors"], [])

    def test_retries_are_counted_and_json_is_stable(self):
        common = {
            "queued_at": "2026-11-09T01:00:00Z",
            "started": "2026-11-09T01:00:05Z",
            "finished": "2026-11-09T01:00:35Z",
            "queued_seconds": 5,
        }
        self.write_run("red-retry", stage="red", attempt=2,
                       usage={"input_tokens": 11}, **common)
        self.write_run("red-first", stage="red", attempt=1,
                       usage={"input_tokens": 7}, **common)
        self.write_run("green", stage="green", attempt=1,
                       usage={"input_tokens": 13}, **common)
        self.write_run("review", stage="Review", attempt=1,
                       usage={"input_tokens": 17}, **common)

        first = overhead.render_report(overhead.build_report(self.runs))
        second = overhead.render_report(overhead.build_report(self.runs))
        parsed = json.loads(first)

        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))
        self.assertEqual(parsed["stages"]["red"]["run_count"], 2)
        self.assertEqual(parsed["stages"]["red"]["usage"]["input_tokens"], 18)
        self.assertEqual(parsed["extra_run_count"], 1)
        self.assertEqual(list(parsed["stages"]),
                         ["Spec", "spec-check", "red", "green", "holdout", "Review"])

    def test_missing_timestamp_and_negative_duration_are_quality_errors(self):
        self.write_run(
            "missing-start",
            stage="holdout",
            queued_at="2026-11-09T02:00:00Z",
            finished="2026-11-09T02:00:20Z",
            queued_seconds=4,
        )
        self.write_run(
            "negative",
            stage="spec-check",
            queued_at="2026-11-09T03:00:00Z",
            started="2026-11-09T03:00:10Z",
            finished="2026-11-09T03:00:09Z",
            queued_seconds=10,
        )

        report = overhead.build_report(self.runs)

        self.assertEqual(report["data_quality_errors"], [
            {"run_id": "missing-start", "field": "started", "error": "missing"},
            {"run_id": "negative", "field": "execution_seconds", "error": "negative"},
        ])
        self.assertEqual(report["stages"]["holdout"]["run_count"], 1)
        self.assertEqual(report["stages"]["spec-check"]["run_count"], 1)

    def test_swrm_run_shape_reports_execution_without_queue(self):
        run = self.runs / "swrm-run"
        run.mkdir()
        (run / "status.json").write_text(json.dumps({
            "stage": "red",
            "queued_at": 1000.0,
            "started": 1010.0,
            "queued_seconds": 12.5,
            "budget_minutes": 1,
        }), encoding="utf-8")
        (run / "result.json").write_text(json.dumps({
            "seconds": 45.0,
            "usage": {"input_tokens": 5},
        }), encoding="utf-8")

        report = overhead.build_report(self.runs)

        self.assertEqual(report["data_quality_errors"], [])
        self.assertEqual(report["stages"]["red"], {
            "run_count": 1,
            "queued_seconds": 12.5,
            "execution_seconds": 45.0,
            "budget_seconds": 45.0,
            "usage": {"input_tokens": 5},
        })
        self.assertTrue(report["budget_excludes_queue_seconds"])

    def test_budget_is_capped_at_the_allowance_and_never_charges_queue(self):
        self.write_run(
            "over-budget",
            stage="green",
            queued_at="2026-11-09T04:00:00Z",
            started="2026-11-09T04:00:10Z",
            finished="2026-11-09T04:03:30Z",
            queued_seconds=90,
            budget_minutes=1,
        )

        report = overhead.build_report(self.runs)

        self.assertEqual(report["stages"]["green"]["execution_seconds"], 200.0)
        self.assertEqual(report["stages"]["green"]["budget_seconds"], 60.0)
        self.assertEqual(report["stages"]["green"]["queued_seconds"], 90.0)
        self.assertTrue(report["budget_excludes_queue_seconds"])

    def test_cli_prints_the_same_report_as_the_library(self):
        self.write_run(
            "spec-cli",
            stage="Spec",
            queued_at="2026-11-09T05:00:00Z",
            started="2026-11-09T05:00:10Z",
            finished="2026-11-09T05:00:40Z",
            queued_seconds=10,
            budget_minutes=1,
            usage={"input_tokens": 3},
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = overhead._main([str(self.runs)])

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(),
                         overhead.render_report(overhead.build_report(self.runs)))


if __name__ == "__main__":
    unittest.main()
