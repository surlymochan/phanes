"""The MiniMax burn reading the lane status shows (Refs #1671).

``tools/mm-burn-rate.py`` is the MiniMax burn watchdog: it reads the provider
request log of the MiniMax broker (``openswarm/bin/pi-minimax-broker.mjs``),
whose rows carry the token counters MiniMax itself reported per request, and
writes a JSON snapshot.  Both dispatchers show the hourly token reading of
that snapshot in ``lanes-status.json``:
``tools/lane-autodispatch/autodispatch.py`` (the box) and
``openswarm/lib/dispatch/engine.py`` (the fleet).

The snapshot is only shown while the *reading* is fresh.  The watchdog writes
``token_observed_at`` -- the moment the reading was taken -- and the reading
is dropped when it is older than the freshness window: a watchdog that stopped
must not show the last known burn as the current one.  The 2026-11-02 collapse
was only found in the MiniMax console because the fleet side had no token
reading at all.

``token_sample_at`` is the timestamp of the newest provider request the
reading is based on -- the token sample itself, not the timestamp of the
newest quota sample: the two can differ, and only the token sample describes
the burn reading.

The snapshot's own ``status`` describes the weekly quota history, not the token
reading, so it is not a gate here: the token reading decides.

Standard library only, Python 3.9+.
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Dict, Optional

__all__ = [
    "DEFAULT_MAX_AGE_S",
    "parse_ts",
    "read_snapshot",
    "snapshot_fields",
]

#: A reading older than this is not shown in the status (seconds).  The
#: box hotfix uses the same 900 s window for its quota signals.
DEFAULT_MAX_AGE_S = 900

#: A stamp further in the future than this is a clock skew, not a reading.
MAX_FUTURE_S = 60


def parse_ts(value: Any) -> datetime.datetime:
    """Parse an ISO-8601 timestamp into an aware datetime."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(text)
    return parsed.replace(tzinfo=datetime.timezone.utc) if parsed.tzinfo is None else parsed


def _now(now: Any) -> datetime.datetime:
    """The reference time: a datetime, an epoch second or an ISO stamp."""
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if isinstance(now, datetime.datetime):
        return now.replace(tzinfo=datetime.timezone.utc) if now.tzinfo is None else now
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    return parse_ts(now)


def snapshot_fields(snapshot: Any, now: Any = None,
                    max_age_s: int = DEFAULT_MAX_AGE_S) -> Dict[str, Any]:
    """The status fields of a burn snapshot, or ``{}`` when it must not be shown.

    A snapshot is usable when it is an object with a numeric ``hourly_tokens``
    and a reading that is not older than ``max_age_s`` seconds (and not in the
    future).  ``token_observed_at`` is the moment the reading was taken;
    ``written_at`` is the fallback for a snapshot of an older watchdog.  The
    token sample (``token_sample_at``) is reported as the moment the reading
    is based on, but it is not the freshness gate: a genuine zero burn has no
    request at all and is still a reading of this hour.

    A snapshot without a reading is a *collection failure* (the collector is
    gone, the log lost rows, the collector cannot be verified): there is no
    token count to show, but the state is shown, so the operator tells a
    broken collection apart from a fleet that burned nothing.  The state of
    the collector itself (``collector_state``) is shown with it.

    The consumption of the current Token Plan interval (``interval_tokens``)
    is shown with the hour: it is the figure of the interval the MiniMax
    console shows, and it is only set when the rows of that interval are
    complete (the watchdog leaves it empty when the log lost rows).
    """
    if not isinstance(snapshot, dict):
        return {}
    tokens = snapshot.get("hourly_tokens")
    stamp = snapshot.get("token_observed_at") or snapshot.get("written_at")
    if not isinstance(stamp, str):
        return {}
    try:
        observed = parse_ts(stamp)
    except ValueError:
        return {}
    age = (_now(now) - observed).total_seconds()
    if age < -MAX_FUTURE_S or age > max_age_s:
        return {}
    fields = {}
    if not isinstance(tokens, bool) and isinstance(tokens, (int, float)):
        fields.update({"_minimax_hourly_tokens": int(tokens),
                       "_minimax_token_alert": bool(snapshot.get("token_alert")),
                       "_minimax_usage_at": snapshot.get("token_sample_at") or stamp,
                       "_minimax_token_age_s": int(age),
                       "_minimax_token_state": str(snapshot.get("token_state") or "unknown")})
    elif snapshot.get("token_state"):
        # A collection failure is no reading, but it is not a zero burn
        # either: the state says which one it is.
        fields["_minimax_token_state"] = str(snapshot.get("token_state"))
        fields["_minimax_token_age_s"] = int(age)
    interval = snapshot.get("interval_tokens")
    if not isinstance(interval, bool) and isinstance(interval, (int, float)):
        # The consumption of the current Token Plan interval: the operator
        # compares it with the interval figure of the MiniMax console.
        fields["_minimax_interval_tokens"] = int(interval)
        requests = snapshot.get("interval_requests")
        if not isinstance(requests, bool) and isinstance(requests, (int, float)):
            fields["_minimax_interval_requests"] = int(requests)
        reset = snapshot.get("interval_reset_at")
        if isinstance(reset, str):
            fields["_minimax_interval_reset_at"] = reset
    collector = snapshot.get("collector_state")
    if isinstance(collector, str):
        # The state of the collector behind the reading: "alive" is a verified
        # reading, the other states are collection failures.
        fields["_minimax_collector_state"] = collector
    last = snapshot.get("token_last_request_at")
    if isinstance(last, str):
        fields["_minimax_token_last_request_at"] = last
    return fields


def read_snapshot(path: Any, now: Any = None,
                  max_age_s: int = DEFAULT_MAX_AGE_S) -> Dict[str, Any]:
    """The status fields of the burn snapshot at ``path`` (``{}`` when unusable).

    A missing or malformed snapshot is not an error: the lane status must keep
    working when the burn watchdog never ran.
    """
    try:
        with open(path, encoding="utf-8") as stream:
            snapshot = json.load(stream)
    except (OSError, ValueError, TypeError):
        return {}
    return snapshot_fields(snapshot, now, max_age_s)
