#!/usr/bin/env python3
"""Disk usage probe: the filesystem use and the largest directories (Refs #1666).

The probe answers two questions about one filesystem:

* how full is it (`used_pct`, `used_bytes`, `free_bytes`), and
* which directories really occupy the disk (the `top` rows).

The ranking uses the *allocated* blocks (`st_blocks`), not the apparent size of
the files: a sparse or a preallocated file looks huge but occupies almost
nothing, so ranking by the apparent size points the cleanup at the wrong
directories.  The output is reproducible: the same tree always yields the same
rows in the same order (no timestamps, no random order).

Usage:

    python3 tools/disk-usage-probe.py --path / --top 10
    python3 tools/disk-usage-probe.py /var --top 5

The guard (`tools/disk-watermark-guard.py`) loads this module from its own
directory: the probe is a hard dependency of the guard, not a copy of it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

BLOCK_SIZE = 512      # st_blocks is counted in 512 byte units
DF_BLOCK = 1024       # df -Pk reports 1K blocks
DEFAULT_TOP = 10
DEFAULT_MAX_DEPTH = 3


def parse_df(text, path=None):
    """Parse the output of ``df -Pk`` into the usage numbers of one filesystem.

    The wanted mount point wins, otherwise the root filesystem, otherwise the
    first data row.  A missing percentage is computed from the block counts.
    """
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("Filesystem"):
            continue
        fields = line.split()
        if len(fields) < 5:
            continue
        try:
            total, used, available = int(fields[1]), int(fields[2]), int(fields[3])
        except ValueError:
            continue
        capacity = fields[4]
        mount = " ".join(fields[5:]) if len(fields) > 5 else ""
        percent = None
        if capacity.endswith("%"):
            try:
                percent = int(capacity[:-1])
            except ValueError:
                percent = None
        rows.append({"mount": mount, "total": total, "used": used,
                     "available": available, "percent": percent})
    if not rows:
        raise ValueError("no filesystem row in the df output")
    wanted = None
    if path:
        wanted = next((row for row in rows if row["mount"] == path), None)
    if wanted is None:
        wanted = next((row for row in rows if row["mount"] == "/"), None)
    if wanted is None:
        wanted = rows[0]
    percent = wanted["percent"]
    if percent is None:
        denominator = wanted["used"] + wanted["available"]
        percent = int(wanted["used"] * 100 / denominator) if denominator else 0
    return {"mount": wanted["mount"],
            "total_bytes": wanted["total"] * DF_BLOCK,
            "used_bytes": wanted["used"] * DF_BLOCK,
            "free_bytes": wanted["available"] * DF_BLOCK,
            "used_pct": percent}


def allocated_bytes(path):
    """The bytes really allocated for a path, not its apparent size."""
    try:
        info = os.lstat(path)
    except OSError:
        return 0
    blocks = getattr(info, "st_blocks", None)
    if blocks is None:                     # a filesystem without st_blocks
        return info.st_size
    return blocks * BLOCK_SIZE


def walk_usage(root, max_depth=DEFAULT_MAX_DEPTH):
    """The allocated bytes of a directory tree (the directory itself included)."""
    total = allocated_bytes(root)
    stack = [(root, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    total += allocated_bytes(entry.path)
                    stack.append((entry.path, depth + 1))
                else:
                    total += allocated_bytes(entry.path)
            except OSError:
                continue
    return total


def directory_usage(root, max_depth=DEFAULT_MAX_DEPTH):
    """The allocated bytes of every top level directory below ``root``."""
    usage = {}
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return usage
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        usage[entry.path] = walk_usage(entry.path, max_depth)
    return usage


def probe(root, top=DEFAULT_TOP, max_depth=DEFAULT_MAX_DEPTH):
    """The probe result of one filesystem: the usage numbers and the top rows."""
    usage = shutil.disk_usage(root)
    used = usage.total - usage.free
    rows = sorted(directory_usage(root, max_depth).items(),
                  key=lambda row: (-row[1], row[0]))
    return {"path": str(root),
            "used_pct": int(used * 100 / usage.total) if usage.total else 0,
            "used_bytes": used,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "top": rows[:top] if top and top > 0 else rows}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report the filesystem use and the largest directories.")
    parser.add_argument("root", nargs="?", default=None,
                        help="the filesystem or directory to measure")
    parser.add_argument("--path", dest="path_option", default=None,
                        help="the filesystem or directory to measure")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP,
                        help="how many directories to list")
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH,
                        help="how deep the directory walk goes")
    args = parser.parse_args(argv)
    root = args.path_option or args.root or "/"
    try:
        result = probe(root, args.top, args.max_depth)
    except OSError as error:
        print(f"the disk usage probe failed on {root}: {error}", file=sys.stderr)
        return 1
    print(f"used_pct={result['used_pct']}")
    print(f"used_bytes={result['used_bytes']}")
    print(f"free_bytes={result['free_bytes']}")
    print(f"total_bytes={result['total_bytes']}")
    print(f"path={result['path']}")
    print(f"top={len(result['top'])}")
    for path, size in result["top"]:
        print(f"  {size}  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
