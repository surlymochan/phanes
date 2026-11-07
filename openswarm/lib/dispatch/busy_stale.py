"""Busy entry expiry: a busy lane that shows no life any more is stale (Refs #1786).

Two field incidents of the same shape:

* M6 lost contact with the dispatcher and its busy entry kept saying
  ``busy #1361`` for 44 minutes.  Nothing was running any more, but the water
  level and the patrol both believed the lane was working.
* On another host two zombie busy entries (#1731, #1688) survived more than two
  hours, because the probe no longer covered the lane (that root cause was
  fixed in the probe layer).

A busy entry that nobody updates is worse than an idle entry: it hides the
lane from the water level and misleads everyone who reads the status.  This
module holds the *policy* of that expiry.  It is pure: it reads no file, runs
no command and never touches the clock -- the caller passes the readings and
the moment the verdict belongs to.

The verdict is built on the **last trustworthy activity** of the run, never on
the age of the dispatch:

``stamp(value)``
    Read one timestamp (epoch number, ISO-8601 text or the dispatcher's
    ``"%Y-%m-%d %H:%M:%S"`` wall time).

``observe(record, probe, now, dispatched_at)``
    Fold one probe reading into the activity record of a busy entry.  The
    trusted activity is the progress record the host itself keeps writing
    while the run works (the last child event or provider request of the
    run): a host that stops writing it has stopped working.  A host that is
    gone keeps the last reading we took, so the silence grows on its own --
    that is what expires the busy entry of a host that lost contact.  A host
    that never answered at all falls back to the dispatch moment: that is the
    only thing we know about it.

    The reading only moves the activity when the host's stamp *moves*, and the
    silence is measured on our own clock: a stamp that is repeated, or that
    lies in the future because the host clock runs ahead, never refreshes the
    activity.

``judge(record, now, max_idle_s)``
    The verdict for one busy entry: stale or not, and the numbers behind it.

The stale mark is an **overlay**: it never removes the run record, so a host
that comes back is harvested normally, and it lives in its own map so that it
survives the ticks until the run it marks is resolved or an operator handles
it explicitly.
"""
from __future__ import annotations

import datetime
import math
import time
from typing import Any, Dict, Optional

__all__ = [
    "DEFAULT_MAX_IDLE_S",
    "judge",
    "max_idle",
    "observe",
    "stamp",
]

# A busy lane that showed no life for half an hour is stale.  The fleet itself
# aborts a turn after ``lane.stall_after_s`` (900s by default) without a child
# event and without a provider request, so half an hour of silence is already
# past the point where the box would have given up on the run itself.
DEFAULT_MAX_IDLE_S = 1800.0

# The wall time the dispatcher writes into its own state ("%Y-%m-%d %H:%M:%S").
_LOCAL_FORMAT = "%Y-%m-%d %H:%M:%S"


def max_idle(value: Any, default: float = DEFAULT_MAX_IDLE_S) -> float:
    """The idle limit of the expiry, read from a configuration value.

    A value that is no number (or a negative one) falls back to the default.
    Zero switches the check off.
    """
    if value is None:
        return float(default)
    try:
        limit = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(limit) or limit < 0:
        return float(default)
    return limit


def stamp(value: Any) -> Optional[float]:
    """Return the epoch of a timestamp, or None when it names no moment.

    Numbers are epochs, ISO-8601 text is read as it stands (a naive stamp is
    local wall time, like the dispatcher's own ``at``) and the dispatcher's
    wall time is read as local wall time.  A stamp that is not finite is no
    stamp at all.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            pass
        else:
            return number if math.isfinite(number) else None
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            return parsed.timestamp()
        try:
            return time.mktime(time.strptime(text, _LOCAL_FORMAT))
        except ValueError:
            return None
    return None


def _kept(previous, dispatched_at, moment=None):
    """The record of a run whose host gave no new reading.

    The silence we measured keeps growing: the last activity we trusted stands.
    A run we never heard of falls back to the dispatch moment -- the only thing
    we know about it.  A run whose host answered but reported no progress at
    all stays unknown: an unknown reading is not evidence.
    """
    if previous.get("at") is not None or previous.get("since") is not None:
        return {"at": previous.get("at"), "since": previous.get("since"),
                "stamp": previous.get("stamp"), "source": previous.get("source") or "dispatch"}
    if dispatched_at is None:
        return {}
    start = stamp(dispatched_at)
    if start is None:
        start = moment
    if start is None:
        return {}
    return {"at": None, "since": start, "stamp": None, "source": "dispatch"}


def observe(record: Any, probe: Any, now: Any, dispatched_at: Any = None) -> Dict[str, Any]:
    """Fold one probe reading into the activity record of a busy entry.

    ``record`` is the record of the previous tick (or None), ``probe`` the
    reading of this tick and ``now`` the moment of this tick.  The returned
    record is a new mapping; the caller stores it.

    The record holds four things: ``at`` (the progress stamp the host
    reported), ``since`` (when we saw that stamp move, on *our* clock), ``stamp``
    (the raw stamp of the host) and ``source`` (``"host"`` when the host itself
    reported progress, ``"dispatch"`` when nothing but the dispatch is known).

    The host's progress record is the trusted activity: it is the moment of the
    last real step of the run (a child event or a provider request), written by
    the box itself.  A host that stops writing it has stopped working, and a
    host that is gone keeps the last reading we took -- the silence then grows
    on its own, which is what expires a busy entry of a host that lost contact.
    The dispatch moment is only the fallback of a host that never answered at
    all.

    The reading only moves the activity forward:

    * the same stamp again does not refresh anything, so a host that repeats one
      old stamp cannot keep a dead run alive;
    * the silence is measured on *our* clock, so the value of the host stamp
      never matters: a host clock that runs ahead (or behind) can neither hide
      nor fake a silence.
    """
    previous = record if isinstance(record, dict) else {}
    moment = stamp(now)
    probe = probe if isinstance(probe, dict) else {}
    if "activity" not in probe:
        # The host did not answer (unreachable, in backoff, ssh error): the silence
        # we measured keeps growing.
        return _kept(previous, dispatched_at, moment)
    host = stamp(probe.get("activity"))
    if host is None:
        # The host answered but reported no progress: an unknown reading is not
        # evidence, so the silence we measured keeps growing.
        return _kept(previous, None, moment)
    last = stamp(previous.get("at"))
    if last is not None and host == last:
        # The same old reading again: the activity does not move, the silence grows.
        return {"at": last, "since": previous.get("since"), "stamp": previous.get("stamp"),
                "source": previous.get("source") or "host"}
    if previous.get("since") is not None or last is not None:
        # We already track this run and its stamp moved: the run showed life now.
        since = moment
    else:
        # The very first reading of this run: the silence cannot be older than the
        # dispatch, that is the only moment we know the run existed.
        since = stamp(dispatched_at)
        if since is None:
            since = moment
        elif moment is not None:
            since = min(since, moment)
    return {"at": host, "since": since, "stamp": host, "source": "host"}


def judge(record: Any, now: Any, max_idle_s: Any = DEFAULT_MAX_IDLE_S) -> Dict[str, Any]:
    """The verdict for one busy entry: stale or not, and the numbers behind it.

    ``idle_s`` is the time since the last trustworthy activity, measured on our
    own clock (the clock of the host may drift, so its stamps are only used to
    see *that* the run moved).  It is None when no activity is known at all,
    and a lane without a known activity is never called stale (an unknown
    reading is not evidence).
    """
    moment = stamp(now)
    record = record if isinstance(record, dict) else {}
    limit = float(max_idle_s) if max_idle_s is not None else 0.0
    if not math.isfinite(limit) or limit < 0:
        limit = 0.0
    since = stamp(record.get("since"))
    if since is None:
        since = stamp(record.get("at"))     # a record written before we tracked our own clock
    idle = None if (since is None or moment is None) else max(0.0, moment - since)
    stale = bool(idle is not None and limit > 0 and idle >= limit)
    return {"stale": stale, "idle_s": idle, "activity_at": stamp(record.get("at")),
            "source": record.get("source"), "limit_s": limit}
