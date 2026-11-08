"""Acceptance tests for the offline Sol Spec pilot dispatch gate."""

import json
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
