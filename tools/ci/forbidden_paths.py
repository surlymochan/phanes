#!/usr/bin/env python3
"""Reject forbidden paths in a change or in a committed tree."""

import argparse
import re
import subprocess
import sys


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
    for status, raw_path in entries:
        path = raw_path.decode("utf-8", errors="surrogateescape")
        rule = rule_for(path)
        if rule:
            print(f"FORBIDDEN {status} {path} ({rule})")
            forbidden = True
    if forbidden:
        return 1
    print(f"forbidden-paths: OK ({len(entries)} changed paths checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
