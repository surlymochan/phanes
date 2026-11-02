#!/usr/bin/env python3
"""Offline contract for fresh-window quota signals in health-check (Refs #1547).

``health-check.py`` must not read an old log line as a live quota signal:

* every quota signal carries a timestamp, and only counts while it is inside
  the fresh window (the box hotfix uses ``HEALTH_QUOTA_FRESH_SEC=900``);
* a line from before the current quota window never counts either;
* for MiniMax the ``coding_plan/remains`` API is authoritative: an exhausted
  plan yields ``mm_remains:until=<reset>`` for the backend that consumes the
  provider (``pi`` in ``openswarm/policy.json``), while a plan that still has
  quota lets an automatic quota pause be released early.

The gate names follow the repository convention ``PAUSE-BACKEND-<backend>``
(``openswarm/lib/dispatch/engine.py``, ``docs/ctl-boot.md``): the backend is
resolved from the caller's lane->backend mapping, never from the lane name.

Everything below is offline: no file is read and no state directory is
touched.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


QUOTA_FRESH = 900  # box hotfix: HEALTH_QUOTA_FRESH_SEC=900
CLEAR_MIN_PCT = 5  # a plan with >= 5% left may release the pause early
TZ8 = timezone(timedelta(hours=8))

# The box logs local time without an offset, so the offset stays optional.
TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?)"
)
# The quota line is tagged with the lane that produced it (log_quota:M6).
TAG_RE = re.compile(r"log_quota:(?P<lane>[A-Za-z0-9_.-]+)")
# Some lines carry explicit key=value fields instead of the tag.
FIELD_RE = re.compile(r"(?:^|\s)(?P<key>lane|backend)=(?P<value>[^\s]+)")
# Automatic bodies that are not quota pauses (host cooldown, mem/disk holds).
NON_QUOTA_AUTO = ("auto:cooldown:", "auto:host_hard", "auto:mem_hard")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=TZ8) if parsed.tzinfo is None else parsed


def _lane_of(line: str) -> str | None:
    """The lane that produced the signal: the ``log_quota:<LANE>`` tag first."""
    tag = TAG_RE.search(line)
    if tag:
        return tag.group("lane")
    for key, value in FIELD_RE.findall(line):
        if key == "lane":
            return value
    return None


def _backend_of(line: str, lane: str, lane_backends: dict[str, str]) -> str | None:
    """The backend to gate: an explicit ``backend=`` field, else the lane's backend."""
    for key, value in FIELD_RE.findall(line):
        if key == "backend":
            return value
    return lane_backends.get(lane)


def scan_quota(
    lines,
    lanes,
    now,
    fresh_s,
    mm_state,
    window_start=0.0,
    lane_backends=None,
):
    """Return ``{pause name: [reasons]}`` for the quota signals to act on.

    A line counts only when it

    * carries a timestamp -- an untimestamped ``rate_limit`` line is not a
      quota signal at all;
    * is not older than ``fresh_s`` seconds and not in the future;
    * is not older than ``window_start`` (the current quota window);
    * names a watched lane and a backend (``backend=`` field or the caller's
      ``lane_backends`` mapping).

    The remains conclusion of the MiniMax ``coding_plan`` API is authoritative
    for the backend that consumes the provider; it never drops the log hits
    collected above, and it does not invent a backend the repository does not
    know.  The first reason of a pause is the body to write (remains wins over
    the log), the remaining ones keep the other evidence visible.
    """
    lane_backends = lane_backends or {}
    hits: dict[str, dict[str, float]] = {}
    for line in lines:
        if "rate_limit" not in line:
            continue
        match = TS_RE.search(line)
        if not match:
            continue  # a quota signal must carry a timestamp
        stamp = _parse_time(match.group(1)).timestamp()
        age = now - stamp
        if age < 0 or age > fresh_s or stamp < window_start:
            continue
        lane = _lane_of(line)
        if not lane or lane not in lanes:
            continue
        backend = _backend_of(line, lane, lane_backends)
        if not backend:
            continue  # without a backend there is no PAUSE-BACKEND-<backend> gate
        hits.setdefault(f"PAUSE-BACKEND-{backend}", {})[f"log_quota:{lane}"] = stamp

    signals: dict[str, list[str]] = {}
    for name, reasons in hits.items():
        signals[name] = [
            reason for reason, _ in sorted(reasons.items(), key=lambda kv: (kv[1], kv[0]))
        ]
    if mm_state.get("exhausted"):
        name = f"PAUSE-BACKEND-{mm_state.get('backend') or 'pi'}"
        reasons = signals.setdefault(name, [])
        reason = f"mm_remains:until={mm_state['until']}"
        if reason not in reasons:
            reasons.insert(0, reason)
    return signals


def signal_backends(signals: dict[str, list[str]]) -> set[str]:
    """The backends that currently carry a fresh quota signal."""
    return {
        name.removeprefix("PAUSE-BACKEND-")
        for name in signals
        if name.startswith("PAUSE-BACKEND-")
    }


def should_clear_auto_pause(
    pause_name: str,
    pause_text: str,
    remaining_pct: float | None,
    fresh_backends=(),
) -> bool:
    """Whether an automatic pause may be released early because quota came back.

    Mirrors ``tools/check-health-pause.py``: a ``PAUSE-BACKEND`` pause is only
    released while no fresh quota signal exists for that backend, so a fresh
    429 keeps the gate closed even when the provider reports quota again.
    A manual pause (body not starting with ``auto:``) and the non-quota
    automatic bodies (``auto:cooldown:`` and friends) are never released here.
    """
    if not pause_name.startswith("PAUSE-BACKEND-"):
        return False  # host and lane pauses are not quota gates
    if not pause_text.startswith("auto:"):
        return False  # a manual pause is never cleared
    if pause_text.startswith(NON_QUOTA_AUTO):
        return False  # a cooldown/host_hard/mem_hard body is not a quota pause
    if remaining_pct is None or remaining_pct < CLEAR_MIN_PCT:
        return False
    backend = pause_name.removeprefix("PAUSE-BACKEND-")
    return backend not in set(fresh_backends)


def main() -> None:
    now = datetime(2026, 11, 2, 6, 30, tzinfo=TZ8).timestamp()
    window_start = datetime(2026, 11, 2, 6, 27, tzinfo=TZ8).timestamp()
    lanes = ["M6", "M1"]
    # The lane->backend mapping the caller already owns (the gate is named after
    # the backend, the lane only appears in the body of the pause).
    lane_backends = {"M6": "pi", "M1": "codex"}

    # The 06:13 line of the #1547 report (box health-check log).  The report
    # pins the shape that matters: a local timestamp, the ``log_quota:<LANE>``
    # tag of the lane that produced the line and the ``rate_limit`` marker.
    # The box log file is not part of this repository, so the fixture keeps
    # exactly that minimal shape: the parser must not depend on an extra
    # ``lane=`` / ``backend=`` field to see the signal at all.
    rate_limit_0613 = "2026-11-02 06:13:41 log_quota:M6 rate_limit"
    # The same line with the explicit key=value fields some lines carry.
    rate_limit_0613_fields = "2026-11-02 06:13:41 log_quota:M6 lane=M6 backend=pi rate_limit"
    fresh_pi = "2026-11-02 06:29:10 log_quota:M6 rate_limit"
    fresh_pi_fields = "2026-11-02 06:29:10 log_quota:M6 lane=M6 backend=pi rate_limit"
    fresh_codex = "2026-11-02 06:28:20 log_quota:M1 rate_limit"
    pre_window_pi = "2026-11-02 06:25:10 log_quota:M6 rate_limit"
    future_pi = "2026-11-02 06:35:10 log_quota:M6 rate_limit"
    no_timestamp = "log_quota:M6 rate_limit"
    other_lane = "2026-11-02 06:29:30 log_quota:M9 rate_limit"
    no_backend = "2026-11-02 06:29:40 log_quota:M9 rate_limit"

    # The two remains samples of the coding_plan API: the exhausted one and the
    # one that still has quota left.  The backend is the one that consumes the
    # MiniMax provider: ``pi`` runs on ``provider: minimax-cn`` with
    # ``default_model: MiniMax-M3.1-Flash`` (openswarm/policy.json), so the
    # remains conclusion gates ``PAUSE-BACKEND-pi`` -- not a backend name the
    # repository does not know.
    mm_exhausted = {
        "exhausted": True,
        "remaining_pct": 0.0,
        "until": "2026-11-02T06:29:00+08:00",
        "backend": "pi",
    }
    mm_available = {
        "exhausted": False,
        "remaining_pct": 80.0,
        "until": None,
        "backend": "pi",
    }
    no_hits: dict[str, list[str]] = {}

    cases = [
        ("quota signal without a timestamp is not a signal",
         scan_quota([no_timestamp], lanes, now, QUOTA_FRESH, mm_available,
                    lane_backends=lane_backends) == no_hits),
        ("06:13 line outside the fresh window is not a signal",
         scan_quota([rate_limit_0613], lanes, now, QUOTA_FRESH, mm_available,
                    lane_backends=lane_backends) == no_hits),
        ("06:13 line with lane=/backend= fields is not a signal either",
         scan_quota([rate_limit_0613_fields], lanes, now, QUOTA_FRESH, mm_available,
                    lane_backends=lane_backends) == no_hits),
        ("pi line before the current window start is not a signal",
         scan_quota([pre_window_pi], lanes, now, QUOTA_FRESH, mm_available,
                    window_start, lane_backends=lane_backends) == no_hits),
        ("line of an unwatched lane is not a signal",
         scan_quota([other_lane], lanes, now, QUOTA_FRESH, mm_available,
                    lane_backends=lane_backends) == no_hits),
        ("line without a resolvable backend names no gate",
         scan_quota([no_backend], ["M9"], now, QUOTA_FRESH, mm_available,
                    lane_backends=lane_backends) == no_hits),
        ("line with a future timestamp is not a signal",
         scan_quota([future_pi], lanes, now, QUOTA_FRESH, mm_available,
                    lane_backends=lane_backends) == no_hits),
        ("fresh in-window line gates the backend of its lane",
         scan_quota([fresh_pi], lanes, now, QUOTA_FRESH, mm_available,
                    window_start, lane_backends=lane_backends) ==
         {"PAUSE-BACKEND-pi": ["log_quota:M6"]}),
        ("fresh line with lane=/backend= fields gates the same backend",
         scan_quota([fresh_pi_fields], lanes, now, QUOTA_FRESH, mm_available,
                    window_start, lane_backends=lane_backends) ==
         {"PAUSE-BACKEND-pi": ["log_quota:M6"]}),
        ("mm exhausted yields mm_remains:until= for the pi backend",
         scan_quota([], lanes, now, QUOTA_FRESH, mm_exhausted,
                    lane_backends=lane_backends) ==
         {"PAUSE-BACKEND-pi": ["mm_remains:until=2026-11-02T06:29:00+08:00"]}),
        ("mm exhausted keeps the fresh log hit of the same backend",
         scan_quota([fresh_pi], lanes, now, QUOTA_FRESH, mm_exhausted,
                    window_start, lane_backends=lane_backends) ==
         {"PAUSE-BACKEND-pi": ["mm_remains:until=2026-11-02T06:29:00+08:00",
                               "log_quota:M6"]}),
        ("mm exhausted keeps the fresh log hit of another lane",
         scan_quota([fresh_codex], lanes, now, QUOTA_FRESH, mm_exhausted,
                    window_start, lane_backends=lane_backends) ==
         {"PAUSE-BACKEND-codex": ["log_quota:M1"],
          "PAUSE-BACKEND-pi": ["mm_remains:until=2026-11-02T06:29:00+08:00"]}),
        ("available remains adds no pause of its own",
         scan_quota([], lanes, now, QUOTA_FRESH, mm_available,
                    lane_backends=lane_backends) == no_hits),
        ("manual pause is never cleared early",
         not should_clear_auto_pause("PAUSE-BACKEND-pi", "operator hold\n", 80)),
        ("quota auto pause is released early at five percent",
         should_clear_auto_pause("PAUSE-BACKEND-pi", "auto:quota:M6:rate_limit\n", 5)),
        ("quota auto pause is kept below five percent or when unknown",
         not should_clear_auto_pause("PAUSE-BACKEND-pi", "auto:quota:M6:rate_limit\n", 4.9)
         and not should_clear_auto_pause("PAUSE-BACKEND-pi", "auto:quota:M6:rate_limit\n", None)),
        ("available remains releases the pause while no signal is fresh",
         should_clear_auto_pause("PAUSE-BACKEND-pi", "auto:quota:M6:rate_limit\n",
                                 mm_available["remaining_pct"],
                                 signal_backends(scan_quota(
                                     [], lanes, now, QUOTA_FRESH, mm_available,
                                     lane_backends=lane_backends)))),
        ("fresh quota signal keeps the gate closed although quota came back",
         not should_clear_auto_pause("PAUSE-BACKEND-pi", "auto:quota:M6:rate_limit\n",
                                    mm_available["remaining_pct"],
                                    signal_backends(scan_quota(
                                        [fresh_pi], lanes, now, QUOTA_FRESH, mm_available,
                                        window_start, lane_backends=lane_backends)))),
        ("host cooldown is not released early with quota remaining",
         not should_clear_auto_pause("PAUSE-HOST-box", "auto:cooldown:until=06:40\n", 80)),
        ("lane pause is not a quota gate",
         not should_clear_auto_pause("PAUSE-M6", "auto:quota:M6:rate_limit\n", 80)),
    ]
    for name, ok in cases:
        print(("OK: " if ok else "FAIL: ") + name)
    if any(not ok for _, ok in cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
