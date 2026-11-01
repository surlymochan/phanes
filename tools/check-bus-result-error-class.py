#!/usr/bin/env python3
"""Check that issue-bus result comments use the stable error_class enum (Refs #1228)."""

import json
import re


RESULT_RE = re.compile(r"```openswarm-result\s*\n(.*?)\n```", re.S)
REQUIRED = (
    "status",
    "run_id",
    "summary",
    "files_changed",
    "tests",
    "followups",
    "branch",
    "commit",
    "error_class",
)
SUCCESS = {"done", "partial"}
ERROR_CLASSES = {
    "none",
    "auth",
    "base_unavailable",
    "budget_exceeded",
    "budget_exceeded_daily",
    "bus_error",
    "claim_task_mutated",
    "compare_runner_failed",
    "exec_error",
    "executor_failed",
    "executor_io_lost",
    "model_unavailable",
    "network",
    "non-fast-forward",
    "no_report",
    "prepare_failed",
    "protocol_error",
    "rate_limit",
    "repo-missing",
    "risk_blocked",
    "stopped",
    "upstream_forbidden",
    "verify_failed",
    "worker_crashed",
    "worktree_unavailable",
}


def parse_result(comment):
    matches = list(RESULT_RE.finditer(comment))
    if len(matches) != 1 or comment[matches[0].end():].strip():
        raise AssertionError("result must contain one fenced openswarm-result payload")
    payload = json.loads(matches[0].group(1))
    if not isinstance(payload, dict) or any(field not in payload for field in REQUIRED):
        raise AssertionError("result is missing a required field")
    status = payload["status"]
    error_class = payload["error_class"]
    if not isinstance(status, str) or status not in SUCCESS | {"failed"}:
        raise AssertionError(f"unsupported status: {status!r}")
    if not isinstance(error_class, str) or error_class not in ERROR_CLASSES:
        raise AssertionError(f"unsupported error_class: {error_class!r}")
    if status == "failed" and error_class == "none":
        raise AssertionError("failed result requires a non-none error_class")
    if status in SUCCESS and error_class != "none":
        raise AssertionError("successful result must use error_class=none")
    return payload


def main():
    success = """```openswarm-result
{"status":"done","run_id":"run-success","error_class":"none","summary":"completed","files_changed":[],"tests":"ok","followups":[],"branch":"openswarm/run-success","commit":null}
```"""
    failure = """```openswarm-result
{"status":"failed","run_id":"run-failure","error_class":"verify_failed","summary":"verification failed","files_changed":[],"tests":"not run: verification failed","followups":[],"branch":null,"commit":null}
```"""
    assert parse_result(success)["error_class"] == "none"
    assert parse_result(failure)["error_class"] == "verify_failed"

    # 缺字段拒绝：针对 REQUIRED 中的每个字段，缺失均必须抛出异常
    for sample in (success, failure):
        for field in REQUIRED:
            data = json.loads(RESULT_RE.search(sample).group(1))
            del data[field]
            missing_sample = f"```openswarm-result\n{json.dumps(data)}\n```"
            try:
                parse_result(missing_sample)
            except AssertionError:
                pass
            else:
                raise AssertionError(f"missing field {field!r} must be rejected")

    unsupported_error_class = failure.replace('"verify_failed"', '"unknown_failure"')
    try:
        parse_result(unsupported_error_class)
    except AssertionError:
        pass
    else:
        raise AssertionError("unsupported error_class must be rejected")

    failed_with_none = failure.replace('"verify_failed"', '"none"')
    try:
        parse_result(failed_with_none)
    except AssertionError:
        pass
    else:
        raise AssertionError("failed result with error_class=none must be rejected")

    success_with_error = success.replace('"none"', '"exec_error"')
    try:
        parse_result(success_with_error)
    except AssertionError:
        pass
    else:
        raise AssertionError("successful result with non-none error_class must be rejected")

    print("bus result error_class contract: success, failure, and missing-field samples passed")


if __name__ == "__main__":
    main()
