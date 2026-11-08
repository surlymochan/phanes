#!/usr/bin/env python3
"""Reject forbidden paths in a change or in a committed tree."""

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path


ALLOW_FILE = Path(__file__).with_name("forbidden_paths.allow")


def rule_for(path, tree=False):
    parts = path.split("/")
    if ".tmp" in parts[:-1]:
        return ".tmp/"
    if ".lane-review" in parts[:-1]:
        return ".lane-review/"
    if path.endswith(".so"):
        return "*.so"
    if ".bak" in parts[-1]:
        return "*.bak*"
    if not tree and len(parts) > 1 and parts[0] == "openswarm":
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

    patterns = []
    if ALLOW_FILE.exists():
        patterns = [line.strip() for line in ALLOW_FILE.read_text().splitlines()
                    if line.strip() and not line.strip().startswith("#")]

    if args.tree:
        entries = [("A", path) for path in git("ls-tree", "-r", "--name-only", "-z", args.head).split(b"\0") if path]
    else:
        fields = git("diff", "--name-status", "--no-renames", "-z", f"{args.base}...{args.head}").split(b"\0")
        entries = list(zip(fields[0::2], fields[1::2]))
        entries = [(status.decode(), path) for status, path in entries if status in (b"A", b"M")]

    forbidden = False
    for status, raw_path in entries:
        path = raw_path.decode("utf-8", errors="surrogateescape")
        rule = rule_for(path, tree=args.tree)
        if rule and not any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns):
            print(f"FORBIDDEN {status} {path} ({rule})")
            forbidden = True
    if forbidden:
        return 1
    print(f"forbidden-paths: OK ({len(entries)} changed paths checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
