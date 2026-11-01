#!/usr/bin/env python3
"""Offline contract check for re-queuing a failed lane issue.

The checker only evaluates supplied/fake labels and state.  It never calls
GitHub and never reads or writes the live autodispatch state directory.
"""

from __future__ import annotations

import re


BLOCKING_LABELS = {"in-progress", "lane-pr"}


def state_has_occupancy(state: dict) -> bool:
    """Return whether state.json still contains an assigned lane slot.

    ``_done`` and other underscore-prefixed bookkeeping keys are not lane
    ownership; every other entry represents a live or stale assignment.
    """
    return any(not str(key).startswith("_") and value for key, value in state.items())


def acceptance_command_present(text: str) -> bool:
    """Recognise the issue's inline acceptance/verify command contract."""
    return bool(
        re.search(
            r"^\s*(?:验收|Acceptance|Verify|verify)\s*[:：].*`[^`]+`",
            text,
            re.MULTILINE,
        )
    )


def can_requeue(labels: set[str], state: dict, issue_body: str) -> bool:
    """Return whether ``lane-ready`` may be restored for a failed issue."""
    return (
        "lane-failed" in labels
        and not labels.intersection(BLOCKING_LABELS)
        and not state_has_occupancy(state)
        and acceptance_command_present(issue_body)
    )


def main() -> None:
    clean = {"lane-failed"}
    verify = "验收：`python3 tools/check-lane-failed-requeue.py`"

    assert can_requeue(clean, {}, verify)
    assert can_requeue(clean, {"_done": [960]}, verify)

    assert not can_requeue(clean | {"in-progress"}, {}, verify)
    assert not can_requeue(clean | {"lane-pr"}, {}, verify)
    assert not can_requeue(clean, {"lane-a": {"issue": 960, "run": "r1"}}, verify)
    assert not can_requeue(clean, {}, "验收：验收命令已删除")

    print("lane-failed requeue contract: OK")


if __name__ == "__main__":
    main()
