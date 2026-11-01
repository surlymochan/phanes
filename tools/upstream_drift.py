#!/usr/bin/env python3
"""Report component changes relative to the recorded upstream snapshots."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "upstream-sync.json"


def run(args: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "command failed: " + " ".join(args))
    return result.stdout


def run_bytes(args: list[str]) -> bytes:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def git_output(args: list[str]) -> str | None:
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        return None
    return result.stdout.strip()


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def blob_id(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def snapshot(upstream: dict[str, str], base_sha: str, work_dir: Path) -> dict[str, str]:
    # Fetch just the pinned object, keeping all scratch data inside TMPDIR.
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", str(ROOT))) as scratch:
        repo = Path(scratch)
        run(["git", "init", "--bare", str(repo)])
        run(["git", "--git-dir", str(repo), "fetch", "--depth=1", upstream["upstream_repo"], base_sha])
        tree = run_bytes(["git", "--git-dir", str(repo), "ls-tree", "-r", "-z", "FETCH_HEAD"])
        result: dict[str, str] = {}
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            if kind == b"blob":
                result[path.decode()] = blob_id(run_bytes(["git", "--git-dir", str(repo), "cat-file", "blob", oid.decode()]))
        return result


def owns_repository(root: Path) -> bool:
    """True when ``root`` is the top level of the git repository holding it."""
    top = git_output(["git", "-C", str(root), "rev-parse", "--show-toplevel"])
    return top is not None and Path(top).resolve() == root.resolve()


def tracked_paths(directory: Path, root: Path) -> list[str] | None:
    """Component relative paths git tracks; None when git does not own ``root``.

    Only tracked files and untracked-but-not-ignored files are listed, so files
    phanes ignores on purpose (upstream build artefacts) never show up as drift.
    """
    try:
        relative = directory.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    if not owns_repository(root):
        return None
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard", "--", relative or "."],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if listing.returncode:
        return None
    prefix = f"{relative}/" if relative else ""
    paths = []
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        text = entry.decode()
        paths.append(text[len(prefix):] if prefix and text.startswith(prefix) else text)
    return paths


def walked_paths(directory: Path) -> list[str]:
    """Fallback listing for checkouts that are not git repositories."""
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in list(dirnames):
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                dirnames.remove(name)
                paths.append(candidate.relative_to(directory).as_posix())
        for name in filenames:
            paths.append((Path(dirpath) / name).relative_to(directory).as_posix())
    return paths


def local_files(directory: Path, root: Path | None = None) -> dict[str, str]:
    """Snapshot the working tree the way git stores it (symlinks included)."""
    paths = tracked_paths(directory, root or directory)
    if paths is None:
        paths = walked_paths(directory)
    result: dict[str, str] = {}
    for relative in paths:
        path = directory / relative
        if path.is_symlink():
            # git stores the link target as the blob content.
            data = os.readlink(path).encode()
        elif path.is_file():
            data = path.read_bytes()
        else:
            continue
        result[relative] = blob_id(data)
    return result


def ignored_paths(paths: list[str], root: Path) -> set[str]:
    """Paths git ignores in ``root``; they are absent on purpose, not deleted."""
    if not paths or not owns_repository(root):
        return set()
    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-z", "--stdin"],
        input=b"\0".join(path.encode() for path in paths) + b"\0",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode not in (0, 1):
        return set()
    return {entry.decode() for entry in result.stdout.split(b"\0") if entry}


def component_ignored(name: str, paths: list[str], root: Path) -> set[str]:
    """Component relative paths phanes ignores on purpose."""
    prefix = f"{name}/"
    ignored = ignored_paths([f"{prefix}{path}" for path in paths], root)
    return {path[len(prefix):] for path in ignored if path.startswith(prefix)}


def classify(upstream: dict[str, str], local: dict[str, str], patterns: list[str],
             ignored: frozenset[str] = frozenset()) -> dict[str, list[str]]:
    changed = {p for p in upstream.keys() | local.keys() if upstream.get(p) != local.get(p)}
    missing = sorted(p for p in changed if p in upstream and p not in local and p not in ignored)
    phanes = sorted(p for p in changed if p in local and matches(p, patterns))
    generic = sorted(p for p in changed if p not in ignored and p not in missing and p not in phanes)
    return {"phanes_only": phanes, "generic": generic, "missing": missing}


def pr_changed_paths() -> set[str]:
    base_ref = os.environ.get("GITHUB_BASE_REF")
    if not base_ref:
        return set()
    run(["git", "fetch", "--no-tags", "origin", base_ref], cwd=ROOT)
    return set(run(["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"], cwd=ROOT).splitlines())


def marks_upstream(title: str) -> bool:
    """A title registers an upstream sync when it mentions ``upstream``."""
    return "upstream" in title.lower()


def unmarked_paths(reports: dict[str, dict[str, list[str]]], changed: set[str], title: str) -> list[str]:
    paths = [f"{name}/{path}" for name, groups in reports.items()
             for path in groups["generic"] if f"{name}/{path}" in changed]
    return paths if paths and not marks_upstream(title) else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--fail-on-unmarked", action="store_true")
    parser.add_argument("--title", default=os.environ.get("PR_TITLE", ""))
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    reports: dict[str, dict[str, list[str]]] = {}
    try:
        for name, component in config["components"].items():
            base = snapshot(component, component["base_sha"], args.root / name)
            current = local_files(args.root / name, args.root)
            ignored = component_ignored(name, list(base), args.root)
            reports[name] = classify(base, current, component.get("phanes_only", []), ignored)
    except (RuntimeError, OSError) as exc:
        print(f"upstream drift: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(reports, indent=2, sort_keys=True))
    else:
        for name, groups in reports.items():
            print(f"{name}:")
            for category, paths in groups.items():
                print(f"  {category}: {len(paths)}")
                for path in paths:
                    print(f"    {path}")

    if args.fail_on_unmarked:
        changed = pr_changed_paths() if args.root.resolve() == ROOT.resolve() else set()
        unmarked = unmarked_paths(reports, changed, args.title)
        if unmarked:
            print("generic upstream paths require a PR title mentioning 'upstream' "
                  "(for example 'upstream:' or 'upstream(openbot):')", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
