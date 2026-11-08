"""The decisions of the stale `in-progress` sweep of the dispatch loop (Refs #1493).

The dispatch loop labels an issue `in-progress` before the run is really alive.
When the executor dies in that window the label stays behind and the issue is
never scheduled again (a "ghost" in-progress).  A sweep that judges the label by
its age alone is worse than no sweep at all: it strips the label of a card that
is still running (the 05:12 box pause accident behind #1403) and the issue is
dispatched a second time.

This module is the decision part of the sweep and nothing else: it reads the
labelled issues, the leases of ``lib/dispatch/lease.py``, the issues the lanes
still hold and the moment of the last box resume, and it answers ``keep`` or
``strip`` for every labelled issue.  It never writes a label, never touches the
network and never raises on a broken row, so the wiring stays with the caller
(#1364).

The decisions, in the order they are taken:

* ``clock_rollback``: the label is newer than ``now``, or the resume marker is
  newer than ``now`` (the clock jumped back) -- a clock that jumped cannot
  prove that anything is idle.
* ``no_timestamp``: the row has no readable ``updatedAt``.
* ``resume_grace``: the last box resume is less than ``resume_grace_s`` old,
  the clock may have jumped during the pause.
* ``label_debt``: the issue carries a label debt.
* ``held``: a lane still holds the issue.
* ``lease_alive``: the lease of the issue is not over yet.
* ``within_grace``: the label is not older than ``grace_s``.
* ``stale``: everything else, the only reason that strips the label.

Timestamp convention
--------------------
An epoch is plain seconds and **every naive timestamp** (``%F %T``, ``%FT%T``,
an ISO string without a zone) is read as **UTC**.  The ``box-resume`` marker is
written by the box, so the convention has to be the same on every host: a naive
value parsed as local time would shift the resume grace by the offset of the
host (eight hours on a UTC+8 box) and the grace would protect nothing.

Review blockers of #1493 -> the change and the test that covers it:

* B1: a non finite timestamp (``updatedAt="nan"``, ``now=NaN``) must never
  strip the label -> :func:`_epoch` refuses every non finite value and
  :func:`decide` keeps the label when the clock cannot be read ->
  ``test_non_finite_timestamps_keep_the_label``.
* B2: a ``box-resume`` marker in the future (the clock jumped back) must never
  strip the label -> :func:`decide` keeps the label when the marker is newer
  than ``now`` -> ``test_a_resume_marker_in_the_future_keeps_the_label``.
* B3: the naive timestamps of the marker are UTC, so ``%F %T`` and ``%FT%T``
  read the same epoch -> :func:`_epoch` pins every naive value to UTC ->
  ``test_load_resume_at_reads_the_first_line_as_utc``.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

# The idle window before a label is considered stale (the default of `grace_s`).
DEFAULT_GRACE_S = 900.0
# The window after a box resume in which nothing is considered idle.
DEFAULT_RESUME_GRACE_S = 600.0

# The timestamp formats of the `box-resume` marker; the naive ones are UTC.
TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
)


def _finite(value: Any) -> Optional[float]:
    """A finite float, or None: a broken number is never a timestamp."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _epoch_of(moment: datetime) -> Optional[float]:
    """The epoch of a datetime; None when it is out of the finite range."""
    try:
        number = moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _epoch(value: Any) -> Optional[float]:
    """The epoch of a timestamp; None when the value cannot be read.

    An epoch is plain seconds, a naive timestamp is UTC (see the module
    docstring).  Every non finite number (``nan``, ``inf``) is refused: a
    broken clock must never look like an idle issue (B1).
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _finite(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        return _finite(number)
    for pattern in TIMESTAMP_FORMATS:
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return _epoch_of(parsed.replace(tzinfo=timezone.utc))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _epoch_of(parsed)


def _seconds(value: Any, default: float) -> float:
    """A number of seconds; the default when the value cannot be read."""
    number = _epoch(value)
    if number is None:
        return default
    return number if number > 0 else 0.0


def _issue(row: Any) -> Any:
    """The issue of a row; None when the row does not name one."""
    if isinstance(row, dict):
        for key in ("issue", "number", "n"):
            value = row.get(key)
            if value is not None:
                return value
        return None
    return row


def _usable_issue(value: Any) -> bool:
    """True when the value can be a lease key (an int or a non empty text)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    return False


def _updated(row: Any) -> Any:
    """The `updatedAt` of a row; None when the row has no timestamp."""
    if not isinstance(row, dict):
        return None
    for key in ("updatedAt", "updated_at", "updated"):
        if key in row:
            return row[key]
    return None


def _lease_expiry(leases: Any, number: Any, key: str) -> Optional[float]:
    """The end of the lease of an issue; None when it holds no lease.

    The lease store of ``lib/dispatch/lease.py`` refuses an issue that is
    neither an int nor a text; such a row is unusable and the caller keeps it
    (see :func:`_usable_issue`).
    """
    if leases is None:
        return None
    for candidate in (number, key):
        try:
            record = leases.get(candidate)
        except (ValueError, TypeError):
            return None
        if isinstance(record, dict):
            return _epoch(record.get("expires_at"))
    return None


def decide(
    labelled: Iterable[Any],
    held: Iterable[Any],
    leases: Any,
    resume_at: Any,
    now: Any,
    grace_s: Any = DEFAULT_GRACE_S,
    resume_grace_s: Any = DEFAULT_RESUME_GRACE_S,
    debt: Iterable[Any] = (),
) -> List[Dict[str, Any]]:
    """The keep/strip decision of every labelled issue.

    `labelled` are the rows of the issues that carry the `in-progress` label
    (with their `updatedAt`), `held` the issues a lane still holds, `leases` a
    ``LeaseStore`` of ``lib/dispatch/lease.py``, `resume_at` the moment of the
    last box resume and `now` the current moment.  `grace_s` is the idle
    window of a label, `resume_grace_s` the window after a resume and `debt`
    the issues that carry a label debt.

    Every labelled issue gets exactly one decision::

        {"issue": 1403, "action": "keep", "reason": "lease_alive"}

    The decision never raises: a row that cannot be read is kept with a
    reason, because a sweep that strips a label it cannot judge is the
    accident this module exists to prevent.
    """
    moment = _epoch(now)
    resumed = _epoch(resume_at)
    broken_resume = resume_at is not None and resumed is None
    grace = _seconds(grace_s, DEFAULT_GRACE_S)
    resume_grace = _seconds(resume_grace_s, DEFAULT_RESUME_GRACE_S)
    held_keys = {str(_issue(item)) for item in held or ()}
    debt_keys = {str(_issue(item)) for item in debt or ()}
    decisions: List[Dict[str, Any]] = []
    for row in labelled or ():
        number = _issue(row)
        key = str(number)
        updated = _epoch(_updated(row))
        reason = None
        if number is None or not _usable_issue(number):
            reason = "invalid_issue"
        elif moment is None:
            reason = "clock_rollback"
        elif updated is None:
            reason = "no_timestamp"
        elif moment < updated:
            reason = "clock_rollback"
        elif broken_resume:
            reason = "no_timestamp"
        elif resumed is not None and resumed > moment:
            reason = "clock_rollback"
        elif resumed is not None and moment - resumed < resume_grace:
            reason = "resume_grace"
        elif key in debt_keys:
            reason = "label_debt"
        elif key in held_keys:
            reason = "held"
        else:
            expiry = _lease_expiry(leases, number, key)
            if expiry is not None and moment < expiry:
                reason = "lease_alive"
            elif moment - updated <= grace:
                reason = "within_grace"
        decisions.append(
            {
                "issue": number,
                "action": "keep" if reason else "strip",
                "reason": reason or "stale",
            }
        )
    return decisions


def load_resume_at(path: Any) -> Optional[float]:
    """The moment of the last box resume; None when there is no marker.

    The marker is a small file whose first line is the moment of the resume:
    an epoch or a timestamp, naive values are UTC (see the module docstring).
    The rest of the file is ignored.  A missing or unreadable marker is None.
    """
    try:
        with open(os.fspath(path), encoding="utf-8") as stream:
            line = stream.readline()
    except (OSError, TypeError, ValueError):
        return None
    return _epoch(line.strip())
