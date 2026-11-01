#!/usr/bin/env python3
"""Offline check for ghost in-progress labels without lane state (Refs #922).

In dual-mode dispatch, an issue may linger with an `in-progress` label while
`state.json` has no record of an active lane running that issue. This tool provides
offline detection against a state fixture and an issue-label fixture, emitting the
recommended remediation (drop `in-progress` and re-label `lane-ready`) without
touching GitHub.

Two guards keep the detection honest:

* an issue whose `in-progress` label is still fresh is an **in-flight dispatch**
  (`autodispatch.py` adds the label before it records the lane in `state.json`),
  never a ghost;
* the remediation follows the contracts already on `main`: `lane-pr` issues are
  protected, and a `lane-failed` issue may only be re-labelled `lane-ready` when
  `state.json` has no lane occupancy and the issue body still carries the
  acceptance command (#960).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# `dispatch()` adds `in-progress` before it writes the lane into `state.json`; the
# window is one SSH task upload (timeout 60s) plus one `lane assign` (timeout 180s),
# so ~240s worst case. Only a label older than 600s is treated as stale.
GHOST_MIN_AGE_S = 600

ACCEPTANCE_RE = re.compile(r"^\s*(?:验收|Acceptance|Verify|verify)\s*[:：].*`[^`]+`", re.MULTILINE)


def normalize_labels(labels_field: Any) -> set[str]:
    """Extract string label names from either list of strings or list of label dicts."""
    labels: set[str] = set()
    if not labels_field:
        return labels
    for item in labels_field:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str):
                labels.add(name.strip())
        elif isinstance(item, str):
            labels.add(item.strip())
    return labels


def extract_active_issues(state_data: dict[str, Any]) -> set[int]:
    """Extract active issue numbers from state.json dictionary."""
    active: set[int] = set()
    for k, v in state_data.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and "issue" in v:
            try:
                active.add(int(v["issue"]))
            except (ValueError, TypeError):
                continue
    return active


def state_has_occupancy(state_data: dict[str, Any]) -> bool:
    """Mirror of `check-lane-failed-requeue.py`: any live lane entry blocks a requeue."""
    return any(not str(key).startswith("_") and value for key, value in state_data.items())


def acceptance_command_present(text: str) -> bool:
    """Mirror of `check-lane-failed-requeue.py`: the issue must keep its acceptance command."""
    return bool(ACCEPTANCE_RE.search(text or ""))


def parse_timestamp(value: Any) -> float | None:
    """Parse a `gh issue list` `updatedAt` value into epoch seconds (None when unusable)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def format_remediation_command(
    issue_number: int,
    labels: set[str],
    repo: str | None = None,
    requeue: bool = True,
) -> str:
    """Build the `gh issue edit` command that drops the stale labels and re-queues the issue.

    A `lane-failed` issue must lose `lane-failed` as well, otherwise the re-labelled card
    would carry `lane-failed` + `lane-ready` and stay out of the dispatchable queue (#903).
    """
    repo_flag = f" --repo {repo}" if repo else ""
    parts = ["--remove-label", "in-progress"]
    if "lane-failed" in labels:
        parts += ["--remove-label", "lane-failed"]
    if requeue:
        parts += ["--add-label", "lane-ready"]
    return f"gh issue edit {issue_number}{repo_flag} " + " ".join(parts)


def classify_issue(
    issue: dict[str, Any],
    active_issues: set[int],
    state_data: dict[str, Any],
    now: float,
    min_age_s: float,
    repo: str | None,
) -> dict[str, Any] | None:
    """Classify one issue row; returns None for issues that cannot be ghost candidates."""
    state = issue.get("state", "OPEN")
    if isinstance(state, str) and state.upper() not in ("OPEN", ""):
        return None

    try:
        num = int(issue["number"])
    except (KeyError, ValueError, TypeError):
        return None

    labels = normalize_labels(issue.get("labels", []))
    if "in-progress" not in labels:
        return None

    updated = parse_timestamp(issue.get("updatedAt"))
    age = None if updated is None else now - updated
    row: dict[str, Any] = {
        "number": num,
        "title": issue.get("title", ""),
        "labels": sorted(labels),
        "age_s": age,
        "status": "",
        "note": "",
        "remediation": None,
    }

    if num in active_issues:
        row["status"] = "owned"
        row["note"] = "state.json still records a lane for this issue"
        return row
    if "lane-pr" in labels:
        row["status"] = "protected"
        row["note"] = "lane-pr issue: main protects it (autodispatch.py reconcile), not a ghost"
        return row
    if updated is None:
        row["status"] = "unknown"
        row["note"] = "no usable updatedAt: staleness cannot be proven, treated as in-flight"
        return row
    if age < min_age_s:
        row["status"] = "in-flight"
        row["note"] = (
            f"in-progress is only {int(age)}s old (< {int(min_age_s)}s): "
            "the dispatch may still be in flight, not a ghost"
        )
        return row

    body = issue.get("body") or ""
    if "lane-failed" in labels:
        row["status"] = "ghost-lane-failed"
        if state_has_occupancy(state_data):
            row["remediation"] = format_remediation_command(num, labels, repo=repo, requeue=False)
            row["note"] = (
                "lane-failed requeue needs an idle state.json (no lane occupancy) and the "
                "acceptance command kept in the issue body (#960)"
            )
        elif not acceptance_command_present(body):
            row["remediation"] = format_remediation_command(num, labels, repo=repo, requeue=False)
            row["note"] = (
                "lane-failed requeue needs the acceptance command kept in the issue body "
                "(#960); restore it before re-adding lane-ready"
            )
        else:
            row["remediation"] = format_remediation_command(num, labels, repo=repo, requeue=True)
            row["note"] = (
                "drop in-progress and lane-failed, then re-label lane-ready (#960 contract honoured)"
            )
        return row

    row["status"] = "ghost"
    row["remediation"] = format_remediation_command(num, labels, repo=repo, requeue=True)
    row["note"] = "drop the stale in-progress label and re-label lane-ready"
    return row


def scan_issues(
    state_data: dict[str, Any],
    issues: list[dict[str, Any]],
    repo: str | None = None,
    now: float | None = None,
    min_age_s: float = GHOST_MIN_AGE_S,
) -> list[dict[str, Any]]:
    """Classify every `in-progress` issue row against the state fixture."""
    now = time.time() if now is None else now
    active_issues = extract_active_issues(state_data)
    rows: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        row = classify_issue(issue, active_issues, state_data, now, min_age_s, repo)
        if row:
            rows.append(row)
    return rows


def report(rows: list[dict[str, Any]], active_issues: set[int]) -> None:
    """Print the ghosts with their remediation and the skipped candidates with the reason."""
    ghosts = [r for r in rows if r["status"].startswith("ghost")]
    skipped = [r for r in rows if not r["status"].startswith("ghost")]
    print("Offline ghost in-progress check (Refs #922):")
    print(f"  Active issues in state.json: {sorted(active_issues)}")
    print(f"  Detected {len(ghosts)} ghost in-progress issue(s):")
    for r in ghosts:
        age = "unknown age" if r["age_s"] is None else f"{int(r['age_s'])}s old"
        print(f"    - #{r['number']}: {r['title']} (labels: {', '.join(r['labels'])}; {age})")
        print(f"      Remediation: {r['remediation']}")
        print(f"      Note: {r['note']}")
    print(f"  Skipped {len(skipped)} candidate(s) that are not ghosts:")
    for r in skipped:
        print(f"    - #{r['number']}: {r['status']} — {r['note']}")


def iso(epoch: float) -> str:
    """Render an epoch as the ISO-8601 `gh issue list` `updatedAt` string."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_self_test() -> int:
    """Offline self-test with fake state.json and fake issue label list."""
    now = 1762000000.0
    stale = iso(now - 3600)  # one hour old: the label is stale enough to be a ghost
    fresh = iso(now - 120)  # dispatch() may still be running: in-flight, not a ghost
    fake_state = {
        "L1": {"issue": 101, "run": "run-001", "host": "ib1"},
        "L2": {"issue": 102, "run": "run-002", "host": "ib2"},
        "_done": [98, 99, 100],
    }
    idle_state = {"_done": [98, 99, 100]}
    fake_issues = [
        {"number": 101, "title": "Active run in L1", "labels": [{"name": "in-progress"}, {"name": "backend:codex"}], "state": "OPEN", "updatedAt": fresh},
        {"number": 102, "title": "Active run in L2", "labels": ["in-progress", "lane-ready"], "state": "OPEN", "updatedAt": fresh},
        {"number": 103, "title": "Queued task", "labels": ["lane-ready"], "state": "OPEN", "updatedAt": fresh},
        {"number": 104, "title": "Ghost run without lane in state.json", "labels": ["in-progress", "backend:pi"], "state": "OPEN", "updatedAt": stale},
        {"number": 105, "title": "Ghost run double labelled", "labels": [{"name": "in-progress"}, {"name": "lane-ready"}], "state": "OPEN", "updatedAt": stale},
        {"number": 106, "title": "Closed issue with leftover in-progress", "labels": ["in-progress"], "state": "CLOSED", "updatedAt": stale},
        {"number": 107, "title": "Unrelated issue", "labels": ["documentation"], "state": "OPEN", "updatedAt": stale},
        {"number": 108, "title": "Dispatch still in flight", "labels": ["in-progress"], "state": "OPEN", "updatedAt": fresh},
        {"number": 109, "title": "Already has a PR", "labels": ["in-progress", "lane-pr"], "state": "OPEN", "updatedAt": stale},
        {"number": 110, "title": "Failed lane without lane state", "labels": ["in-progress", "lane-failed"], "state": "OPEN", "updatedAt": stale, "body": "验收：`python3 tools/check-ghost-in-progress.py`"},
        {"number": 111, "title": "Failed lane without acceptance command", "labels": ["in-progress", "lane-failed"], "state": "OPEN", "updatedAt": stale, "body": "no acceptance command here"},
        {"number": 112, "title": "Ghost candidate without updatedAt", "labels": ["in-progress"], "state": "OPEN"},
    ]

    active = extract_active_issues(fake_state)
    assert active == {101, 102}, f"Unexpected active issues: {active}"

    rows = scan_issues(fake_state, fake_issues, now=now)
    by_number = {r["number"]: r for r in rows}
    ghost_numbers = [r["number"] for r in rows if r["status"].startswith("ghost")]
    assert ghost_numbers == [104, 105, 110, 111], f"Expected ghost issues [104, 105, 110, 111], got {ghost_numbers}"

    # A dispatch that is still in flight must not be reported as a ghost.
    assert by_number[108]["status"] == "in-flight", by_number[108]
    assert by_number[108]["remediation"] is None, by_number[108]
    # A lane that owns the issue is not a ghost either.
    assert by_number[101]["status"] == "owned", by_number[101]
    assert by_number[102]["status"] == "owned", by_number[102]
    # `lane-pr` issues are protected by main's orphan recycle path.
    assert by_number[109]["status"] == "protected", by_number[109]
    assert by_number[109]["remediation"] is None, by_number[109]
    # Without `updatedAt` the staleness cannot be proven.
    assert by_number[112]["status"] == "unknown", by_number[112]
    assert by_number[112]["remediation"] is None, by_number[112]

    # Plain ghosts: drop `in-progress`, re-label `lane-ready`.
    assert by_number[104]["remediation"] == "gh issue edit 104 --remove-label in-progress --add-label lane-ready"
    assert by_number[105]["remediation"] == "gh issue edit 105 --remove-label in-progress --add-label lane-ready"

    # `lane-failed` ghosts: `lane-failed` must go as well, and `lane-ready` is only
    # re-added when the #960 preconditions hold (idle state.json + acceptance command).
    assert by_number[110]["remediation"] == "gh issue edit 110 --remove-label in-progress --remove-label lane-failed"
    assert "--add-label lane-ready" not in by_number[110]["remediation"]
    assert by_number[111]["remediation"] == "gh issue edit 111 --remove-label in-progress --remove-label lane-failed"
    assert "--add-label lane-ready" not in by_number[111]["remediation"]

    idle_rows = {r["number"]: r for r in scan_issues(idle_state, [fake_issues[9]], now=now)}
    assert idle_rows[110]["remediation"] == (
        "gh issue edit 110 --remove-label in-progress --remove-label lane-failed --add-label lane-ready"
    ), idle_rows[110]

    for r in rows:
        if r["status"].startswith("ghost"):
            assert "--remove-label in-progress" in r["remediation"], r

    report(rows, active)
    print("ghost in-progress detection: OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline detection and remediation for ghost in-progress labels without lane state (Refs #922)."
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        help="Path to state.json fixture (default: runs built-in offline test)",
    )
    parser.add_argument(
        "--issues-file",
        type=Path,
        help="Path to JSON file containing issues fixture with labels (default: runs built-in offline test)",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Optional repository name to include in remediation commands",
    )
    parser.add_argument(
        "--min-age-seconds",
        type=float,
        default=GHOST_MIN_AGE_S,
        help=f"Minimum age of the in-progress label before it counts as a ghost (default: {GHOST_MIN_AGE_S})",
    )
    parser.add_argument(
        "--now",
        type=float,
        default=None,
        help="Override 'now' (epoch seconds) so the staleness check is reproducible offline",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run offline self-test with fake state.json and fake labels",
    )
    args = parser.parse_args()

    if args.self_test or (args.state_file is None and args.issues_file is None):
        return run_self_test()

    if args.state_file is None or args.issues_file is None:
        parser.error("Both --state-file and --issues-file must be provided together, or use --self-test")

    try:
        state_data = json.loads(args.state_file.read_text(encoding="utf-8"))
    except Exception as e:
        sys.stderr.write(f"Error reading state file {args.state_file}: {e}\n")
        return 1

    if not isinstance(state_data, dict):
        sys.stderr.write(f"State file {args.state_file} must contain a JSON object\n")
        return 1

    try:
        issues_data = json.loads(args.issues_file.read_text(encoding="utf-8"))
    except Exception as e:
        sys.stderr.write(f"Error reading issues file {args.issues_file}: {e}\n")
        return 1

    if not isinstance(issues_data, list):
        sys.stderr.write("Issues file must contain a JSON array of issue objects\n")
        return 1

    rows = scan_issues(
        state_data,
        issues_data,
        repo=args.repo,
        now=args.now,
        min_age_s=args.min_age_seconds,
    )
    report(rows, extract_active_issues(state_data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
