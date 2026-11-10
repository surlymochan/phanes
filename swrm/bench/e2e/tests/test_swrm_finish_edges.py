"""CLI and result receipt edge cases for swrm_finish."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FINISH = ROOT / "harness" / "swrm_finish.py"
sys.path.insert(0, str(ROOT))

from harness.swrm_finish import extract_result_object  # noqa: E402


def test_receipt_ignores_unrelated_trailing_json_object():
    receipt = {
        "id": "run-123",
        "status": "done",
        "workspace": {"mode": "worktree", "path": "/tmp/run-123"},
        "run_dir": "/tmp/swrm/runs/run-123",
    }
    telemetry = {"id": "metrics-456", "event": "usage", "count": 7}
    stdout = "starting run\n" + json.dumps(receipt) + "\n" + json.dumps(telemetry)

    assert extract_result_object(stdout) == receipt


def test_receipt_ignores_trailing_telemetry_with_attempt_metadata():
    receipt = {
        "id": "run-123",
        "status": "done",
        "workspace": {"mode": "worktree", "path": "/tmp/run-123"},
        "run_dir": "/tmp/swrm/runs/run-123",
    }
    telemetry = {
        "id": "metrics-456",
        "event": "usage",
        "attempts": [
            {
                "backend": "cursor",
                "model": "grok",
                "meta": {"usage": {"inputTokens": 8}},
            }
        ],
    }
    stdout = json.dumps(receipt) + "\n" + json.dumps(telemetry)

    assert extract_result_object(stdout) == receipt
    assert extract_result_object(json.dumps(telemetry)) is None


@pytest.mark.parametrize(
    "run_context",
    [
        {"workspace": {"mode": "worktree"}},
        {"run_dir": "/tmp/swrm/runs/metrics-456"},
    ],
)
def test_receipt_ignores_trailing_statusless_telemetry_with_run_context(run_context):
    receipt = {
        "id": "run-123",
        "status": "done",
        "workspace": {"mode": "worktree", "path": "/tmp/run-123"},
    }
    telemetry = {"id": "metrics-456", "event": "usage", "count": 7, **run_context}
    stdout = json.dumps(receipt) + "\n" + json.dumps(telemetry)

    assert extract_result_object(stdout) == receipt
    assert extract_result_object(json.dumps(telemetry)) is None


def test_receipt_keeps_support_for_multiple_stdout_objects():
    receipt = {
        "id": "run-789",
        "status": "failed",
        "run_dir": "/tmp/swrm/runs/run-789",
        "attempts": [],
    }
    stdout = "noise\n" + json.dumps({"ok": True}) + "\n" + json.dumps(receipt)

    assert extract_result_object(stdout) == receipt


def test_receipt_requires_run_result_shape():
    telemetry = {"id": "metrics-456", "status": "usage", "event": "usage"}

    assert extract_result_object(json.dumps(telemetry)) is None


@pytest.mark.parametrize("result", ["ok", {"tokens": 7}, None])
def test_receipt_ignores_trailing_log_with_result_field(result):
    receipt = {
        "id": "run-123",
        "status": "done",
        "workspace": {"mode": "worktree", "path": "/tmp/run-123"},
    }
    telemetry = {"id": "metrics-456", "event": "usage", "result": result}
    stdout = json.dumps(receipt) + "\n" + json.dumps(telemetry)

    assert extract_result_object(stdout) == receipt
    assert extract_result_object(json.dumps(telemetry)) is None


@pytest.mark.parametrize(
    "metadata",
    [
        {"attempts": []},
        {"attempts": [], "status": "done"},
        {"attempts": [{"retry": 1}]},
        {"attempts": [None]},
        {"workspace": {}},
        {"run_dir": ""},
        {"run_dir": "   "},
        {"status": "usage", "run_dir": "/tmp/metrics"},
    ],
)
def test_receipt_ignores_trailing_retry_metrics(metadata):
    receipt = {
        "id": "run-123",
        "status": "done",
        "workspace": {"mode": "worktree", "path": "/tmp/run-123"},
    }
    telemetry = {"id": "metrics-456", "event": "retry_metrics", **metadata}
    stdout = json.dumps(receipt) + "\n" + json.dumps(telemetry)

    assert extract_result_object(stdout) == receipt
    assert extract_result_object(json.dumps(telemetry)) is None


@pytest.mark.parametrize("status", ["done", "partial", "failed", "stopped", "budget_exceeded"])
def test_receipt_returns_last_run_result(status):
    receipt = {"id": "run-123", "status": status, "run_dir": "/tmp/swrm/runs/run-123"}
    stdout = json.dumps({**receipt, "id": "previous-run"}) + "\n" + json.dumps(receipt)

    assert extract_result_object(stdout) == receipt


def test_unroutable_receipt_without_run_context_wins_over_previous_receipt():
    previous = {"id": "previous", "status": "done", "run_dir": "runs/previous"}
    receipt = {"id": "current", "status": "unroutable", "scope": "task",
               "decision": {"ok": False, "reasons": [], "skipped": {},
                            "planner": {}, "profile": {}}}
    telemetry = {"id": "metrics", "event": "usage", "run_dir": "runs/current"}
    assert extract_result_object(json.dumps(receipt)) == receipt
    assert extract_result_object("\n".join(map(json.dumps, [previous, receipt, telemetry]))) == receipt
    assert extract_result_object(json.dumps({"id": "metrics", "status": "unroutable"})) is None


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
