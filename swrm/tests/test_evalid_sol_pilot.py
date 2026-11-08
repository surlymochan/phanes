"""Acceptance tests for the offline Sol Spec pilot dispatch gate."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PILOT = ROOT / "bin" / "evalid_sol_pilot.py"


class EvalidSolPilotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.runs = pathlib.Path(self.directory.name) / "runs"
        self.runs.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def receipt(self, run_id="spec-1", **overrides):
        receipt = {
            "id": run_id,
            "backend": "codex",
            "model": "gpt-6.1-sol",
            "effort": "high",
            "status": "done",
            "duration_seconds": 75.0,
            "queued_seconds": 20.0,
            "usage": {"input_tokens": 1200, "output_tokens": 300},
            "artifacts": [{"name": "spec.json", "sha256": "a" * 64}],
        }
        receipt.update(overrides)
        run = self.runs / run_id
        run.mkdir()
        (run / "result.json").write_text(json.dumps(receipt), encoding="utf-8")
        return receipt

    def evaluate(self, *, max_execution_seconds=60):
        completed = subprocess.run(
            [sys.executable, str(PILOT), "--runs", str(self.runs),
             "--min-samples", "1", "--max-execution-seconds", str(max_execution_seconds)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(completed.stderr, "")
        self.assertTrue(completed.stdout.endswith("\n"))
        return completed.returncode, json.loads(completed.stdout)

    def assert_rejected_for(self, reason):
        rc, report = self.evaluate()
        self.assertEqual(rc, 1)
        self.assertEqual(report["decision"], "reject")
        self.assertEqual(report["sample_count"], 1)
        self.assertEqual(report["reasons"], [{"run_id": "spec-1", "reason": reason}])

    def test_invalid_timing_and_usage_fail_closed(self):
        cases = [
            ({"duration_seconds": None}, "invalid_duration_seconds"),
            ({"queued_seconds": -1}, "invalid_queued_seconds"),
            ({"queued_seconds": 76}, "queued_exceeds_duration"),
            ({"duration_seconds": float("nan")}, "invalid_duration_seconds"),
            ({"queued_seconds": True}, "invalid_queued_seconds"),
            ({"usage": {}}, "missing_usage"),
            ({"usage": {"input_tokens": -1, "output_tokens": 4}}, "invalid_usage"),
            ({"usage": {"input_tokens": True, "output_tokens": 4}}, "invalid_usage"),
        ]
        for overrides, reason in cases:
            with self.subTest(overrides=overrides):
                path = self.runs / "spec-1"
                if path.exists():
                    (path / "result.json").unlink()
                    path.rmdir()
                self.receipt(**overrides)
                self.assert_rejected_for(reason)

    def test_profile_terminal_and_artifact_are_all_required(self):
        self.receipt(backend="pi", effort="low", status="running",
                     artifacts=[{"name": "spec.json", "sha256": "not-a-digest"}])
        rc, report = self.evaluate()
        self.assertEqual(rc, 1)
        self.assertEqual([r["reason"] for r in report["reasons"]], [
            "profile_mismatch:backend", "profile_mismatch:effort",
            "invalid_terminal_status", "missing_spec_artifact"])

    def test_malformed_receipts_produce_json_in_stable_directory_order(self):
        for name, content in (("z-run", "{broken"), ("a-run", "[]")):
            folder = self.runs / name
            folder.mkdir()
            (folder / "result.json").write_text(content)
        rc, report = self.evaluate()
        self.assertEqual(rc, 1)
        self.assertEqual(report["reasons"], [
            {"run_id": "a-run", "reason": "invalid_receipt"},
            {"run_id": "z-run", "reason": "invalid_receipt"}])
        self.assertEqual(report, self.evaluate()[1])

    def test_empty_directory_cannot_enable(self):
        rc, report = self.evaluate()
        self.assertEqual(rc, 1)
        self.assertEqual(report["sample_count"], 0)
        self.assertEqual(report["reasons"], [{"reason": "insufficient_samples"}])

    def test_receipt_identity_must_match_its_directory(self):
        self.receipt(id="different")
        self.assert_rejected_for("run_id_mismatch")

    def test_invalid_limits_cannot_enable_pool(self):
        self.receipt()
        for value in ("nan", "inf", "-1", "0"):
            with self.subTest(value=value):
                rc, report = self.evaluate(max_execution_seconds=value)
                self.assertEqual(rc, 1)
                self.assertEqual(report["reasons"], [{"reason": "invalid_limits"}])

    def test_linked_receipt_is_not_read(self):
        self.receipt()
        target = self.runs / "spec-1" / "result.json"
        outside = self.runs.parent / "outside.json"
        target.rename(outside)
        target.symlink_to(outside)
        self.assert_rejected_for("invalid_receipt")

    def test_result_and_inputs_are_stable_and_read_only(self):
        self.receipt("z-run")
        self.receipt("a-run")
        before = {path: path.read_bytes() for path in self.runs.glob("*/result.json")}
        rc, report = self.evaluate()
        self.assertEqual(rc, 0)
        self.assertEqual([row["run_id"] for row in report["samples"]], ["a-run", "z-run"])
        self.assertEqual(report, self.evaluate()[1])
        after = {path: path.read_bytes() for path in self.runs.glob("*/result.json")}
        self.assertEqual(before, after)
        self.assertEqual(sorted(path.name for path in self.runs.rglob("*") if path.is_file()),
                         ["result.json", "result.json"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_fifo_receipt_is_rejected_without_waiting_for_a_writer(self):
        folder = self.runs / "spec-1"
        folder.mkdir()
        os.mkfifo(folder / "result.json")
        self.assert_rejected_for("invalid_receipt")

    def test_deeply_nested_json_still_returns_a_rejection(self):
        folder = self.runs / "spec-1"
        folder.mkdir()
        (folder / "result.json").write_text("[" * 2000 + "]" * 2000)
        self.assert_rejected_for("invalid_receipt")

    def test_wrong_actual_model_rejects_role_pool(self):
        self.receipt(model="gpt-6.1-terra")
        self.assert_rejected_for("profile_mismatch:model")

    def test_execution_timeout_rejects_role_pool(self):
        self.receipt(duration_seconds=81.0, queued_seconds=20.0)
        self.assert_rejected_for("execution_timeout")

    def test_missing_usage_rejects_role_pool(self):
        self.receipt(usage=None)
        self.assert_rejected_for("missing_usage")

    def test_complete_sample_enables_pool_and_excludes_queue_time(self):
        self.receipt(duration_seconds=80.0, queued_seconds=20.0)
        rc, report = self.evaluate(max_execution_seconds=60)
        self.assertEqual(rc, 0)
        self.assertEqual(report, {
            "decision": "enable",
            "sample_count": 1,
            "reasons": [],
            "samples": [{"execution_seconds": 60.0, "run_id": "spec-1"}],
        })

    def test_missing_spec_artifact_and_insufficient_samples_are_stable_reasons(self):
        self.receipt(artifacts=[])
        completed = subprocess.run(
            [sys.executable, str(PILOT), "--runs", str(self.runs),
             "--min-samples", "2", "--max-execution-seconds", "60"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(json.loads(completed.stdout)["reasons"], [
            {"reason": "insufficient_samples"},
            {"run_id": "spec-1", "reason": "missing_spec_artifact"},
        ])


if __name__ == "__main__":
    unittest.main()
