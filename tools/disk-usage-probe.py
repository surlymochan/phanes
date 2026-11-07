#!/usr/bin/env python3
"""Report filesystem use and the largest directories by allocated blocks."""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


def directory_bytes(path: Path) -> int:
    """Allocated bytes (st_blocks), so sparse files do not dominate rankings."""
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda _error: None):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for name in files:
            item = Path(root) / name
            try:
                total += item.lstat().st_blocks * 512
            except OSError:
                continue
    return total


def top_directories(root: Path, limit: int = 10) -> list[tuple[int, Path]]:
    entries = [(directory_bytes(path), path) for path in root.iterdir()]
    return sorted(entries, reverse=True)[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, default=Path("/"), nargs="?")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    usage = shutil.disk_usage(args.path)
    print(f"path={args.path} used_pct={(usage.used * 100 // usage.total)} used_bytes={usage.used} free_bytes={usage.free}")
    for size, directory in top_directories(args.path, args.top):
        print(f"{size:12d} {directory}")


if __name__ == "__main__":
    main()
