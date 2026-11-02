"""Classify local run interruptions against recorded VM pause intervals."""

import datetime
import json
import math


def _timestamp(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            # Naive timestamps deliberately use this machine's local timezone:
            # the pause log and dispatcher state both record local wall time.
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _pause(record):
    if not isinstance(record, dict):
        return None
    start = _timestamp(record.get("from", record.get("started")))
    end = _timestamp(record.get("to", record.get("resumed")))
    if start is None or end is None or end < start:
        return None
    gap = record.get("gap_s", end - start)
    if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not math.isfinite(gap) or gap < 0:
        return None
    return {**record, "from": record.get("from", record.get("started")),
            "to": record.get("to", record.get("resumed")),
            "gap_s": gap, "start_ts": start, "end_ts": end}


def load_pauses(path):
    """Read valid JSONL intervals, skipping malformed lines and missing logs."""
    pauses = []
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    pause = _pause(json.loads(line))
                except (ValueError, TypeError):
                    continue
                if pause is not None:
                    pauses.append(pause)
    except OSError:
        pass
    return pauses


def overlapping(start_ts, end_ts, pauses, min_gap_s=120):
    """Return the first sufficiently long pause intersecting the closed run window."""
    start, end = _timestamp(start_ts), _timestamp(end_ts)
    if start is None or end is None or end < start:
        return None
    for record in pauses:
        pause = _pause(record)
        if pause is not None and pause["gap_s"] >= min_gap_s:
            if pause["start_ts"] <= end and pause["end_ts"] >= start:
                return pause
    return None


def run_window(info):
    """Use actual run timestamps, inferring a missing endpoint from its duration."""
    def first(keys):
        return next((stamp for key in keys if (stamp := _timestamp(info.get(key))) is not None), None)
    start = first(("started", "start_ts", "started_at", "start"))
    end = first(("finished", "end_ts", "finished_at", "end", "ts"))
    seconds = info.get("seconds")
    valid_duration = (isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                      and math.isfinite(seconds) and seconds >= 0)
    if valid_duration:
        if start is None and end is not None:
            start = end - seconds
        elif end is None and start is not None:
            end = start + seconds
    if start is None or end is None or end < start:
        return None
    return start, end


def classify(info, lane_cfg, pauses, local_hosts, min_gap_s=120):
    """Return vm_paused only for local exec_error/stalled runs with a known overlap."""
    original = info.get("error_class")
    if lane_cfg.get("host") not in local_hosts or original not in {"exec_error", "stalled"}:
        return original
    window = run_window(info)
    if window and overlapping(*window, pauses, min_gap_s=min_gap_s) is not None:
        return "vm_paused"
    return original
