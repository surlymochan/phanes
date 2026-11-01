#!/usr/bin/env python3
"""Audit external lane issue/PR snapshots without GitHub or network access."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import time


def linked_issue(pr: dict, agent: str) -> int | None:
    """A closing reference takes precedence over the agent's branch convention."""
    closing = re.search(r"\bCloses\s+#([1-9]\d*)\b", pr.get("body") or "", re.I)
    if closing:
        return int(closing.group(1))
    branch = re.fullmatch(
        rf"fleet/{re.escape(agent)}-issue([1-9]\d*)-.+",
        (pr.get("head") or {}).get("ref", ""),
    )
    return int(branch.group(1)) if branch else None


def audit(issues: list[dict], prs: list[dict], agent: str, now: float) -> dict:
    """Return sorted issue rows and actionable alerts for one backend."""
    rows = {}
    for issue in issues:
        if "pull_request" in issue or issue.get("state", "open") != "open":
            continue
        labels = {
            label["name"] if isinstance(label, dict) else label
            for label in issue.get("labels", [])
        }
        if f"backend:{agent}" not in labels:
            continue
        # A terminal/progress label wins over a leftover ready label.
        stage = next(
            (stage for stage in ("failed", "pr", "claimed", "ready")
             if f"lane-{stage}" in labels),
            "unknown",
        )
        updated = datetime.fromisoformat(issue["updated_at"].replace("Z", "+00:00"))
        rows[issue["number"]] = {
            "number": issue["number"],
            "stage": stage,
            "idle_hours": max(0.0, (now - updated.timestamp()) / 3600),
            "prs": [],
        }

    alerts = []
    for pr in sorted(prs, key=lambda item: item["number"]):
        if pr.get("state", "open") != "open":
            continue
        number = linked_issue(pr, agent)
        head = (pr.get("head") or {}).get("ref", "")
        if number in rows:
            rows[number]["prs"].append(pr["number"])
        elif head.startswith(f"fleet/{agent}-"):
            alerts.append({"kind": "unlinked-pr", "pr": pr["number"], "head": head})

    ordered = [rows[number] for number in sorted(rows)]
    for row in ordered:
        if row["stage"] == "claimed":
            if row["prs"]:
                alerts.append({"kind": "label-lag", "issue": row["number"],
                               "prs": row["prs"]})
            elif row["idle_hours"] > 6:
                alerts.append({"kind": "stale-claim", "issue": row["number"]})
    return {"agent": agent, "issues": ordered, "alerts": alerts}


def read_snapshot(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError(f"{path}: expected a JSON array of objects")
    return data


def print_table(report: dict) -> None:
    print("ISSUE  STAGE    IDLE_HOURS  PRS")
    for row in report["issues"]:
        prs = ",".join(f"#{number}" for number in row["prs"]) or "-"
        print(f"#{row['number']:<5} {row['stage']:<8} {row['idle_hours']:>10.2f}  {prs}")
    for alert in report["alerts"]:
        target = f"issue #{alert['issue']}" if "issue" in alert else f"PR #{alert['pr']}"
        print(f"WARNING {alert['kind']}: {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issues", type=Path, required=True)
    parser.add_argument("--prs", type=Path, required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--now", type=float, default=None, help="Unix epoch seconds")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        report = audit(read_snapshot(args.issues), read_snapshot(args.prs), args.agent,
                       time.time() if args.now is None else args.now)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print_table(report)
    return int(bool(report["alerts"]))


if __name__ == "__main__":
    raise SystemExit(main())
