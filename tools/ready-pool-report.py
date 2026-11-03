#!/usr/bin/env python3
"""Report dispatchable issue cards and lane capacity by backend (Refs #1560).

--lanes must be fleet inventory JSON: {"hosts": {host: {"lanes":
[{"name": ..., "backend": ...}]}}}. --status accepts dispatch snapshots
whose lane values are strings ("busy #980", "idle", "?") or lane maps.
When --status is omitted, capacity columns (busy/idle/gap) are reported as '未知'.
With --state-dir (default ~/.claude/openswarm-dispatch), lane, host, backend,
and global pause markers/records are excluded from idle capacity. Unknown status
values are reported separately; only explicit idle snapshots count as idle.

Dispatchable cards are attributed per-backend: unpinned cards (no backend: tag)
are eligible for every backend in inventory; pinned cards are attributed to their
pinned backend. Cross-backend fallbacks (can_take) are not assumed here to keep
per-backend deficit visible for supervisor replenishment.
Backends absent from inventory are treated as external unless they are known
internal executors (pi, codex, agy, copilot, cursor, hermes).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import time

INTERNAL_BACKENDS = ("pi", "codex", "agy", "copilot", "cursor", "hermes")


def gh(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        result = subprocess.run(["gh", *args], text=True, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, "", str(error)
    return result.returncode, result.stdout, result.stderr


def labels(issue: dict) -> set[str]:
    return {row["name"] if isinstance(row, dict) else str(row)
            for row in (issue.get("labels") or [])}


def lanes_from(data: dict) -> list[dict]:
    hosts = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(hosts, dict):
        raise ValueError('--lanes must have shape {"hosts": {host: {"lanes": [...]}}}')
    lanes = []
    for host, config in hosts.items():
        rows = config.get("lanes") if isinstance(config, dict) else None
        if not isinstance(rows, list) or any(not isinstance(lane, dict) or
                not lane.get("name") or not lane.get("backend") for lane in rows):
            raise ValueError(f"invalid lanes list for host {host!r}; each lane needs name and backend")
        lanes.extend(dict(lane, host=host, host_ssh=config.get("ssh")) for lane in rows)
    return lanes


def lane_status_map(data: object | None) -> dict[str, str]:
    if data is None:
        return {}
    mapping: dict[str, str] = {}
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                name = str(row.get("name") or row.get("lane") or "")
                if not name or name.startswith("_"):
                    continue
                if row.get("issue") or row.get("busy") is True or str(row.get("state", "")).lower() == "busy":
                    mapping[name] = "busy"
                elif (row.get("idle") is True or row.get("busy") is False or
                      str(row.get("state", "")).lower() == "idle" or
                      str(row.get("status", "")).lower() == "idle") and not row.get("issue"):
                    mapping[name] = "idle"
                else:
                    mapping[name] = str(row.get("state") or row.get("status") or "?")
    elif isinstance(data, dict):
        lanes = data.get("lanes", data)
        if isinstance(lanes, dict):
            for name, row in lanes.items():
                sname = str(name)
                if sname.startswith("_"):
                    continue
                if isinstance(row, str):
                    s = row.strip()
                    if s.lower().startswith("busy"):
                        mapping[sname] = "busy"
                    elif s.lower() == "idle":
                        mapping[sname] = "idle"
                    else:
                        mapping[sname] = s
                elif isinstance(row, dict):
                    if row.get("issue") or row.get("busy") is True or str(row.get("state", "")).lower() == "busy":
                        mapping[sname] = "busy"
                    elif (row.get("idle") is True or row.get("busy") is False or
                          str(row.get("state", "")).lower() == "idle" or
                          str(row.get("status", "")).lower() == "idle") and not row.get("issue"):
                        mapping[sname] = "idle"
                    else:
                        mapping[sname] = str(row.get("state") or row.get("status") or "?")
        elif isinstance(lanes, list):
            return lane_status_map(lanes)
    return mapping


def paused_lanes(lanes: list[dict], state_dir: Path | None) -> set[str]:
    """Read legacy PAUSE markers and active scoped pause records."""
    if state_dir is None:
        return set()
    scopes = set()
    try:
        for marker in state_dir.iterdir():
            if marker.is_file():
                if marker.name == "PAUSE":
                    scopes.add("all")
                elif marker.name == "PAUSE-MINT":
                    scopes.add("mint")
                elif marker.name.startswith("PAUSE-BACKEND-"):
                    scopes.add("backend:" + marker.name[len("PAUSE-BACKEND-"):])
                elif marker.name.startswith("PAUSE-HOST-"):
                    scopes.add("host:" + marker.name[len("PAUSE-HOST-"):])
                elif marker.name.startswith("PAUSE-"):
                    scopes.add("lane:" + marker.name[len("PAUSE-"):])
        for path in (state_dir / "pauses").glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                expiry = record.get("expires_at")
                if record.get("scope") and (expiry is None or float(expiry) > time.time()):
                    scopes.add(record["scope"])
            except (OSError, ValueError, TypeError):
                continue
    except OSError:
        return set()
    return {str(lane["name"]) for lane in lanes if "all" in scopes or any(
        scope in scopes for scope in ("lane:" + str(lane["name"]),
            "host:" + str(lane.get("host") or ""),
            "host:" + str(lane.get("host_ssh") or ""), "backend:" + str(lane["backend"]))) }


def report(repo: str, inventory: dict, status: object | None, runner=gh,
           state_dir: Path | None = None) -> dict:
    lanes = lanes_from(inventory)
    backends = sorted({str(lane["backend"]) for lane in lanes})
    rc, out, err = runner(["issue", "list", "--repo", repo, "--state", "open",
                           "--label", "lane-ready", "--limit", "100", "--json",
                           "number,title,body,labels"])
    if rc:
        raise RuntimeError(f"gh issue list failed: {err.strip()}")
    issues = json.loads(out)
    mapping = lane_status_map(status) if status is not None else None
    counts = {backend: {"dispatchable": 0, "reasons": {"in-progress": 0,
              "external": 0, "dependency_open": 0}} for backend in backends}
    cache: dict[int, bool] = {}

    def dependency_open(number: int) -> bool:
        if number not in cache:
            code, raw, _ = runner(["issue", "view", str(number), "--repo", repo, "--json", "state"])
            cache[number] = code != 0 or json.loads(raw).get("state", "OPEN").upper() != "CLOSED"
        return cache[number]

    total_dispatchable = 0
    for issue in issues:
        tags = labels(issue)
        pin = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("backend:")), None)
        is_external = ("lane-claimed" in tags or
                       (pin is not None and (pin.startswith("external") or
                        (pin not in backends and pin not in INTERNAL_BACKENDS))))

        reason = None
        if is_external:
            reason = "external"
            consumers = backends
        elif tags & {"in-progress", "lane-pr", "lane-failed"}:
            reason = "in-progress"
            consumers = [pin] if pin in counts else backends
        else:
            body = issue.get("body") or ""
            deps = []
            # Match the dispatcher's first dependency declaration only.
            dependency_line = re.search(r"^\s*(?:依赖|Depends on)\s*[:：](.*)$", body, re.M)
            if dependency_line:
                deps.extend(int(n) for n in re.findall(r"#([1-9]\d*)", dependency_line.group(1)))
            # Dependency numbers outside the first declaration are references, not blockers.
            if any(dependency_open(dep) for dep in deps):
                reason = "dependency_open"
                consumers = [pin] if pin in counts else backends
            else:
                consumers = [pin] if pin in counts else backends

        if reason:
            for backend in consumers:
                counts[backend]["reasons"][reason] += 1
        else:
            total_dispatchable += 1
            for backend in consumers:
                counts[backend]["dispatchable"] += 1

    paused = paused_lanes(lanes, state_dir)
    result = {}
    for backend in backends:
        owned = [lane for lane in lanes if str(lane["backend"]) == backend]
        ready = counts[backend]["dispatchable"]
        if mapping is None:
            busy = "未知"
            idle = "未知"
            gap = "未知"
        else:
            busy = sum(mapping.get(str(lane["name"])) == "busy" for lane in owned)
            idle = sum(mapping.get(str(lane["name"])) == "idle" and
                       not lane.get("drain") and str(lane["name"]) not in paused for lane in owned)
            unknown = sum(mapping.get(str(lane["name"])) not in {"busy", "idle"} and
                          not lane.get("drain") and str(lane["name"]) not in paused for lane in owned)
            gap = max(0, idle - ready)
        if mapping is None:
            unknown = "未知"
        result[backend] = {"dispatchable": ready, "busy": busy, "idle": idle,
                           "unknown": unknown, "gap": gap, **counts[backend]["reasons"]}
    return {"backends": result, "dispatchable": total_dispatchable}


def main(argv: list[str] | None = None, runner=gh) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--lanes", type=Path, required=True)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path("~/.claude/openswarm-dispatch").expanduser(),
                        help="派发器状态目录，用于排除 PAUSE-* 与 pauses/*.json 暂停")
    parser.add_argument("--target", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        inventory = json.loads(args.lanes.read_text(encoding="utf-8"))
        status = json.loads(args.status.read_text(encoding="utf-8")) if args.status else None
        data = report(args.repo, inventory, status, runner, args.state_dir)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        parser.error(str(error))
    if args.as_json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print("BACKEND  READY  BUSY  IDLE  UNKNOWN  GAP  IN_PROGRESS  EXTERNAL  DEPENDENCY_OPEN")
        for backend, row in data["backends"].items():
            print(f"{backend:<8} {row['dispatchable']:>5} {str(row['busy']):>5} {str(row['idle']):>5} "
                  f"{str(row['unknown']):>7} {str(row['gap']):>4} {row['in-progress']:>12} {row['external']:>9} "
                  f"{row['dependency_open']:>16}")
    return 3 if data["dispatchable"] < args.target else 0


if __name__ == "__main__":
    raise SystemExit(main())
