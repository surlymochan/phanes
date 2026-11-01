#!/usr/bin/env python3
"""Offline contract check for resident worker controlled restart and in-flight claim markers (Refs #971).

常驻 worker 异常退出后，重启应识别未完成 claim 并写 error 或续跑，不得双开同 claim。
`tools/check-bus-worker-restart.py` 假 claim 目录断言。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
from typing import Any


DEFAULT_LEASE_SECONDS = 300.0


class DuplicateClaimError(RuntimeError):
    """Raised when attempting to claim or execute an already in-flight task."""
    pass


def claim_marker_path(claim_dir: Path, run_id: str) -> Path:
    return claim_dir / run_id / "claim.json"


def record_in_flight_claim(
    claim_dir: Path,
    run_id: str,
    issue_number: int,
    worker_name: str,
    task: dict[str, Any],
    now: float,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    pid: int | None = None,
) -> dict[str, Any]:
    """Record an in-flight claim marker in the worker claim directory."""
    marker_file = claim_marker_path(claim_dir, run_id)
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    if marker_file.exists():
        existing = json.loads(marker_file.read_text(encoding="utf-8"))
        if existing.get("status") == "in-flight":
            raise DuplicateClaimError(f"claim {run_id} is already in-flight (不得双开同 claim)")

    marker = {
        "run_id": run_id,
        "issue_number": issue_number,
        "worker": worker_name,
        "status": "in-flight",
        "task": task,
        "claimed_at": now,
        "lease_expires_at": now + lease_seconds,
        "pid": pid,
    }
    marker_file.write_text(json.dumps(marker, indent=2, ensure_ascii=False), encoding="utf-8")
    return marker


def scan_in_flight_claims(claim_dir: Path, worker_name: str | None = None) -> list[dict[str, Any]]:
    """Scan claim directory for unfinished in-flight claims, verifying markers are preserved."""
    in_flight = []
    if not claim_dir.exists():
        return in_flight
    for child in sorted(claim_dir.iterdir()):
        marker_file = child / "claim.json"
        if not marker_file.is_file():
            continue
        try:
            data = json.loads(marker_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("status") == "in-flight":
            if worker_name is None or data.get("worker") == worker_name:
                in_flight.append(data)
    return in_flight


def reconcile_restart_claim(
    claim_dir: Path,
    claim: dict[str, Any],
    action: str,
    now: float,
    new_pid: int | None = None,
) -> dict[str, Any]:
    """Reconcile an uncompleted in-flight claim on worker restart without losing the claim marker."""
    marker_file = claim_marker_path(claim_dir, claim["run_id"])
    assert marker_file.is_file(), "in-flight claim marker must exist on disk"
    current = json.loads(marker_file.read_text(encoding="utf-8"))
    assert current["status"] == "in-flight", "reconciliation targets in-flight claims"

    if action == "error":
        result = {
            "status": "failed",
            "error_class": "worker_crashed",
            "summary": "worker restarted after crash with unfinished in-flight claim",
            "run_id": claim["run_id"],
            "files_changed": [],
            "tests": "not run: worker crashed during execution",
            "followups": ["reconciled on controlled restart"],
            "branch": None,
            "commit": None,
        }
        result_file = claim_dir / claim["run_id"] / "result.json"
        result_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        current["status"] = "failed"
        current["reconciled_at"] = now
        current["reconciliation"] = "error"
        current["error_class"] = "worker_crashed"
        marker_file.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        return result

    elif action == "resume":
        current["pid"] = new_pid
        current["resumed_at"] = now
        current["lease_expires_at"] = now + DEFAULT_LEASE_SECONDS
        current["reconciliation"] = "resumed"
        marker_file.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        return current

    else:
        raise ValueError(f"unknown reconciliation action: {action!r}")


def acquire_worker_claim(
    claim_dir: Path,
    run_id: str,
    issue_number: int,
    worker_name: str,
    task: dict[str, Any],
    now: float,
    pid: int,
) -> dict[str, Any]:
    """Attempt to claim a task, strictly rejecting duplicate claims for active runs."""
    marker_file = claim_marker_path(claim_dir, run_id)
    if marker_file.exists():
        existing = json.loads(marker_file.read_text(encoding="utf-8"))
        if existing.get("status") == "in-flight":
            raise DuplicateClaimError(
                f"cannot start duplicate claim for {run_id}: task is already in-flight (不得双开同 claim)"
            )
    return record_in_flight_claim(claim_dir, run_id, issue_number, worker_name, task, now, pid=pid)


def main() -> None:
    now = time.time()
    with tempfile.TemporaryDirectory(prefix="bus-claim-test-") as fixture:
        claim_dir = Path(fixture) / "bus-worker"
        claim_dir.mkdir(parents=True, exist_ok=True)

        task_payload = {"run_id": "run-971", "repo": "org/repo", "base": "a" * 40}

        # 1. Resident worker claims task and marks it in-flight
        marker = acquire_worker_claim(
            claim_dir,
            run_id="run-971",
            issue_number=971,
            worker_name="worker-alpha",
            task=task_payload,
            now=now,
            pid=1001,
        )
        assert marker["status"] == "in-flight"
        assert claim_marker_path(claim_dir, "run-971").is_file()

        # 2. Worker crashes while in-flight; assert restart does NOT lose the claim marker
        in_flight = scan_in_flight_claims(claim_dir, worker_name="worker-alpha")
        assert len(in_flight) == 1
        assert in_flight[0]["run_id"] == "run-971"
        assert in_flight[0]["status"] == "in-flight"

        # 3. Guard against duplicate claims: attempting to start/claim the same task must fail
        try:
            acquire_worker_claim(
                claim_dir,
                run_id="run-971",
                issue_number=971,
                worker_name="worker-alpha",
                task=task_payload,
                now=now + 10,
                pid=1002,
            )
        except DuplicateClaimError as err:
            assert "不得双开同 claim" in str(err)
        else:
            raise AssertionError("duplicate claim must be rejected when task is in-flight")

        # 4. Controlled restart with 'error' action: writes error result and records failure
        error_result = reconcile_restart_claim(
            claim_dir,
            in_flight[0],
            action="error",
            now=now + 20,
        )
        assert error_result["status"] == "failed"
        assert error_result["error_class"] == "worker_crashed"
        result_path = claim_dir / "run-971" / "result.json"
        assert result_path.is_file()
        assert json.loads(result_path.read_text(encoding="utf-8"))["status"] == "failed"

        # Verified: no longer in-flight after error reconciliation
        assert scan_in_flight_claims(claim_dir, worker_name="worker-alpha") == []

        # 5. Controlled restart with 'resume' action (续跑):
        task_payload_2 = {"run_id": "run-972", "repo": "org/repo", "base": "b" * 40}
        acquire_worker_claim(
            claim_dir,
            run_id="run-972",
            issue_number=972,
            worker_name="worker-alpha",
            task=task_payload_2,
            now=now + 30,
            pid=1003,
        )
        in_flight_2 = scan_in_flight_claims(claim_dir, worker_name="worker-alpha")
        assert len(in_flight_2) == 1
        assert in_flight_2[0]["run_id"] == "run-972"

        # Resume the task on restart
        resumed = reconcile_restart_claim(
            claim_dir,
            in_flight_2[0],
            action="resume",
            now=now + 40,
            new_pid=2001,
        )
        assert resumed["status"] == "in-flight"
        assert resumed["pid"] == 2001
        assert resumed["reconciliation"] == "resumed"

        # Still guarded against duplicate execution
        try:
            acquire_worker_claim(
                claim_dir,
                run_id="run-972",
                issue_number=972,
                worker_name="worker-alpha",
                task=task_payload_2,
                now=now + 45,
                pid=2002,
            )
        except DuplicateClaimError:
            pass
        else:
            raise AssertionError("resumed claim must still reject duplicate runs")

        # Transition to terminal done state
        marker_2 = claim_marker_path(claim_dir, "run-972")
        data_2 = json.loads(marker_2.read_text(encoding="utf-8"))
        data_2["status"] = "done"
        marker_2.write_text(json.dumps(data_2), encoding="utf-8")
        assert scan_in_flight_claims(claim_dir, worker_name="worker-alpha") == []

        print("OK: worker crash and restart preserves in-flight claim markers")
        print("OK: restart reconciles unfinished claims via error or resume")
        print("OK: duplicate execution on active in-flight claim rejected (不得双开同 claim)")


if __name__ == "__main__":
    main()
