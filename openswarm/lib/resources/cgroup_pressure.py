#!/usr/bin/env python3
"""Read and assess cgroup v2 memory pressure for a systemd unit."""

import argparse
import json
from pathlib import Path


def _read_number(path):
    try:
        value = path.read_text(encoding="utf-8").strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


def _read_events(path, keys):
    result = {key: None for key in keys}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in result:
                try:
                    result[parts[0]] = int(parts[1])
                except ValueError:
                    pass
    except OSError:
        pass
    return result


def read_cgroup(path):
    """Read the available memory controller values from a cgroup directory."""
    path = Path(path)
    events = _read_events(path / "memory.events", ("high", "max", "oom", "oom_kill"))
    stat = _read_events(path / "memory.stat", ("anon", "shmem", "file"))
    return {
        "memory_current": _read_number(path / "memory.current"),
        "memory_high": _read_number(path / "memory.high"),
        "memory_max": _read_number(path / "memory.max"),
        "events": events,
        "stat": stat,
    }


def unit_cgroup(unit, root="/sys/fs/cgroup"):
    """Return the cgroup v2 path for a system.slice unit."""
    return Path(root) / "system.slice" / unit


def assess(sample, prev=None, now=None, thresholds=None):
    """Classify a sample; ``now`` is accepted for stable caller interfaces."""
    del now
    limits = {"ratio": 0.95, "high_delta": 1000, "shmem_ratio": 0.5}
    if thresholds:
        limits.update(thresholds)
    high = sample.get("memory_high")
    current = sample.get("memory_current")
    events = sample.get("events") or {}
    previous_events = (prev or {}).get("events") or {}
    reasons = []

    if prev is not None and events.get("oom_kill") is not None and previous_events.get("oom_kill") is not None:
        if events["oom_kill"] > previous_events["oom_kill"]:
            reasons.append("oom_kill")
    if high is not None and current is not None and high > 0:
        ratio = current / high
        high_delta = None
        if prev is not None and events.get("high") is not None and previous_events.get("high") is not None:
            high_delta = events["high"] - previous_events["high"]
        if ratio >= limits["ratio"] and high_delta is not None and high_delta >= limits["high_delta"]:
            reasons.append("memory_high_throttling")
            shmem = (sample.get("stat") or {}).get("shmem")
            if shmem is not None and shmem / high >= limits["shmem_ratio"]:
                reasons.append("tmpfs_shmem")

    if "oom_kill" in reasons:
        state = "oom"
    elif "memory_high_throttling" in reasons:
        state = "throttled"
    else:
        state = "ok"
    return {"state": state, "reasons": reasons, "advice": "restart_when_idle" if state != "ok" else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unit")
    parser.add_argument("--root", default="/sys/fs/cgroup")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    sample = read_cgroup(unit_cgroup(args.unit, args.root))
    result = assess(sample)
    result["sample"] = sample
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("{}: {}{}".format(args.unit, result["state"], ": " + ", ".join(result["reasons"]) if result["reasons"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
