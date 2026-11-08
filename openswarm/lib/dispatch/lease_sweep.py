"""The decisions of the `in-progress` leak sweep (Refs #1364).

The dispatch labels an issue ``in-progress`` before the run is really alive.
When the dispatcher dies in that window (or the executor never starts) the
label stays behind and the issue is never scheduled again: a "ghost"
in-progress.

The lease (:mod:`lease`) bounds that window:

* the lease is taken **before** the label, so a crash in between leaves a
  reclaimable lease instead of a label nobody owns;
* the lease expires after ``ttl_s`` seconds unless the lane heartbeat renews
  it;
* an expired lease is reclaimed only when the run it points at is gone -- a
  run that still lives keeps its issue ``in-progress``;
* a label that was written before the upgrade holds no lease: it is reclaimed
  with the original grace rule (the label must be older than ``grace_s`` and
  no lane may hold the issue).

The module holds the decisions only: it knows nothing about GitHub labels,
lanes or the CLI, so the wiring stays with the caller.  Every labelled issue
gets exactly one decision with a reason, so the caller can log why a label was
kept.

Timestamp convention: epochs are plain seconds, every naive timestamp is read
as UTC.
"""
from __future__ import annotations

import calendar
import time
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_GRACE_S = 900.0

# The reasons a label is kept.  They are stable strings: the caller logs them.
KEEP_INVALID_ISSUE = "invalid_issue"
KEEP_LEASE_ALIVE = "lease_alive"
KEEP_RUN_ALIVE = "run_alive"
KEEP_HELD = "held"
KEEP_NO_TIMESTAMP = "no_timestamp"
KEEP_CLOCK_ROLLBACK = "clock_rollback"
KEEP_WITHIN_GRACE = "within_grace"

# The reasons a label is reclaimed.
RECLAIM_LEASE_EXPIRED = "lease_expired"
RECLAIM_NO_LEASE = "no_lease"
RECLAIM_REASONS = frozenset({RECLAIM_LEASE_EXPIRED, RECLAIM_NO_LEASE})


def epoch(value: Any) -> Optional[float]:
    """The epoch seconds of a timestamp or an epoch; None when it cannot be read.

    A GitHub ``updatedAt`` (``2026-11-02T14:25:33Z``) and a plain epoch are
    both accepted; a naive timestamp is read as UTC.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    for pattern in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return float(calendar.timegm(time.strptime(text, pattern)))
        except ValueError:
            continue
    return None


def issue_of(row: Any) -> Any:
    """The issue number of a labelled row (the row itself when it is a number)."""
    if isinstance(row, dict):
        for key in ("number", "issue", "n"):
            if key in row:
                return row[key]
        return None
    return row


def updated_of(row: Any) -> Any:
    """The ``updatedAt`` of a labelled row (None when the row carries none)."""
    if not isinstance(row, dict):
        return None
    for key in ("updatedAt", "updated_at", "updated"):
        if key in row:
            return row[key]
    return None


def lease_run(record: Any) -> str:
    """The run id of a lease record ("" while the dispatch is still in flight)."""
    if not isinstance(record, dict):
        return ""
    for key in ("run_id", "run"):
        if key in record:
            value = record.get(key)
            return "" if value is None else str(value).strip()
    return ""


def lease_expiry(record: Any) -> Optional[float]:
    """The expiry of a lease record (None when the record carries no usable one)."""
    if not isinstance(record, dict):
        return None
    return epoch(record.get("expires_at"))


def _lease_of(leases: Any, key: str) -> Optional[Dict[str, Any]]:
    """The lease record of an issue (None when the issue holds no lease)."""
    if not isinstance(leases, dict):
        return None
    record = leases.get(key)
    if record is None and key.isdigit():
        record = leases.get(int(key))
    return record if isinstance(record, dict) else None


def decide(
    labelled: Iterable[Any],
    leases: Any = None,
    held: Iterable[Any] = (),
    running_runs: Iterable[Any] = (),
    now: Any = None,
    grace_s: Any = DEFAULT_GRACE_S,
) -> List[Dict[str, Any]]:
    """One decision per labelled issue: ``keep`` or ``reclaim``, with the reason.

    `labelled` are the issues that carry the `in-progress` label (rows with a
    ``number`` and an ``updatedAt``), `leases` the lease records keyed by the
    issue, `held` the issues a lane holds right now, `running_runs` the run ids
    that are still alive and `now` the current epoch (the wall clock when it is
    omitted).
    """
    moment = epoch(now) if now is not None else time.time()
    grace = epoch(grace_s)
    grace = DEFAULT_GRACE_S if grace is None else grace
    held_keys = {str(issue_of(item)) for item in (held or ())}
    alive = {str(run).strip() for run in (running_runs or ()) if str(run).strip()}

    decisions: List[Dict[str, Any]] = []
    for row in labelled or ():
        number = issue_of(row)
        key = str(number)
        record = _lease_of(leases, key)
        reason: Optional[str] = None
        if number is None:
            reason = KEEP_INVALID_ISSUE
        elif record is not None:
            expiry = lease_expiry(record)
            run = lease_run(record)
            if expiry is None:
                expiry = 0.0          # a lease without a usable deadline is over
            if moment < expiry:
                reason = KEEP_LEASE_ALIVE
            elif run and run in alive:
                reason = KEEP_RUN_ALIVE      # the run still lives: only the lease expired
            elif not run and key in held_keys:
                reason = KEEP_HELD            # the dispatch is still in flight
            else:
                reason = RECLAIM_LEASE_EXPIRED
        elif key in held_keys:
            reason = KEEP_HELD
        else:
            updated = epoch(updated_of(row))
            if updated is None:
                reason = KEEP_NO_TIMESTAMP
            elif moment < updated:
                reason = KEEP_CLOCK_ROLLBACK
            elif moment - updated < grace:
                reason = KEEP_WITHIN_GRACE
            else:
                reason = RECLAIM_NO_LEASE
        decisions.append({
            "issue": number,
            "action": "reclaim" if reason in RECLAIM_REASONS else "keep",
            "reason": reason or RECLAIM_NO_LEASE,
        })
    return decisions


def reclaimable(
    labelled: Iterable[Any],
    leases: Any = None,
    held: Iterable[Any] = (),
    running_runs: Iterable[Any] = (),
    now: Any = None,
    grace_s: Any = DEFAULT_GRACE_S,
) -> List[Any]:
    """The issues whose `in-progress` label may be taken back."""
    return [
        decision["issue"]
        for decision in decide(labelled, leases, held, running_runs, now, grace_s)
        if decision["action"] == "reclaim"
    ]
