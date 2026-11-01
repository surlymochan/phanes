#!/usr/bin/env python3
"""Check the machine-readable status= / error_class= issue-bus contract."""

import json
import re


RESULT_RE = re.compile(r"```openswarm-result\s*\n(.*?)\n```", re.S)
REQUIRED = ("status", "error_class", "summary", "files_changed", "tests", "followups", "branch", "commit")
SUCCESS = {"done", "partial"}


def parse_result_comment(comment):
    """Parse a fenced result comment and enforce its terminal fields."""
    matches = list(RESULT_RE.finditer(comment))
    if len(matches) != 1 or comment[matches[0].end():].strip():
        raise AssertionError("result comment must contain one fenced openswarm-result payload")
    payload = json.loads(matches[0].group(1))
    if not isinstance(payload, dict) or any(field not in payload for field in REQUIRED):
        raise AssertionError("result payload is missing a required field")
    status = payload["status"]
    error_class = payload["error_class"]
    if status not in SUCCESS | {"failed"}:
        raise AssertionError(f"unsupported status: {status!r}")
    if status == "failed" and (not isinstance(error_class, str) or not error_class.strip()):
        raise AssertionError("failed result requires a non-empty error_class")
    if status in SUCCESS and error_class not in (None, "", "none"):
        raise AssertionError("successful result error_class must be none or empty")
    return payload


def main():
    success = """```openswarm-result
{"status":"done","error_class":"none","summary":"completed","files_changed":[],"tests":"ok","followups":[],"branch":"openswarm/run-success","commit":null}
```"""
    failure = """```openswarm-result
{"status":"failed","error_class":"verify_failed","summary":"verification failed","files_changed":[],"tests":"not run: verification failed","followups":[],"branch":null,"commit":null}
```"""
    for sample, expected in ((success, "done"), (failure, "failed")):
        assert parse_result_comment(sample)["status"] == expected

    missing_error_class = success.replace(',"error_class":"none"', "")
    try:
        parse_result_comment(missing_error_class)
    except AssertionError:
        pass
    else:
        raise AssertionError("missing error_class must be rejected")
    print("bus result contract: success and failure samples passed")


if __name__ == "__main__":
    main()
