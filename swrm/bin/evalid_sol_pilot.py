#!/usr/bin/env python3
"""Offline Sol Spec pilot gate (Refs #1978).

Read RUNS/<run_id>/result.json receipts, never dispatch or enable a pool.
Exit 0 means the supplied evidence meets the gate; exit 1 rejects it.
The report checks receipt metadata, not artifact contents or live quota.
Reasons are ordered by directory then by validation field, with sample-count
failure first. Queue time is subtracted only after validating both durations.
"""
import argparse
import json
import math
from pathlib import Path
import re


PROFILE = {"backend": "codex", "model": "gpt-6.1-sol", "effort": "high"}


def nonnegative_number(value):
    return (type(value) in (int, float) and math.isfinite(value) and value >= 0)


def validate_receipt(receipt, run_id, max_execution_seconds):
    """Return all reasons plus execution time, without executing receipt data."""
    reasons = []
    if not isinstance(receipt, dict):
        return ["invalid_receipt"], None
    if receipt.get("id") != run_id:
        reasons.append("run_id_mismatch")
    for field, expected in PROFILE.items():
        if receipt.get(field) != expected:
            reasons.append(f"profile_mismatch:{field}")
    if receipt.get("status") != "done":
        reasons.append("invalid_terminal_status")
    duration, queued = receipt.get("duration_seconds"), receipt.get("queued_seconds")
    execution = None
    if not nonnegative_number(duration):
        reasons.append("invalid_duration_seconds")
    if not nonnegative_number(queued):
        reasons.append("invalid_queued_seconds")
    if nonnegative_number(duration) and nonnegative_number(queued):
        if queued > duration:
            reasons.append("queued_exceeds_duration")
        else:
            execution = float(duration - queued)
            if execution > max_execution_seconds:
                reasons.append("execution_timeout")
    usage = receipt.get("usage")
    if not isinstance(usage, dict) or not {"input_tokens", "output_tokens"} <= usage.keys():
        reasons.append("missing_usage")
    elif any(type(usage[key]) is not int or usage[key] < 0
             for key in ("input_tokens", "output_tokens")):
        reasons.append("invalid_usage")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not any(
            isinstance(item, dict) and item.get("name") == "spec.json"
            and isinstance(item.get("sha256"), str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", item["sha256"])
            for item in artifacts):
        reasons.append("missing_spec_artifact")
    return reasons, execution


def evaluate(runs, min_samples=3, max_execution_seconds=60):
    """Summarize immediate run directories; unreadable input fails closed."""
    reasons, samples = [], []
    if (type(min_samples) is not int or min_samples < 1
            or not nonnegative_number(max_execution_seconds)
            or max_execution_seconds == 0):
        return {"decision": "reject", "sample_count": 0,
                "reasons": [{"reason": "invalid_limits"}], "samples": []}
    try:
        paths = sorted((path for path in Path(runs).iterdir()
                        if path.is_dir() or path.is_symlink()), key=lambda path: path.name)
    except OSError:
        paths = []
        reasons.append({"reason": "unreadable_runs"})
    if len(paths) < min_samples:
        reasons.insert(0, {"reason": "insufficient_samples"})
    for path in paths:
        result = path / "result.json"
        try:
            # A receipt cannot redirect the reader to another directory.
            if path.is_symlink() or result.is_symlink() or not result.is_file():
                raise ValueError("linked receipt")
            receipt = json.loads(result.read_text(encoding="utf-8"))
            errors, execution = validate_receipt(receipt, path.name, max_execution_seconds)
        except (OSError, ValueError, OverflowError, RecursionError):
            errors, execution = ["invalid_receipt"], None
        reasons.extend({"run_id": path.name, "reason": reason} for reason in errors)
        if execution is not None:
            samples.append({"execution_seconds": execution, "run_id": path.name})
    return {"decision": "reject" if reasons else "enable", "sample_count": len(paths),
            "reasons": reasons, "samples": samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--min-samples", type=int, default=3)
    parser.add_argument("--max-execution-seconds", type=float, default=60)
    args = parser.parse_args()
    report = evaluate(args.runs, args.min_samples, args.max_execution_seconds)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if report["decision"] == "enable" else 1


if __name__ == "__main__":
    raise SystemExit(main())
