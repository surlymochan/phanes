#!/usr/bin/env python3
"""Read block-space and inode usage locally or with a standalone remote snippet."""

import argparse
import inspect
import json
import os


def fs_usage(path, statvfs=os.statvfs) -> dict:
    """Report capacity available to the caller, including reserved-block pressure.

    Percentages use available blocks/inodes rather than total free counts. A
    filesystem that reports no inode capacity has all inode fields set to None.
    """
    path = os.fsdecode(os.fspath(path))
    sample = statvfs(path)
    has_inodes = sample.f_files > 0
    return {
        "path": path,
        "disk_free_gb": sample.f_bavail * sample.f_frsize / (1024 ** 3),
        "disk_used_pct": ((sample.f_blocks - sample.f_bavail) / sample.f_blocks * 100
                          if sample.f_blocks > 0 else None),
        "inode_total": sample.f_files if has_inodes else None,
        "inode_free": sample.f_favail if has_inodes else None,
        "inode_used_pct": ((sample.f_files - sample.f_favail) / sample.f_files * 100
                           if has_inodes else None),
    }


def classify(usage, pause_pct=95, soft_pct=90) -> str:
    """Return the worst reported capacity state, with inclusive watermarks."""
    if not 0 <= soft_pct <= pause_pct <= 100:
        raise ValueError("thresholds must satisfy 0 <= soft_pct <= pause_pct <= 100")
    values = [usage.get(key) for key in ("disk_used_pct", "inode_used_pct")
              if usage.get(key) is not None]
    worst = max(values, default=0)
    if worst >= pause_pct:
        return "hard"
    if worst >= soft_pct:
        return "soft"
    return "ok"


def remote_snippet(paths) -> str:
    """Return one physical line usable by python3 -c on a host without this repo.

    Only the trusted fs_usage function source is executed; filesystem paths are
    separately encoded JSON data. Reusing its source keeps both probes identical.
    The emitted code imports only os and json, with no local package dependency.
    """
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    encoded = json.dumps([os.fsdecode(os.fspath(path)) for path in paths])
    source = inspect.getsource(fs_usage)
    return (f"import os,json;exec({source!r});"
            f"print(json.dumps([fs_usage(p) for p in json.loads({encoded!r})]))")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["/"], help="filesystem paths; defaults to /")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        samples = [fs_usage(path) for path in args.paths]
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.as_json:
        print(json.dumps(samples, ensure_ascii=False))
    else:
        print("PATH  DISK_FREE_GB  DISK_USED_PCT  INODE_TOTAL  INODE_FREE  INODE_USED_PCT  STATE")
        for sample in samples:
            def display(key):
                value = sample[key]
                return "-" if value is None else f"{value:.2f}" if isinstance(value, float) else str(value)
            cells = [sample["path"], *(display(key) for key in (
                "disk_free_gb", "disk_used_pct", "inode_total", "inode_free", "inode_used_pct")),
                classify(sample)]
            print("  ".join(cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
