#!/usr/bin/env python3
"""Offline contract for the lane status snapshot and GH ready queue.

The live dispatcher writes ``_busy`` and ``_ready`` to lanes-status.json.
``_ready`` is the count of open lane-ready issues that are not already owned,
finished, or failed.  This check keeps that definition executable without GH
credentials or a network connection.
"""

from __future__ import annotations

import json
from typing import Any, Iterable


EXCLUDING_LABELS = {"in-progress", "lane-pr", "lane-failed"}


def label_names(issue: dict[str, Any]) -> set[str]:
    """Accept the objects returned by ``gh --json labels`` (and simple names)."""
    labels = issue.get("labels", [])
    return {
        label if isinstance(label, str) else label["name"]
        for label in labels
        if isinstance(label, str) or isinstance(label, dict) and "name" in label
    }


def dispatchable_ready(issues: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return open, unclaimed ``lane-ready`` issues in the GH result."""
    return [
        issue
        for issue in issues
        if issue.get("state", "OPEN") == "OPEN"
        and "lane-ready" in label_names(issue)
        and not label_names(issue) & EXCLUDING_LABELS
    ]


def check_summary(status: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    """Assert the two aggregate fields agree with their source values."""
    busy_lanes = sum(
        isinstance(value, str) and value.startswith("busy ")
        for key, value in status.items()
        if not key.startswith("_")
    )
    ready_issues = dispatchable_ready(issues)
    assert status.get("_busy") == busy_lanes, (
        f"_busy={status.get('_busy')} but {busy_lanes} lane(s) are busy"
    )
    assert status.get("_ready") == len(ready_issues), (
        f"_ready={status.get('_ready')} but GH has {len(ready_issues)} dispatchable issue(s)"
    )


def main() -> None:
    # Deliberately fake data: this command must remain safe to run on a laptop
    # and must not require GH auth or touch the live autodispatch directory.
    status = json.loads(
        '{"CX1":"busy #980","CX2":"idle","_busy":1,"_ready":2,"_blocked":1}'
    )
    issues = json.loads(
        '[{"number":101,"state":"OPEN","labels":[{"name":"lane-ready"}]},'
        '{"number":102,"state":"OPEN","labels":["lane-ready","p1"]},'
        '{"number":103,"state":"OPEN","labels":["lane-ready","in-progress"]},'
        '{"number":104,"state":"CLOSED","labels":["lane-ready"]}]'
    )
    check_summary(status, issues)
    assert [issue["number"] for issue in dispatchable_ready(issues)] == [101, 102]
    print("lanes status summary contract: OK")


if __name__ == "__main__":
    main()
