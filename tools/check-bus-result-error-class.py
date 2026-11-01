#!/usr/bin/env python3
"""Check the stable error_class enum used by issue-bus result comments."""

import json
import re


RESULT_RE = re.compile(r"```openswarm-result\s*\n(.*?)\n```", re.S)
ERROR_CLASSES = frozenset({
    "none",
    "base_unavailable",
    "auth",
    "non-fast-forward",
    "repo-missing",
    "network",
    "bus_error",
    "claim_task_mutated",
    "worker_crashed",
    "verify_failed",
    "executor_failed",
    "executor_io_lost",
    "push_failed",
    "protocol_error",
})
REQUIRED = ("status", "error_class")


def parse_result(comment):
    matches = list(RESULT_RE.finditer(comment))
    if len(matches) != 1 or comment[matches[0].end():].strip():
        raise AssertionError("result comment must contain one fenced openswarm-result payload")
    payload = json.loads(matches[0].group(1))
    if not isinstance(payload, dict) or any(field not in payload for field in REQUIRED):
        raise AssertionError("result payload is missing status or error_class")
    if payload["status"] not in {"done", "partial", "failed"}:
        raise AssertionError("unsupported result status")
    if payload["error_class"] not in ERROR_CLASSES:
        raise AssertionError("error_class is not in the stable enum")
    if payload["status"] != "failed" and payload["error_class"] != "none":
        raise AssertionError("successful results must use error_class=none")
    if payload["status"] == "failed" and payload["error_class"] == "none":
        raise AssertionError("failed results must use a non-none error_class")
    return payload


def main():
    success = """```openswarm-result
{"status":"done","error_class":"none"}
```"""
    failure = """```openswarm-result
{"status":"failed","error_class":"verify_failed"}
```"""
    assert parse_result(success)["status"] == "done"
    assert parse_result(failure)["status"] == "failed"

    missing = success.replace(',"error_class":"none"', "")
    try:
        parse_result(missing)
    except AssertionError:
        pass
    else:
        raise AssertionError("missing error_class must be rejected")
    print("bus result error_class: success, failure, and missing-field checks passed")


if __name__ == "__main__":
    main()
