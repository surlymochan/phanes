#!/usr/bin/env python3
"""Cleanup planning and the built-in cleanup hook.

The planner stays read-only: it selects eligible paths under explicit allow
roots and excludes every busy overlap.  ``execute_cleanup`` is the only
function here that removes anything, and it re-checks the allowlist and the
busy paths immediately before every removal.
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
import json


# Age thresholds are seconds; the package-cache threshold is an entry count.
DEFAULT_RULES = {"run_dir": 3 * 86400, "worktree_node_modules": 12 * 3600,
                 "pkg_cache": 20000, "tmp_dir": 48 * 3600,
                 "completed_run_dir": 3 * 86400, "closed_worktree": 12 * 3600}

# The recorded state of a finished run directory.
TERMINAL_RUN_STATES = ("finished", "failed", "unroutable", "dry-run", "dead",
                        "done", "partial", "cancelled")

# A worktree that carries this marker is closed (its lane is merged or shut down).
CLOSED_MARKER = ".openswarm-closed"

# The name of the worktree directory inside a lane directory (DATA/lanes/<lane>/wt).
WORKTREE_NAME = "wt"


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


def run_state(path) -> str | None:
    """The recorded state of a run directory, or None when it has none."""
    for name in ("status.json", "result.json"):
        try:
            with open(Path(path) / name, encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        state = data.get("state") or data.get("status")
        result = data.get("result")
        if not state and isinstance(result, dict):
            state = result.get("status")
        if isinstance(state, str) and state.strip():
            return state.strip()
    return None


def run_completed(path) -> bool:
    """True when the run directory recorded a terminal state."""
    return run_state(path) in TERMINAL_RUN_STATES


def worktree_of(directory) -> Path:
    """The worktree inside a container directory.

    A lane directory holds its worktree in ``wt`` (``DATA/lanes/<lane>/wt``);
    a plain worktree container holds the worktree itself.
    """
    path = Path(directory)
    inner = path / WORKTREE_NAME
    return inner if inner.is_dir() else path


def _git_dir(path) -> Path | None:
    """The git directory of a worktree (its .git file points at the gitdir)."""
    dotgit = Path(path) / ".git"
    try:
        if dotgit.is_file():
            text = dotgit.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            target = Path(text.split(":", 1)[1].strip())
            return target if target.is_absolute() else (Path(path) / target)
        if dotgit.is_dir():
            return dotgit
    except OSError:
        return None
    return None


def worktree_branch(path) -> str | None:
    """The branch a worktree has checked out, read from its gitdir HEAD."""
    gitdir = _git_dir(path)
    if gitdir is None:
        return None
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return None                     # a detached HEAD has no branch to merge
    ref = head.split(":", 1)[1].strip()
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None


def branch_merged(path, repo, base="main") -> bool:
    """True when the worktree's branch is already merged into ``base``."""
    branch = worktree_branch(path)
    if not branch or not repo:
        return False
    try:
        result = subprocess.run(["git", "-C", os.path.expanduser(os.fspath(repo)),
                                 "merge-base", "--is-ancestor", branch, base],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def prune_worktrees(repo) -> None:
    """Drop the metadata of the worktrees that were removed from ``repo``."""
    try:
        subprocess.run(["git", "-C", os.path.expanduser(os.fspath(repo)), "worktree", "prune"],
                       capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def worktree_closed(path, lanes=None, repo=None, base="main") -> bool:
    """True when the worktree is closed and may be cleaned.

    A worktree is closed when it carries the ``.openswarm-closed`` marker, when
    its lane is no longer configured (the lane was shut down), or when its branch
    is already merged into ``base`` (the work was delivered).
    """
    path = Path(path)
    if (path / CLOSED_MARKER).exists() or (path.parent / CLOSED_MARKER).exists():
        return True
    if lanes is not None and path.name == WORKTREE_NAME:
        if path.parent.name not in lanes:
            return True                 # the lane is gone: its worktree is closed
    return branch_merged(path, repo, base)


def _entries(path) -> int:
    with os.scandir(path) as entries:
        return sum(1 for _ in entries)


def allow_roots(roots) -> list:
    """Every container directory of the cleanup hook, expanded."""
    out = []
    for paths in (roots or {}).values():
        for path in paths or []:
            expanded = os.path.expanduser(os.fspath(path))
            if expanded not in out:
                out.append(expanded)
    return out


def scan_hook(roots, lanes=None, repo=None, base="main") -> list[dict]:
    """Scan the configured roots of the built-in cleanup hook.

    ``roots`` maps a kind to its container directories:
      * ``completed_run_dir``: every child is a run directory; only terminal runs qualify.
      * ``closed_worktree``: every child holds a worktree (a lane directory holds
        it in ``wt``); only closed worktrees qualify.
      * ``worktree_node_modules``: every child holds a worktree; its node_modules is the candidate.
      * ``pkg_cache``: the root is the cache directory itself.
      * ``tmp_dir``: every child is a temporary directory.

    ``lanes`` is the set of the lane names that are still configured; a worktree
    of a lane that is gone is closed. ``repo`` and ``base`` locate the repository
    whose ``base`` branch decides whether a worktree's branch is merged.
    """
    candidates = []
    for kind, paths in (roots or {}).items():
        if kind not in DEFAULT_RULES:
            raise ValueError("unknown cleanup kind: " + str(kind))
        for root in paths or []:
            root = _path(root)
            if kind == "pkg_cache":
                candidates.append({"path": str(root), "kind": kind,
                                   "mtime": root.stat().st_mtime, "entries": _entries(root)})
                continue
            with os.scandir(root) as children:
                for child in children:
                    if not stat.S_ISDIR(os.stat(child.path, follow_symlinks=False).st_mode):
                        continue
                    if kind == "completed_run_dir":
                        if not run_completed(child.path):
                            continue
                        path = child.path
                    elif kind in ("closed_worktree", "worktree_node_modules"):
                        # A lane directory holds its worktree in ``wt``.
                        path = str(worktree_of(child.path))
                    else:
                        path = child.path
                    if kind == "closed_worktree" and not worktree_closed(path, lanes=lanes,
                                                                        repo=repo, base=base):
                        continue
                    if kind == "worktree_node_modules":
                        path = os.path.join(path, "node_modules")
                        if not os.path.isdir(path):
                            continue
                    info = os.stat(path, follow_symlinks=False)
                    candidates.append({"path": path, "kind": kind,
                                       "mtime": info.st_mtime, "entries": _entries(path)})
    return sorted(candidates, key=lambda item: item["path"])


def execute_cleanup(plan, allow_root_list, busy_paths, repo=None) -> list:
    """Remove the planned paths that are still allowed and still unprotected.

    The allowlist and the busy paths are re-checked immediately before every
    removal, so a path that became busy in the meantime is kept.  When ``repo``
    is given, the removed git worktrees are pruned from its worktree list.
    """
    roots = [(_path(root), _path(root).resolve()) for root in allow_root_list]
    busy = [(_path(path), _path(path).resolve()) for path in busy_paths]
    removed = []
    for item in plan:
        path = _path(item["path"])
        try:
            real = path.resolve()
        except OSError:
            continue
        if not any(_inside(path, root) and _inside(real, resolved) for root, resolved in roots):
            continue
        if any(_overlap(left, right) for left in (path, real)
               for pair in busy for right in pair):
            continue
        if path.is_symlink() or not path.is_dir():
            continue
        shutil.rmtree(path)
        removed.append(str(path))
    if removed and repo:
        prune_worktrees(repo)
    return removed


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
