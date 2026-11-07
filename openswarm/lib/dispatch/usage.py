"""Per-run token usage and the burn windows the fleet status shows (Refs #1690).

The control plane only saw busy/idle lanes and run counts, so nobody could tell
which lane burned the tokens of a night.  This module is the one place that
knows what a usage reading means:

* ``normalize`` folds the backend shapes (pi, cursor, codex, claude, agy) into
  one canonical record: ``input``, ``output``, ``cache_read``, ``cache_write``.
* a run that reported no usable count is **not collected**: it is reported as
  missing (``未采集``), never as a zero burn.  A zero burn reads like "this lane
  did nothing", a missing reading reads like "we do not know"; conflating the
  two is how a quiet lane looks like a cheap lane.
* the windows (15 minutes, one hour) are always recomputed from the raw
  samples against the clock of the caller, so a lane that has been idle for an
  hour drops out of the 15 minute window instead of keeping its old numbers.
* the last run of a lane is the newest run, whether it reported usage or not:
  after a run without usage the lane shows "not collected", it never falls back
  to the numbers of an older run.

The module is pure: it reads no file and writes none.

Delivery mapping (Refs #1690, PR #1802)
----------------------------------------
B1 (windows recomputed against the current clock)  -> ``window``/``lane_view``
B2 (the newest run without usage stays missing)     -> ``normalize``/``lane_view``
B3 (the fleet burn table in the health check)       -> ``fleet_table``/``render_table``
B4 (no unrelated ELF build artifacts in the change)  -> n/a (see the commit)
B5 (integration coverage of the run end, the remote
    collection and the dispatcher tick)              -> openswarm/tests/test_dispatch_usage.py
B6 (the health check reads the dispatch state dir)  -> openswarm/bin/openswarm (fleet health)
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

# The canonical usage of one run.
USAGE_KEYS = ("input", "output", "cache_read", "cache_write")

# The windows the fleet status shows.
WINDOW_SHORT_S = 900       # the 15 minute window of a lane
WINDOW_LONG_S = 3600       # the hour of the fleet table

# What a missing reading looks like in the human readable tables.
MISSING_LABEL = "未采集"

# The names the executors report their counts under.
_ALIASES = {
    "input": ("input", "input_tokens", "inputTokens", "prompt_tokens", "promptTokens",
              "in_tok", "input_tok"),
    "output": ("output", "output_tokens", "outputTokens", "completion_tokens", "completionTokens",
               "out_tok", "output_tok"),
    "cache_read": ("cache_read", "cacheRead", "cache_read_tokens", "cacheReadTokens",
                   "cache_read_input_tokens", "cached_tokens"),
    "cache_write": ("cache_write", "cacheWrite", "cache_write_tokens", "cacheWriteTokens",
                    "cache_creation_input_tokens"),
}


def _count(value: Any) -> Optional[int]:
    """A token count, or None when the value is not a usable count."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    return max(0, value)


def _epoch(value: Any) -> Optional[float]:
    """A timestamp in seconds, or None when the value is not a usable timestamp."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        import datetime

        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.datetime.strptime(text, pattern).replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except ValueError:
                continue
    return None


def normalize(raw: Any) -> Optional[Dict[str, int]]:
    """The canonical usage of a run, or None when the run reported nothing usable.

    The backends report their counts under different names; the canonical
    record always has the four keys.  A run whose counts are all zero (a
    backend that could not read its own usage) has no reading at all: it is
    reported as missing instead of as a zero burn.
    """
    if not isinstance(raw, Mapping):
        return None
    counts: Dict[str, int] = {}
    for key, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in raw:
                count = _count(raw[alias])
                if count is not None:
                    counts[key] = count
                break
    if not counts or not any(counts.values()):
        return None
    return {key: counts.get(key, 0) for key in USAGE_KEYS}


def collected(raw: Any) -> bool:
    """Whether a usage reading holds usable counts."""
    return normalize(raw) is not None


def zero() -> Dict[str, int]:
    """The empty usage record."""
    return {key: 0 for key in USAGE_KEYS}


def add(left: Any, right: Any) -> Dict[str, int]:
    """The sum of two usage records; a missing record counts as nothing."""
    out = zero()
    for source in (left, right):
        if not isinstance(source, Mapping):
            continue
        for key in USAGE_KEYS:
            count = _count(source.get(key))
            if count is not None:
                out[key] += count
    return out


def total(usage: Any) -> int:
    """Every token of a usage record, cache included.

    The total is the reading of the MiniMax console: the input and the output
    plus the cached tokens.  ``cache_total`` is the cached part of it.
    """
    counts = usage if isinstance(usage, Mapping) else {}
    return sum(_count(counts.get(key)) or 0 for key in USAGE_KEYS)


def cache_total(usage: Any) -> int:
    """The cached tokens of a usage record: the cache reads plus the cache writes."""
    counts = usage if isinstance(usage, Mapping) else {}
    return sum(_count(counts.get(key)) or 0 for key in ("cache_read", "cache_write"))


def shares(usage: Any) -> Dict[str, float]:
    """The input, output and cache shares of a usage record, in percent.

    The shares are the reading of the MiniMax console: the input tokens are
    the bulk of a session, the cache reads are what keeps it cheap.
    """
    counts = usage if isinstance(usage, Mapping) else {}
    gross = total(counts)
    if not gross:
        return {"input": 0.0, "output": 0.0, "cache": 0.0}
    return {
        "input": round(100.0 * (_count(counts.get("input")) or 0) / gross, 1),
        "output": round(100.0 * (_count(counts.get("output")) or 0) / gross, 1),
        "cache": round(100.0 * cache_total(counts) / gross, 1),
    }


def sample(run: Any = None, lane: Any = None, at: Any = None, usage: Any = None) -> Dict[str, Any]:
    """One usage sample: a run of a lane, when it ended and what it burned."""
    return {"run": run, "lane": lane, "at": _epoch(at), "usage": usage}


def _sorted_samples(samples: Iterable[Any]) -> List[Mapping[str, Any]]:
    """The samples that can be placed on a timeline, oldest first."""
    out = []
    for entry in samples or []:
        if not isinstance(entry, Mapping):
            continue
        at = _epoch(entry.get("at"))
        if at is None:
            continue
        out.append(entry)
    out.sort(key=lambda entry: _epoch(entry.get("at")))
    return out


def window(samples: Iterable[Any], now: Any, seconds: float = WINDOW_SHORT_S) -> Dict[str, Any]:
    """The burn of the last ``seconds`` seconds, recomputed against ``now``.

    The window is never cached: the caller passes the clock of the moment it
    writes the status, so a lane that stopped running ages out of the window
    instead of keeping the numbers of its last run forever.
    """
    moment = _epoch(now)
    limit = float(seconds)
    counts = zero()
    runs = 0
    measured = 0
    for entry in _sorted_samples(samples):
        at = _epoch(entry.get("at"))
        if moment is None or at is None:
            continue
        if at > moment or at < moment - limit:
            continue
        runs += 1
        usage = normalize(entry.get("usage"))
        if usage is None:
            continue
        counts = add(counts, usage)
        measured += 1
    out = dict(counts)
    out.update({"runs": runs, "measured": measured, "window_s": limit,
                "total": total(counts), "cache": cache_total(counts)})
    return out


def last_run(samples: Iterable[Any]) -> Optional[Dict[str, Any]]:
    """The newest run of a lane, with or without a usage reading.

    The newest run wins even when it reported nothing: falling back to an
    older run would show the burn of a run that is no longer the last one.
    """
    ordered = _sorted_samples(samples)
    if not ordered:
        return None
    entry = ordered[-1]
    usage = normalize(entry.get("usage"))
    return {"run": entry.get("run"), "lane": entry.get("lane"), "at": _epoch(entry.get("at")),
            "collected": usage is not None, "usage": usage}


def lane_view(samples: Iterable[Any], now: Any,
              short_s: float = WINDOW_SHORT_S, long_s: float = WINDOW_LONG_S) -> Dict[str, Any]:
    """The burn of one lane: its last run and the two windows around it."""
    ordered = _sorted_samples(samples)
    last = last_run(ordered)
    return {"last": last or {"run": None, "lane": None, "at": None, "collected": False, "usage": None},
            "window_15m": window(ordered, now, short_s),
            "window_1h": window(ordered, now, long_s)}


def fleet_view(lanes: Mapping[str, Any], now: Any,
               short_s: float = WINDOW_SHORT_S, long_s: float = WINDOW_LONG_S) -> Dict[str, Any]:
    """The burn of the whole fleet: per lane, per host and the fleet total.

    ``lanes`` maps a lane name to ``{"host": ..., "samples": [...]}``.
    """
    rows: List[Dict[str, Any]] = []
    hosts: Dict[str, Dict[str, Any]] = {}
    short_total, long_total = zero(), zero()
    for name in sorted(lanes or {}):
        entry = lanes[name] if isinstance(lanes.get(name), Mapping) else {}
        samples = entry.get("samples") or []
        view = lane_view(samples, now, short_s, long_s)
        host = entry.get("host")
        row = {"lane": name, "host": host, "last": view["last"],
               "window_15m": view["window_15m"], "window_1h": view["window_1h"]}
        rows.append(row)
        short_total = add(short_total, view["window_15m"])
        long_total = add(long_total, view["window_1h"])
        if host:
            bucket = hosts.setdefault(host, {"host": host, "window_15m": zero(), "window_1h": zero(),
                                              "lanes": []})
            bucket["window_15m"] = add(bucket["window_15m"], view["window_15m"])
            bucket["window_1h"] = add(bucket["window_1h"], view["window_1h"])
            bucket["lanes"].append(name)
    host_rows = []
    for host in sorted(hosts):
        bucket = hosts[host]
        for key in ("window_15m", "window_1h"):
            counts = bucket[key]
            counts["total"] = total(counts)
            counts["cache"] = cache_total(counts)
        host_rows.append(bucket)
    for counts in (short_total, long_total):
        counts["total"] = total(counts)
        counts["cache"] = cache_total(counts)
    return {"lanes": rows, "hosts": host_rows, "window_15m": short_total, "window_1h": long_total}


def fleet_table(status: Any) -> Optional[Dict[str, Any]]:
    """The burn table of a lanes-status snapshot.

    The snapshot already holds the per lane windows the dispatcher wrote; the
    table adds the per host subtotals and the fleet total.  ``None`` means the
    snapshot holds no burn at all (an older dispatcher, or no snapshot).
    """
    if not isinstance(status, Mapping):
        return None
    lanes = status.get("lanes")
    entries = lanes if isinstance(lanes, Mapping) else status
    rows, hosts = [], {}
    short_total, long_total = zero(), zero()
    found = False
    for name in sorted(entries or {}):
        entry = entries[name]
        if not isinstance(entry, Mapping):
            continue
        if str(name).startswith("_"):
            continue
        usage = entry.get("usage")
        if not isinstance(usage, Mapping):
            continue
        found = True
        last = usage.get("last") if isinstance(usage.get("last"), Mapping) else {}
        short = usage.get("window_15m") if isinstance(usage.get("window_15m"), Mapping) else {}
        long = usage.get("window_1h") if isinstance(usage.get("window_1h"), Mapping) else {}
        row = {"lane": name, "host": entry.get("host"), "last": dict(last),
               "window_15m": dict(short), "window_1h": dict(long)}
        rows.append(row)
        short_total = add(short_total, short)
        long_total = add(long_total, long)
        host = entry.get("host")
        if host:
            bucket = hosts.setdefault(host, {"host": host, "window_15m": zero(),
                                              "window_1h": zero(), "lanes": []})
            bucket["window_15m"] = add(bucket["window_15m"], short)
            bucket["window_1h"] = add(bucket["window_1h"], long)
            bucket["lanes"].append(name)
    if not found:
        return None
    host_rows = []
    for host in sorted(hosts):
        bucket = hosts[host]
        for key in ("window_15m", "window_1h"):
            counts = bucket[key]
            counts["total"] = total(counts)
            counts["cache"] = cache_total(counts)
        host_rows.append(bucket)
    for counts in (short_total, long_total):
        counts["total"] = total(counts)
        counts["cache"] = cache_total(counts)
    return {"lanes": rows, "hosts": host_rows, "window_15m": short_total, "window_1h": long_total,
            "at": status.get("_at")}


def format_tokens(value: Any) -> str:
    """A token count for a human reader."""
    count = _count(value)
    if count is None:
        return MISSING_LABEL
    return f"{count:,}"


def _measured(window: Any) -> bool:
    """Whether a window holds at least one measured run."""
    if not isinstance(window, Mapping):
        return False
    measured = window.get("measured")
    if isinstance(measured, int) and not isinstance(measured, bool):
        return measured > 0
    return bool(window.get("total"))


def _window_text(window: Any) -> str:
    """A window for a human reader: the total, or the missing label."""
    if not _measured(window):
        return MISSING_LABEL
    return format_tokens(window.get("total"))


def _width(text: str) -> int:
    """The display width of a text: the CJK glyphs take two columns."""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def _pad(text: str, width: int, right: bool = True) -> str:
    """Pad a text to a display width."""
    fill = max(0, width - _width(text))
    return (" " * fill + text) if right else (text + " " * fill)


def render_table(table: Any, title: str = "fleet burn") -> List[str]:
    """The burn table of a fleet for a human reader.

    The table names the input and the cache share of the hour, so the reading
    can be compared with the MiniMax console.  A run without a usage reading
    shows the missing label, never a zero.
    """
    if not isinstance(table, Mapping):
        return [f"{title}: {MISSING_LABEL}"]
    lines = [f"{title} (tokens; input share / cache share of the last hour)"]
    header = ["lane", "host", "last input", "last output", "cache read", "cache write", "15m", "1h"]
    widths = [10, 10, 13, 13, 12, 12, 14, 14]
    lines.append(" ".join(_pad(name, width, right=False) for name, width in zip(header, widths)))
    for row in table.get("lanes") or []:
        last = row.get("last") if isinstance(row.get("last"), Mapping) else {}
        usage = last.get("usage") if isinstance(last.get("usage"), Mapping) else {}
        if not last.get("collected"):
            cells = [MISSING_LABEL] * 6
        else:
            cells = [format_tokens(usage.get("input")), format_tokens(usage.get("output")),
                     format_tokens(usage.get("cache_read")), format_tokens(usage.get("cache_write")),
                     _window_text(row.get("window_15m")), _window_text(row.get("window_1h"))]
        cells = [str(row.get("lane")), str(row.get("host") or "-")] + cells
        lines.append(" ".join([_pad(cells[0], widths[0], right=False), _pad(cells[1], widths[1], right=False)]
                              + [_pad(cell, width) for cell, width in zip(cells[2:], widths[2:])]))
    for bucket in table.get("hosts") or []:
        lines.append(f"host {bucket.get('host')}: 1h {_window_text(bucket.get('window_1h'))} "
                     f"(input {_share_text(bucket.get('window_1h'), 'input')}%, "
                     f"cache {_share_text(bucket.get('window_1h'), 'cache')}%)")
    hour = table.get("window_1h")
    lines.append(f"fleet 1h total: {_window_text(hour)} "
                 f"(input {_share_text(hour, 'input')}%, cache {_share_text(hour, 'cache')}%)")
    return lines


def _share_text(window: Any, key: str) -> str:
    """The share of a window, in percent, for a human reader."""
    if not _measured(window):
        return MISSING_LABEL
    return f"{shares(window).get(key, 0.0):.1f}"
