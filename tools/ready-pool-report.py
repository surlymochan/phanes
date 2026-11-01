#!/usr/bin/env python3
"""Report dispatchable issue cards and lane capacity by backend (Refs #1560)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


def gh(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Small injectable gh runner; production calls use the normal gh executable."""
    try:
        result = subprocess.run(["gh", *args], text=True, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, "", str(error)
    return result.returncode, result.stdout, result.stderr


def labels(issue: dict) -> set[str]:
    return {row["name"] if isinstance(row, dict) else row
            for row in issue.get("labels", [])}


def lanes_from(data: dict) -> list[dict]:
    return [lane for host in data.get("hosts", {}).values()
            for lane in host.get("lanes", [])]


def busy_names(data: object) -> set[str]:
    """Accept autodispatch's lane map and the common list-shaped status snapshot."""
    if isinstance(data, list):
        return {str(row.get("name", row.get("lane", ""))) for row in data
                if isinstance(row, dict) and row.get("issue", row.get("busy", False))}
    if isinstance(data, dict):
        lanes = data.get("lanes", data)
        if isinstance(lanes, dict):
            return {str(name) for name, row in lanes.items()
                    if isinstance(row, dict) and (row.get("issue") or row.get("busy"))}
        if isinstance(lanes, list):
            return busy_names(lanes)
    return set()


def report(repo: str, inventory: dict, status: object | None, runner=gh) -> dict:
    lanes = lanes_from(inventory)
    backends = sorted({str(lane.get("backend", "unknown")) for lane in lanes})
    rc, out, err = runner(["issue", "list", "--repo", repo, "--state", "open",
                           "--limit", "1000", "--json", "number,title,body,labels"])
    if rc:
        raise RuntimeError(f"gh issue list failed: {err.strip()}")
    issues = json.loads(out)
    active = busy_names(status)
    counts = {backend: {"dispatchable": 0, "reasons": {"in-progress": 0,
              "external": 0, "dependency_open": 0}} for backend in backends}
    cache: dict[int, bool] = {}
    total_dispatchable = 0

    def dependency_open(number: int) -> bool:
        if number not in cache:
            code, raw, _ = runner(["issue", "view", str(number), "--repo", repo,
                                   "--json", "state"])
            cache[number] = code != 0 or json.loads(raw).get("state", "OPEN").upper() != "CLOSED"
        return cache[number]

    for issue in issues:
        tags = labels(issue)
        if "lane-ready" not in tags:
            continue
        pin = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("backend:")), None)
        # A card is external when explicitly starter-pinned or pinned outside this inventory.
        target = pin if pin in counts else (backends[0] if not pin and backends else None)
        reason = None
        if pin == "external" or (pin and pin not in counts):
            reason = "external"
        elif "in-progress" in tags:
            reason = "in-progress"
        else:
            deps = [int(n) for n in re.findall(r"#([1-9]\d*)", issue.get("body", ""))
                    if re.search(r"依赖\s*：", issue.get("body", ""))]
            if any(dependency_open(dep) for dep in deps):
                reason = "dependency_open"
        if reason:
            # Reasons are global to the queue; attribute pinned cards to their backend,
            # and unpinned cards to every backend that could otherwise consume them.
            for backend in ([target] if target else backends):
                counts[backend]["reasons"][reason] += 1
        else:
            total_dispatchable += 1
            for backend in backends:
                if target is None or backend == target or pin is None:
                    counts[backend]["dispatchable"] += 1

    result = {}
    for backend in backends:
        owned = [lane for lane in lanes if str(lane.get("backend", "unknown")) == backend]
        busy = sum(str(lane.get("name")) in active for lane in owned)
        idle = len(owned) - busy
        ready = counts[backend]["dispatchable"]
        result[backend] = {"dispatchable": ready, "busy": busy, "idle": idle,
                           "gap": max(0, idle - ready), **counts[backend]["reasons"]}
    return {"backends": result, "dispatchable": total_dispatchable}


def main(argv: list[str] | None = None, runner=gh) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--lanes", type=Path, required=True)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--target", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        inventory = json.loads(args.lanes.read_text(encoding="utf-8"))
        status = json.loads(args.status.read_text(encoding="utf-8")) if args.status else None
        data = report(args.repo, inventory, status, runner)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        parser.error(str(error))
    if args.as_json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print("BACKEND  READY  BUSY  IDLE  GAP  IN_PROGRESS  EXTERNAL  DEPENDENCY_OPEN")
        for backend, row in data["backends"].items():
            print(f"{backend:<8} {row['dispatchable']:>5} {row['busy']:>5} {row['idle']:>5} "
                  f"{row['gap']:>4} {row['in-progress']:>12} {row['external']:>9} "
                  f"{row['dependency_open']:>16}")
    return 3 if data["dispatchable"] < args.target else 0


if __name__ == "__main__":
    raise SystemExit(main())
