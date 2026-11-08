"""Edge cases for reading swrm result receipts from mixed stdout."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
