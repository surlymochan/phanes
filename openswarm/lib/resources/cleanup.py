#!/usr/bin/env python3
"""Read-only cleanup planning. No plan produced here is executed."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import stat
import time
import json


# Age thresholds are seconds; the package-cache threshold is an entry count.
DEFAULT_RULES = {"run_dir": 3 * 86400, "worktree_node_modules": 12 * 3600,
                 "pkg_cache": 20000, "tmp_dir": 48 * 3600}


def _path(value) -> Path:
    if not os.fspath(value):
        raise ValueError("paths must not be empty")
    return Path(os.path.abspath(os.path.expanduser(value)))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _overlap(left: Path, right: Path) -> bool:
    return _inside(left, right) or _inside(right, left)


def _number(value) -> float:
    if isinstance(value, bool):
        raise ValueError("thresholds and timestamps must be finite numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("thresholds and timestamps must be finite numbers")
    return number


def plan_cleanup(candidates, busy_paths, now, rules) -> list[dict]:
    """Select eligible paths under explicit allow roots, excluding every busy overlap.

    rules overrides DEFAULT_RULES by kind and must provide allow_roots. Both
    lexical and symlink-resolved containment must hold. Missing allow roots deny
    every candidate; uncertain path resolution raises instead of producing a plan.
    """
    rules = rules or {}
    limits = {kind: _number(rules.get(kind, value)) for kind, value in DEFAULT_RULES.items()}
    if any(value < 0 for value in limits.values()):
        raise ValueError("cleanup thresholds must be nonnegative")
    now = _number(now)
    roots = [(_path(root), _path(root).resolve()) for root in rules.get("allow_roots", [])]
    busy = [(_path(path), _path(path).resolve()) for path in busy_paths]
    plan, seen = [], set()
    for candidate in candidates:
        kind = candidate["kind"]
        if kind not in limits:
            continue
        path = _path(candidate["path"])
        real = path.resolve()
        if not any(_inside(path, root) and _inside(real, resolved) for root, resolved in roots):
            continue
        if any(_overlap(left, right) for left in (path, real)
               for pair in busy for right in pair):
            continue
        if kind == "pkg_cache":
            entries = _number(candidate["entries"])
            eligible = entries > limits[kind]
            reason = f"entry count {entries:g} exceeds {limits[kind]:g}"
        else:
            age = now - _number(candidate["mtime"])
            eligible = age > limits[kind]
            reason = f"age {age:g}s exceeds {limits[kind]:g}s"
        key = str(path)
        if eligible and key not in seen:
            seen.add(key)
            plan.append({"path": key, "kind": kind, "reason": reason})
    return sorted(plan, key=lambda item: item["path"])


def scan(roots, kinds) -> list[dict]:
    """Scan immediate child directories of each container root, without following links.

    kinds is one kind for all roots, or a sequence aligned with roots. For
    worktree_node_modules, each root is a worktree and only its node_modules
    child is considered. Entry counts include one level of files/directories.
    Inaccessible or disappearing paths raise: an incomplete scan is not a plan.
    """
    roots = list(roots)
    kinds = [kinds] if isinstance(kinds, str) else list(kinds)
    if len(kinds) == 1:
        kinds *= len(roots)
    if len(roots) != len(kinds) or any(kind not in DEFAULT_RULES for kind in kinds):
        raise ValueError("kinds must contain one known kind, or one per root")
    candidates = []
    for root, kind in zip(roots, kinds):
        root = _path(root)
        with os.scandir(root) as children:
            for child in children:
                if kind == "worktree_node_modules" and child.name != "node_modules":
                    continue
                info = os.stat(child.path, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    continue
                with os.scandir(child.path) as entries:
                    count = sum(1 for _ in entries)
                candidates.append({"path": child.path, "kind": kind,
                                   "mtime": info.st_mtime, "entries": count})
    return sorted(candidates, key=lambda item: item["path"])


def _csv(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", required=True, help="comma-separated container directories")
    parser.add_argument("--busy", required=True, help="comma-separated protected paths; empty is allowed")
    parser.add_argument("--kinds", default="run_dir", help="one kind for all roots, or one per root")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    roots = _csv(args.roots)
    if not roots:
        parser.error("at least one root is required")
    try:
        plan = plan_cleanup(scan(roots, _csv(args.kinds)), _csv(args.busy), time.time(),
                            {"allow_roots": roots})
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    if args.as_json:
        print(json.dumps(plan, ensure_ascii=False))
    else:
        print("DRY RUN: cleanup plan only")
        for item in plan:
            print(f"{item['kind']}  {item['path']}  {item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
