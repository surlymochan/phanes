import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evalid_overhead", ROOT / "bin" / "evalid_overhead.py"
)
overhead = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = overhead
SPEC.loader.exec_module(overhead)


class EvalidOverheadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT.parent)
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

        self.assertEqual(report["data_quality_errors"], [
            {"run_id": "swrm-run", "field": "finished", "error": "missing"},
        ])
        self.assertEqual(report["stages"]["red"], {
            "run_count": 1,
            "queued_seconds": 12.5,
            "execution_seconds": 45.0,
            "budget_seconds": 45.0,
            "usage": {"input_tokens": 5},
        })
        self.assertTrue(report["budget_excludes_queue_seconds"])

    def test_result_seconds_excludes_verification_and_finalization(self):
        self.write_run("finished", stage="red", queued_at=100, started=110,
                       attempt_started=120, finished=210, queued_seconds=10,
                       budget_minutes=1)
        (self.runs / "finished" / "result.json").write_text(json.dumps({
            "seconds": 30, "attempts": [{"seconds": 10}, {"seconds": 20}],
        }), encoding="utf-8")

        report = overhead.build_report(self.runs)

        self.assertEqual(report["stages"]["red"]["execution_seconds"], 30)
        self.assertEqual(report["stages"]["red"]["budget_seconds"], 30)
        self.assertEqual(report["data_quality_errors"], [])

    def test_lane_without_attempt_started_excludes_verification_and_finalization(self):
        cases = ((None, 30), (1, 30), (0.25, 15))
        for index, (allowance, expected) in enumerate(cases):
            with self.subTest(budget_minutes=allowance):
                run_id = f"lane-{index}"
                stage = ("Spec", "red", "Review")[index]
                # lane_do_run writes execution_started, but no attempt_started.
                # Execution ends at 150; verification and cleanup end at 210.
                self.write_run(run_id, stage=stage, queued_at=100, started=120,
                               execution_started=120, finished=210,
                               queued_seconds=20, budget_minutes=allowance)
                (self.runs / run_id / "result.json").write_text(json.dumps({
                    "seconds": 30, "error_class": None,
                    "attempts": [{"seconds": 30, "error_class": None,
                                  "meta": {"usage": {"input_tokens": 5}}}],
                }), encoding="utf-8")

                report = overhead.build_report(self.runs)

                self.assertEqual(report["stages"][stage], {
                    "run_count": 1, "queued_seconds": 20.0,
                    "execution_seconds": 30.0, "budget_seconds": expected,
                    "usage": {"input_tokens": 5},
                })
                self.assertTrue(report["budget_excludes_queue_seconds"])
                self.assertEqual(report["data_quality_errors"], [])
                self.assertEqual(overhead.render_report(report),
                                 overhead.render_report(overhead.build_report(self.runs)))

    def test_runs_without_retry_backoff_use_recorded_execution_for_budget(self):
        for index, attempts in enumerate((None, [], [{"seconds": 30}],
                                          [{"seconds": 10, "error_class": "model_unavailable"},
                                           {"seconds": 20, "error_class": None}])):
            with self.subTest(attempts=attempts):
                run_id = f"finished-{index}"
                self.write_run(run_id, stage="green", queued_at=100, started=120,
                               finished=210, queued_seconds=20)
                (self.runs / run_id / "result.json").write_text(json.dumps({
                    "seconds": 30, "attempts": attempts,
                }), encoding="utf-8")

                report = overhead.build_report(self.runs)

                self.assertEqual(report["stages"]["green"]["budget_seconds"],
                                 30 * (index + 1))
                self.assertEqual(report["data_quality_errors"], [])

    def test_rate_limit_wait_consumes_budget_without_charging_capacity_queue(self):
        cases = (("red", None, 90), ("green", 2, 90), ("Review", 1, 60))
        for index, (stage, allowance, expected) in enumerate(cases):
            with self.subTest(budget_minutes=allowance):
                # First admission at 120; after a 30s attempt and 30s backoff,
                # capacity wait adds 40s to both started and deadline. The last
                # attempt then runs from 220 to 250: 60s execution, 90s budget.
                run_id = f"retry-{index}"
                self.write_run(run_id, stage=stage, queued_at=100,
                               execution_started=120, started=160, finished=250,
                               queued_seconds=60, budget_minutes=allowance)
                (self.runs / run_id / "result.json").write_text(json.dumps({
                    "seconds": 60,
                    "attempts": [
                        {"seconds": 30, "error_class": "rate_limit"},
                        {"seconds": 30, "error_class": None},
                    ],
                }), encoding="utf-8")
                (self.runs / run_id / "events.2.pi.jsonl").write_text(json.dumps({
                    "type": "RATELIMIT", "retry": 1, "wait_seconds": 30,
                }) + "\n", encoding="utf-8")

                report = overhead.build_report(self.runs)

                self.assertEqual(report["stages"][stage]["execution_seconds"], 60)
                self.assertEqual(report["stages"][stage]["queued_seconds"], 60)
                self.assertEqual(report["stages"][stage]["budget_seconds"], expected)
                self.assertEqual(report["totals"]["budget_seconds"],
                                 sum((90, 90, 60)[:index + 1]))
                self.assertTrue(report["budget_excludes_queue_seconds"])
                self.assertEqual(report["data_quality_errors"], [])
                self.assertEqual(overhead.render_report(report),
                                 overhead.render_report(overhead.build_report(self.runs)))

    def test_retry_budget_excludes_finalization_after_the_last_attempt(self):
        self.write_run("retry", stage="green", queued_at=100, started=120,
                       attempt_started=180, finished=300, queued_seconds=20,
                       budget_minutes=2)
        (self.runs / "retry" / "result.json").write_text(json.dumps({
            "seconds": 60,
            "attempts": [{"seconds": 30, "error_class": "rate_limit"},
                         {"seconds": 30, "error_class": None}],
        }), encoding="utf-8")

        report = overhead.build_report(self.runs)

        self.assertEqual(report["stages"]["green"]["execution_seconds"], 60)
        self.assertEqual(report["stages"]["green"]["budget_seconds"], 90)
        self.assertTrue(report["budget_excludes_queue_seconds"])
        self.assertEqual(report["data_quality_errors"], [])

    def test_lane_retry_budget_uses_recorded_backoff_without_finalization(self):
        for index, wait in enumerate((30, 45.25, 0)):
            with self.subTest(wait_seconds=wait):
                run_id = f"lane-retry-{index}"
                stage = ("Spec", "red", "Review")[index]
                self.write_run(run_id, stage=stage, queued_at=100, started=120,
                               execution_started=120, finished=240 + wait,
                               queued_seconds=20, budget_minutes=5)
                run = self.runs / run_id
                (run / "result.json").write_text(json.dumps({
                    "seconds": 60, "error_class": None,
                    "attempts": [
                        {"seconds": 30, "error_class": "rate_limit",
                         "events": "/original/host/events.1.pi.jsonl"},
                        {"seconds": 30, "error_class": None,
                         "events": "/original/host/events.2.pi.jsonl"},
                    ],
                }), encoding="utf-8")
                # Lane retries record the wait in the following attempt's log.
                (run / "events.2.pi.jsonl").write_text(json.dumps({
                    "type": "RATELIMIT", "retry": 1, "wait_seconds": wait,
                }) + "\n", encoding="utf-8")

                report = overhead.build_report(self.runs)

                self.assertEqual(report["stages"][stage]["execution_seconds"], 60)
                self.assertEqual(report["stages"][stage]["budget_seconds"], 60 + wait)
                self.assertTrue(report["budget_excludes_queue_seconds"])
                self.assertEqual(report["data_quality_errors"], [])
                self.assertEqual(overhead.render_report(report),
                                 overhead.render_report(overhead.build_report(self.runs)))

    def test_retry_without_timing_evidence_does_not_charge_finalization(self):
        self.write_run("unknown-retry", stage="red", queued_at=100, started=120,
                       finished=270, queued_seconds=20, budget_minutes=5)
        (self.runs / "unknown-retry" / "result.json").write_text(json.dumps({
            "seconds": 60,
            "attempts": [{"seconds": 30, "error_class": "rate_limit"},
                         {"seconds": 30, "error_class": None}],
        }), encoding="utf-8")

        report = overhead.build_report(self.runs)

        # No timestamp separates the unknown backoff from finalization. Keep
        # the measured execution lower bound and reject the budget assertion.
        self.assertEqual(report["stages"]["red"]["budget_seconds"], 60)
        self.assertFalse(report["budget_excludes_queue_seconds"])
        self.assertEqual(report["data_quality_errors"], [
            {"run_id": "unknown-retry", "field": "retry_wait_seconds", "error": "missing"},
        ])

    def test_multiple_retry_waits_are_read_once_for_direct_and_lane_logs(self):
        for index, log_offset in enumerate((0, 1)):
            with self.subTest(log_offset=log_offset):
                run_id = f"multiple-{index}"
                stage = ("red", "green")[index]
                self.write_run(run_id, stage=stage, queued_at=100, started=160,
                               execution_started=120, finished=385,
                               queued_seconds=60, budget_minutes=5)
                run = self.runs / run_id
                attempts = [{"seconds": 30, "error_class": error,
                             "events": f"/original/events.{number}.pi.jsonl"}
                            for number, error in enumerate(("rate_limit", "rate_limit", None), 1)]
                (run / "result.json").write_text(json.dumps({
                    "seconds": 90, "attempts": attempts,
                }), encoding="utf-8")
                for number, wait in enumerate((25, 50), 1):
                    (run / f"events.{number + log_offset}.pi.jsonl").write_text(
                        "[]\nnot-json\n" + json.dumps({
                            "type": "RATELIMIT", "retry": number, "wait_seconds": wait,
                        }) + "\n", encoding="utf-8")
                before = {path.name: path.read_bytes() for path in run.iterdir()}

                report = overhead.build_report(self.runs)

                self.assertEqual(report["stages"][stage]["budget_seconds"], 165)
                self.assertTrue(report["budget_excludes_queue_seconds"])
                self.assertEqual(report["data_quality_errors"], [])
                self.assertEqual(before, {path.name: path.read_bytes() for path in run.iterdir()})

    def test_budget_assertion_detects_queue_charges_and_double_deductions(self):
        self.write_run("audit", stage="red", queued_at=100, started=120,
                       finished=240, queued_seconds=20, budget_minutes=5)
        (self.runs / "audit" / "result.json").write_text(
            json.dumps({"seconds": 60}), encoding="utf-8")
        record = overhead._run_record(self.runs / "audit")
        self.assertTrue(overhead.build_report(self.runs)["budget_excludes_queue_seconds"])

        for incorrect in (80, 40):
            with self.subTest(budget_seconds=incorrect):
                # Both errors still fit inside finished - started. A check
                # against the calculator's own elapsed value cannot catch them.
                corrupted = dict(record, budget_seconds=incorrect, budget_elapsed_seconds=120)
                with mock.patch.object(overhead, "_run_record", return_value=corrupted):
                    report = overhead.build_report(self.runs)
                self.assertFalse(report["budget_excludes_queue_seconds"])

    def test_budget_assertion_checks_independent_queue_free_wall_window(self):
        self.write_run("inconsistent", stage="red", queued_at=100, started=120,
                       finished=150, queued_seconds=20, budget_minutes=5)
        (self.runs / "inconsistent" / "result.json").write_text(
            json.dumps({"seconds": 50}), encoding="utf-8")

        self.assertFalse(overhead.build_report(self.runs)["budget_excludes_queue_seconds"])

    def test_budget_exhausted_during_rate_limit_wait_counts_the_wait(self):
        self.write_run("expired", stage="Review", queued_at=100, started=120,
                       attempt_started=120, finished=180, queued_seconds=20,
                       budget_minutes=1)
        (self.runs / "expired" / "result.json").write_text(json.dumps({
            "error_class": "budget_exceeded", "seconds": 30,
            "attempts": [{"seconds": 30, "error_class": "budget_exceeded"}],
        }), encoding="utf-8")

        report = overhead.build_report(self.runs)

        self.assertEqual(report["stages"]["Review"]["execution_seconds"], 30)
        self.assertEqual(report["stages"]["Review"]["budget_seconds"], 60)
        self.assertTrue(report["budget_excludes_queue_seconds"])
        self.assertEqual(report["data_quality_errors"], [])

    def test_interrupted_retry_queue_excludes_only_unshifted_capacity_wait(self):
        cases = (
            # First attempt: 30s execution, 10s backoff, 60s cancelled queue.
            ("stopped", 120, 120, 220, 80, 30, 40),
            ("budget_exceeded", 120, 120, 220, 80, 30, 40),
            ("exec_error", 120, 120, 220, 80, 30, 40),
            # One admitted retry already shifted started by 40s. A further
            # cancelled queue adds 60s; both 10s backoffs still consume budget.
            ("stopped", 160, 200, 300, 120, 60, 80),
            # Cancellation before the first admission consumes no budget.
            ("stopped", 100, None, 160, 60, 0, 0),
        )
        for index, (error, started, attempt_started, finished, queued,
                    execution, expected) in enumerate(cases):
            with self.subTest(error=error, started=started):
                run_id = f"interrupted-{index}"
                stage = ("Spec", "spec-check", "red", "green", "holdout")[index]
                self.write_run(run_id, stage=stage, queued_at=100,
                               started=started, attempt_started=attempt_started,
                               finished=finished, queued_seconds=queued,
                               budget_minutes=2)
                attempts = [{"seconds": 30, "error_class": "rate_limit"}
                            for _ in range(execution // 30)]
                # swrm records an unadmitted retry as a zero-second attempt.
                attempts.append({"seconds": 0, "error_class": error})
                (self.runs / run_id / "result.json").write_text(json.dumps({
                    "seconds": execution, "error_class": error,
                    "attempts": attempts,
                }), encoding="utf-8")

                report = overhead.build_report(self.runs)

                self.assertEqual(report["stages"][stage]["budget_seconds"], expected)
                self.assertEqual(report["stages"][stage]["execution_seconds"], execution)
                self.assertEqual(report["stages"][stage]["queued_seconds"], queued)
                self.assertTrue(report["budget_excludes_queue_seconds"])
                self.assertEqual(report["data_quality_errors"], [])
                self.assertEqual(overhead.render_report(report),
                                 overhead.render_report(overhead.build_report(self.runs)))

    def test_result_seconds_does_not_hide_timestamp_or_duration_errors(self):
        self.write_run("missing-times", stage="Spec")
        self.write_run("reversed", stage="red", queued_at=100, started=110,
                       finished=109)
        self.write_run("negative-result", stage="green", queued_at=100,
                       started=110, finished=210)
        for run_id, seconds in (("missing-times", 30), ("reversed", 30),
                                ("negative-result", -1)):
            (self.runs / run_id / "result.json").write_text(
                json.dumps({"seconds": seconds}), encoding="utf-8")

        report = overhead.build_report(self.runs)

        self.assertEqual(report["data_quality_errors"], [
            {"run_id": "missing-times", "field": "queued_at", "error": "missing"},
            {"run_id": "missing-times", "field": "started", "error": "missing"},
            {"run_id": "missing-times", "field": "finished", "error": "missing"},
            {"run_id": "negative-result", "field": "execution_seconds", "error": "negative"},
            {"run_id": "reversed", "field": "execution_seconds", "error": "negative"},
        ])
        self.assertEqual(report["stages"]["green"]["execution_seconds"], 0)

    def test_baselines_are_counted_per_change_and_retries_remain_extra(self):
        common = {"queued_at": 100, "started": 110, "finished": 140,
                  "queued_seconds": 10, "usage": {"input_tokens": 5}}
        for change in ("issue-a", "issue-b"):
            for stage in ("red", "green", "holdout", "Review"):
                self.write_run(f"{change}-{stage}", stage=stage,
                               change_id=change, **common)
        self.write_run("spec", stage="Spec", change_id="issue-a", **common)

        report = overhead.build_report(self.runs)
        self.assertEqual(report["baseline_run_count"], 8)
        self.assertEqual(report["extra_run_count"], 1)

        # A retry belongs to the same change even if metadata is in result.json.
        self.write_run("retry", stage="red", **common)
        (self.runs / "retry" / "result.json").write_text(
            json.dumps({"change_id": "issue-b", "seconds": 30}), encoding="utf-8")
        report = overhead.build_report(self.runs)
        self.assertEqual(report["baseline_run_count"], 8)
        self.assertEqual(report["extra_run_count"], 2)
        self.assertEqual(report["totals"]["usage"], {"input_tokens": 50})
        self.assertEqual(report["stages"]["red"]["run_count"], 3)
        self.assertEqual(report["stages"]["red"]["execution_seconds"], 90)
        self.assertEqual(overhead.build_report(self.runs, stages=["red"])
                         ["baseline_run_count"], 2)
        self.assertEqual(overhead.render_report(report),
                         overhead.render_report(overhead.build_report(self.runs)))

    def test_swrm_task_cards_identify_changes_without_json_change_id(self):
        common = {"queued_at": 100, "started": 110, "finished": 140,
                  "queued_seconds": 10}

        def write_role_run(run_id, change):
            self.write_run(run_id, **common)
            run = self.runs / run_id
            (run / "task.md").write_text(
                "---\nevalid_version: 1\nrole: evalid-tdd\n"
                f"change_id: {change}\ninput_evidence: []\n---\n"
                "Build it.\nchange_id: body-is-not-metadata\n", encoding="utf-8")
            (run / "result.json").write_text(json.dumps({
                "evalid_stage": "red", "seconds": 30,
            }), encoding="utf-8")

        write_role_run("red-a", "issue-a")
        write_role_run("red-b", '"issue-b"')
        report = overhead.build_report(self.runs)
        self.assertEqual(report["baseline_run_count"], 2)
        self.assertEqual(report["extra_run_count"], 0)
        self.assertEqual(report["data_quality_errors"], [])

        write_role_run("red-b-retry", "'issue-b'")
        report = overhead.build_report(self.runs)
        self.assertEqual(report["stages"]["red"]["run_count"], 3)
        self.assertEqual(report["baseline_run_count"], 2)
        self.assertEqual(report["extra_run_count"], 1)
        self.assertEqual(overhead.render_report(report),
                         overhead.render_report(overhead.build_report(self.runs)))

    def test_swrm_attempt_usage_includes_retries_without_double_counting(self):
        for run_id in ("red-a", "red-b"):
            self.write_run(run_id, queued_at=100, started=110, finished=140,
                           usage={"input_tokens": 7})
            (self.runs / run_id / "result.json").write_text(json.dumps({
                "evalid_stage": "red", "seconds": 30,
                "usage": {"input_tokens": 7, "output_tokens": 3, "cost_usd": 0.2},
                "attempts": [
                    {"seconds": 10, "meta": {"usage": {
                        "input_tokens": 5, "output_tokens": 2, "cost_usd": 0.1}}},
                    {"seconds": 20, "meta": {"usage": {
                        "input_tokens": 7, "output_tokens": 3, "cost_usd": 0.2}}},
                ],
            }), encoding="utf-8")

        report = overhead.build_report(self.runs)

        self.assertEqual(report["stages"]["red"]["run_count"], 2)
        self.assertEqual(report["totals"]["usage"]["input_tokens"], 24)
        self.assertEqual(report["totals"]["usage"]["output_tokens"], 10)
        self.assertAlmostEqual(report["totals"]["usage"]["cost_usd"], 0.6)
        self.assertEqual(report["stages"]["red"]["usage"], report["totals"]["usage"])
        self.assertEqual(report["stages"]["red"]["execution_seconds"], 60)

    def test_usage_falls_back_to_top_level_without_attempt_usage(self):
        for index, attempts in enumerate((None, [], [{"seconds": 30}],
                                           [None, {"meta": None}, {"meta": {"usage": None}}])):
            with self.subTest(attempts=attempts):
                run_id = f"red-{index}"
                self.write_run(run_id, stage="red", queued_at=100,
                               started=110, finished=140)
                (self.runs / run_id / "result.json").write_text(json.dumps({
                    "usage": {"input_tokens": 7}, "attempts": attempts,
                }), encoding="utf-8")
                report = overhead.build_report(self.runs)
                self.assertEqual(report["totals"]["usage"], {"input_tokens": 7 * (index + 1)})

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
