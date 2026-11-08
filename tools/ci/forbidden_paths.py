#!/usr/bin/env python3
"""Reject forbidden paths in a change or in a committed tree."""

import argparse
from collections import Counter
import re
import subprocess
import sys
from pathlib import Path


LEGACY_FILE = Path(__file__).with_name("forbidden_paths.legacy")
LEGACY_PATH = "tools/ci/forbidden_paths.legacy"


def rule_for(path):
    parts = path.split("/")
    if ".tmp" in parts[:-1]:
        return ".tmp/"
    if ".lane-review" in parts[:-1]:
        return ".lane-review/"
    if path.endswith(".so"):
        return "*.so"
    if re.search(r"\.bak($|[-._0-9])", parts[-1]):
        return ".bak($|[-._0-9])"
    if len(parts) > 1 and parts[0] == "openswarm":
        return "openswarm/"
    return None


def git(*args):
    return subprocess.check_output(["git", *args])


def legacy_lines(revision):
    if not git("ls-tree", "--name-only", "-z", revision, "--", LEGACY_PATH):
        return []
    return git("show", f"{revision}:{LEGACY_PATH}").decode("utf-8", errors="surrogateescape").splitlines()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--tree", action="store_true")
    args = parser.parse_args()
    if not args.tree and not args.base:
        parser.error("--base is required unless --tree is used")

    if args.tree:
        entries = [("A", path) for path in git("ls-tree", "-r", "--name-only", "-z", args.head).split(b"\0") if path]
    else:
        fields = git("diff", "--name-status", "--no-renames", "-z", f"{args.base}...{args.head}").split(b"\0")
        entries = []
        fields = iter(fields)
        for status in fields:
            if not status:
                break
            path = next(fields)
            if status.startswith((b"C", b"R")):
                path = next(fields)
            if status != b"D":
                entries.append((status.decode(), path))

    forbidden = False
    legacy = set()
    if args.tree:
        if LEGACY_FILE.exists():
            legacy = {line.strip() for line in LEGACY_FILE.read_text(errors="surrogateescape").splitlines()
                      if line.strip() and not line.strip().startswith("#")}
        tree_paths = {path.decode("utf-8", errors="surrogateescape") for _, path in entries}
        openswarm_paths = {path for path in tree_paths if path.startswith("openswarm/")}
        for path in sorted(legacy - tree_paths):
            print(f"STALE-LEGACY {path}（已删除，请从 forbidden_paths.legacy 移除该行）")
            forbidden = True
        if legacy and not openswarm_paths:
            print("LEGACY-NOT-EMPTY")
            forbidden = True
    elif any(path.decode("utf-8", errors="surrogateescape") == LEGACY_PATH
             for _, path in entries):
        def entries_of(lines):
            return Counter(l.strip() for l in lines if l.strip() and not l.strip().startswith("#"))
        base_lines = legacy_lines(args.base)
        added = entries_of(legacy_lines(args.head)) - entries_of(base_lines)
        if not git("ls-tree", "--name-only", "-z", args.base, "--", LEGACY_PATH):
            # Bootstrap: the list may only name files that already exist on the base.
            base_tree = {path.decode("utf-8", errors="surrogateescape") for path in
                         git("ls-tree", "-r", "--name-only", "-z", args.base, "--", "openswarm").split(b"\0") if path}
            added = Counter({l: n for l, n in added.items() if l not in base_tree})
        for line in sorted(added.elements()):
            print(f"LEGACY-GROWN {line}")
            forbidden = True

    for status, raw_path in entries:
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if not args.tree and path.startswith("openswarm/"):
            print(f"FORBIDDEN {status} {path} (openswarm/)")
            forbidden = True
            continue
        rule = rule_for(path)
        if rule == "openswarm/" and args.tree and path in legacy:
            continue
        if rule:
            print(f"FORBIDDEN {status} {path} ({rule})")
            forbidden = True
    if forbidden:
        return 1
    print(f"forbidden-paths: OK ({len(entries)} changed paths checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
