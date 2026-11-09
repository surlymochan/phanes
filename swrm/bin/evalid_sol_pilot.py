#!/usr/bin/env python3
"""Offline Sol Spec pilot gate (Refs #1978).

Read RUNS/<run_id>/result.json receipts, never dispatch or enable a pool.
Exit 0 means the supplied evidence meets the gate; exit 1 rejects it.
Actual profiles come from attempts, with a successful final attempt required.
Only evalid-spec/spec receipts qualify. Batched Spec artifacts are read from
--artifact-sink/<batch>/<name>; legacy unbatched names are relative to the run
directory. Their local bytes must match the declared SHA-256. No live quota
query is performed.
Reasons are ordered by directory then by validation field, with sample-count
failure first. Native seconds already excludes queue time; legacy total
duration_seconds includes it and requires subtraction after validation.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re


PROFILE = {"backend": "codex", "model": "gpt-6.1-sol", "effort": "high"}


def nonnegative_number(value):
    return (type(value) in (int, float) and math.isfinite(value) and value >= 0)


def validate_spec_artifacts(artifacts, run_dir, artifact_sink=None):
    """Verify local Spec evidence without following receipt-controlled links."""
    if not isinstance(artifacts, list):
        return "missing_spec_artifact"
    found = False
    for item in artifacts:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or Path(item["name"]).name != "spec.json"):
            continue
        found = True
        name, digest = Path(item["name"]), item.get("sha256")
        if (name.is_absolute() or ".." in name.parts
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
            return "missing_spec_artifact"
        try:
            source = run_dir
            parts = name.parts
            if "batch" in item:
                batch = item["batch"]
                if (artifact_sink is None or not isinstance(batch, str) or not batch
                        or Path(batch).is_absolute() or ".." in Path(batch).parts
                        or not Path(batch).parts):
                    return "missing_spec_artifact"
                source = Path(artifact_sink)
                parts = (*Path(batch).parts, *parts)
            if source.is_symlink():
                return "missing_spec_artifact"
            for part in parts:
                source = source / part
                if source.is_symlink():
                    return "missing_spec_artifact"
            if not source.is_file():
                return "missing_spec_artifact"
            actual = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    actual.update(chunk)
        except (OSError, ValueError):
            return "missing_spec_artifact"
        if actual.hexdigest() != digest.lower():
            return "spec_artifact_digest_mismatch"
    return None if found else "missing_spec_artifact"


def validate_receipt(receipt, run_id, max_execution_seconds, run_dir, artifact_sink=None):
    """Return all reasons plus execution time, without executing receipt data."""
    reasons = []
    if not isinstance(receipt, dict):
        return ["invalid_receipt"], None
    if receipt.get("id") != run_id:
        reasons.append("run_id_mismatch")
    if receipt.get("evalid_role") != "evalid-spec":
        reasons.append("invalid_evalid_role")
    if receipt.get("evalid_stage") != "spec":
        reasons.append("invalid_evalid_stage")
    for field, expected in PROFILE.items():
        if receipt.get(field) != expected:
            reasons.append(f"profile_mismatch:{field}")
    attempts = receipt.get("attempts")
    if (not isinstance(attempts, list) or not attempts
            or any(not isinstance(attempt, dict) for attempt in attempts)):
        reasons.append("invalid_attempts")
    else:
        # A configured profile cannot certify a fallback to a different model.
        for field, expected in PROFILE.items():
            if any(attempt.get(field) != expected for attempt in attempts):
                reasons.append(f"attempt_profile_mismatch:{field}")
        final = attempts[-1]
        if (type(final.get("rc")) is not int or final["rc"] != 0
                or final.get("error_class") is not None):
            reasons.append("invalid_terminal_attempt")
    if receipt.get("status") != "done":
        reasons.append("invalid_terminal_status")
    # swrm sums attempt seconds measured after provider-slot admission.
    # Keep support for the total-duration receipts used by offline fixtures.
    native_seconds = "seconds" in receipt
    duration_field = "seconds" if native_seconds else "duration_seconds"
    duration, queued = receipt.get(duration_field), receipt.get("queued_seconds")
    execution = None
    if not nonnegative_number(duration):
        reasons.append(f"invalid_{duration_field}")
    if not nonnegative_number(queued):
        reasons.append("invalid_queued_seconds")
    if nonnegative_number(duration) and nonnegative_number(queued):
        if not native_seconds and queued > duration:
            reasons.append("queued_exceeds_duration")
        else:
            execution = float(duration if native_seconds else duration - queued)
            if execution > max_execution_seconds:
                reasons.append("execution_timeout")
    usage = receipt.get("usage")
    if not isinstance(usage, dict) or not {"input_tokens", "output_tokens"} <= usage.keys():
        reasons.append("missing_usage")
    elif any(type(usage[key]) is not int or usage[key] < 0
             for key in ("input_tokens", "output_tokens")):
        reasons.append("invalid_usage")
    artifact_error = validate_spec_artifacts(receipt.get("artifacts"), run_dir, artifact_sink)
    if artifact_error:
        reasons.append(artifact_error)
    return reasons, execution


def evaluate(runs, min_samples=3, max_execution_seconds=60, artifact_sink=None):
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
            errors, execution = validate_receipt(
                receipt, path.name, max_execution_seconds, path, artifact_sink)
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
    parser.add_argument("--artifact-sink", type=Path,
                        help="local artifact sink required for receipts with batch entries")
    parser.add_argument("--min-samples", type=int, default=3)
    parser.add_argument("--max-execution-seconds", type=float, default=60)
    args = parser.parse_args()
    report = evaluate(args.runs, args.min_samples, args.max_execution_seconds, args.artifact_sink)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if report["decision"] == "enable" else 1


if __name__ == "__main__":
    raise SystemExit(main())
