"""Lease audit and lease health metrics for the lease based dispatch (Refs #1795, Epic #1785).

The lease system (P2-1/P2-2) gives every dispatched card a claim: a lease that
expires unless its holder renews it.  Two failure modes can only be seen from
the outside, and neither of them is visible in the lease file alone:

* two claims on the same card at the same time (double claim) and two runs
  working the same card at the same time (double execution, or two runs
  writing the result back for one card).  Both break the "one card, one lane"
  rule the whole fleet relies on;
* a lease that expired and was never reclaimed (a ghost lease): the card stays
  invisible to the pool while nobody works on it.

This module is the read-only half of the story.  It looks at the lease records
and at the run records and says what is wrong.  It never writes a lease, never
releases one and never relabels a card: the reclaim path of the dispatcher
stays the only writer, so the audit can never become the second writer of the
lease file.

The audit produces two things:

``alerts``
    structured findings -- kind, severity, fingerprint, summary, detail -- that
    a control plane can patrol on.  Every finding carries a stable
    ``fingerprint``: the same anomaly seen twice is the same fingerprint, so
    the caller can report it once instead of every round;
``metrics``
    the health numbers: how many ghost leases there are *right now*, how long
    the cards waited for their lease, and the same numbers over a rolling 24
    hour window.

The rolling window is computed from the audit state -- the ledger of the
alerts that were seen and of the grants that were observed -- and not from
the lease file.  The lease file only holds the leases that are still there:
counting it would miss every anomaly that was already fixed, and it would
pass the historical stock of the file for the state of the fleet right now.
The window counts the anomalies that *appeared* inside the window; the live
snapshot counts the ones that are still there.  Both numbers are reported, and
they are never the same number.

Usage::

    result = lease_audit.audit(leases, runs, now)
    state = lease_audit.apply_round(state, result, now)
    report = lease_audit.health_report(result, state, now)

The module also holds the readers of the stores (``read_leases``,
``read_lane_state``, ``read_runs``), so the periodic tool and the health
report of the fleet read the very same files: one convention, one number.

Standard library only, Python 3.9+.
"""
from __future__ import annotations

import datetime
import json
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

__all__ = [
    "ALERT_DUAL_CLAIM",
    "ALERT_DUAL_EXECUTION",
    "ALERT_EXPIRED_LEASE",
    "ACTIVE_STATES",
    "SCHEMA",
    "STATE_VERSION",
    "WINDOW_S",
    "apply_round",
    "audit",
    "health_report",
    "issue_key",
    "lease_store_files",
    "normalize_lease",
    "normalize_run",
    "read_json",
    "read_lane_state",
    "read_leases",
    "read_runs",
    "window_metrics",
]

SCHEMA = 1
STATE_VERSION = 1

# The rolling window of the health numbers: 24 hours.
WINDOW_S = 86400.0

# The kinds of finding the audit reports.
ALERT_DUAL_CLAIM = "dual_claim"
ALERT_DUAL_EXECUTION = "dual_execution"
ALERT_EXPIRED_LEASE = "expired_lease"

# The severity of a finding.  A double execution is the worst one: two lanes
# write the same card at the same time.
SEVERITY = {
    ALERT_DUAL_CLAIM: "high",
    ALERT_DUAL_EXECUTION: "critical",
    ALERT_EXPIRED_LEASE: "medium",
}

# The run states that mean "this run is still working on the card".
ACTIVE_STATES = frozenset({
    "running", "queued", "routing", "starting", "active", "reviewing",
    "in-progress", "in_progress", "waiting", "claimed", "finalizing", "verifying",
})

# The run states that mean "this run is over".
TERMINAL_STATES = frozenset({
    "finished", "failed", "done", "unroutable", "dry-run", "dry_run",
    "budget_rejected", "cancelled", "canceled", "stopped", "error", "timeout",
    "aborted", "dead", "merged",
})

# The keys a lease record may carry for the moment the card asked for the lease.
REQUEST_KEYS = ("requested_at", "queued_at", "waited_since", "submitted_at")

# The keys a run record may carry for the moment the card asked for the lease.
RUN_REQUEST_KEYS = ("requested_at", "queued_at", "submitted_at", "created_at")


# ------------------------------------------------------------------ helpers

def _number(value: Any) -> Optional[float]:
    """A finite number, or None.  A clock that reads NaN is no clock at all."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: Any) -> str:
    """The text form of a value; a missing one stays empty."""
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _first(record: Dict[str, Any], keys: Sequence[str]) -> Any:
    """The first of ``keys`` that the record carries with a value."""
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def iso(moment: Any) -> Optional[str]:
    """The ISO-8601 UTC stamp of a moment, or None when there is no moment."""
    stamp = _number(moment)
    if stamp is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def issue_key(value: Any) -> str:
    """The key of a card: its number as text (``1795``), or its text (``PR12``).

    The lease store keys its records by the text of the issue, so the audit
    uses the same key: a lease of card 1795 and a run of card "1795" belong
    to the same card.
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def issue_number(key: Any) -> Optional[int]:
    """The number of a card key, or None when the key is not a number."""
    text = _text(key)
    try:
        return int(text)
    except ValueError:
        return None


# ------------------------------------------------------------------- readers

def read_json(path: Any) -> Any:
    """The JSON of a file, or None when it can not be read."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def lease_store_files(path: Any) -> List[str]:
    """The lease stores of a file or of a directory of stores.

    A directory holds one store per host (``leases.json``) or one store per
    lane (``leases-<lane>.json``); every one of them is a writer of its own,
    so every one of them is read.
    """
    if not path:
        return []
    if os.path.isfile(path):
        return [str(path)]
    if os.path.isdir(path):
        names = []
        for name in os.listdir(path):
            if not (name.startswith("leases") and name.endswith(".json")):
                continue
            store = os.path.join(path, name)
            if os.path.isfile(store):
                names.append(store)
        return sorted(names)
    return []


def read_leases(path: Any) -> List[Dict[str, Any]]:
    """Every lease record of every store, each one tagged with its source.

    The source is the store the record came from: a card that holds two
    leases is only a double claim when the two stores are told apart.
    """
    records: List[Dict[str, Any]] = []
    for store in lease_store_files(path):
        payload = read_json(store)
        leases = payload.get("leases") if isinstance(payload, dict) else None
        if not isinstance(leases, dict):
            leases = payload if isinstance(payload, dict) else {}
        for key, record in leases.items():
            if not isinstance(record, dict):
                continue
            row = dict(record)
            if not row.get("issue"):
                row["issue"] = key
            row["source"] = str(store)
            records.append(row)
    return records


def read_lane_state(path: Any) -> List[Dict[str, Any]]:
    """The run records of a lane state (``lane -> {issue, run}``).

    A lane that holds a card is working on it: the dispatcher only drops the
    entry when the run is over.

    Only the entries that name a card are lane entries.  A file that is not a
    lane state (the probe state of the home, for one) carries other records
    and must not be read as lanes.
    """
    payload = read_json(path)
    if not isinstance(payload, dict):
        return []
    records: List[Dict[str, Any]] = []
    for lane, record in payload.items():
        if str(lane).startswith("_") or not isinstance(record, dict):
            continue
        if not _first(record, ("issue", "card", "issue_number")):
            continue
        row = dict(record)
        row.setdefault("lane", lane)
        records.append(row)
    return records


def read_runs(path: Any) -> List[Dict[str, Any]]:
    """The run records of a run directory or of a file with run records."""
    if not path:
        return []
    if os.path.isdir(path):
        records: List[Dict[str, Any]] = []
        for name in sorted(os.listdir(path)):
            run_dir = os.path.join(path, name)
            if not os.path.isdir(run_dir):
                continue
            payload = read_json(os.path.join(run_dir, "status.json"))
            if not isinstance(payload, dict):
                continue
            row = dict(payload)
            row.setdefault("run_id", name)
            records.append(row)
        return records
    payload = read_json(path)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        records = []
        for key, record in payload.items():
            if str(key).startswith("_") or not isinstance(record, dict):
                continue
            row = dict(record)
            row.setdefault("run_id", key)
            records.append(row)
        return records
    return []


# ------------------------------------------------------------- normalisation

def normalize_lease(key: Any, record: Any, source: Any = None) -> Optional[Dict[str, Any]]:
    """Normalise one lease record into the shape the audit works with.

    ``key`` is the key the lease is filed under (the card it belongs to); a
    record that carries its own ``issue`` wins over the key.  A record without
    a usable expiry cannot be judged (it is neither live nor expired), but it
    is still returned: the caller counts it as unreadable.
    """
    if not isinstance(record, dict):
        return None
    issue = issue_key(_first(record, ("issue", "card", "issue_number")) or key)
    if not issue:
        issue = issue_key(key)
    return {
        "issue": issue,
        "run_id": _text(_first(record, ("run_id", "run"))),
        "lane": _text(_first(record, ("lane", "lane_id"))) or None,
        "granted_at": _number(_first(record, ("granted_at", "granted"))),
        "expires_at": _number(_first(record, ("expires_at", "expires"))),
        "ttl_s": _number(_first(record, ("ttl_s", "ttl"))),
        "requested_at": _number(_first(record, REQUEST_KEYS)),
        "source": _text(record.get("source")) or _text(source) or None,
    }


def normalize_run(record: Any, lane: Any = None) -> Optional[Dict[str, Any]]:
    """Normalise one run record into the shape the audit works with.

    The shapes in the wild differ: the lane state of the dispatcher files the
    card under ``issue`` and the run under ``run``, a run status files the
    state under ``state`` and the card under ``issue`` or ``card``.  Every
    alias the audit knows is accepted here, once.
    """
    if not isinstance(record, dict):
        return None
    run_id = _text(_first(record, ("run_id", "run", "id")))
    issue = issue_key(_first(record, ("issue", "card", "issue_number")))
    state = _text(_first(record, ("state", "status"))).lower()
    started = _number(_first(record, ("started", "started_at")))
    finished = _number(_first(record, ("finished", "finished_at", "ended_at")))
    requested = _number(_first(record, RUN_REQUEST_KEYS))
    return {
        "run_id": run_id,
        "issue": issue,
        "lane": _text(_first(record, ("lane", "lane_id"))) or (_text(lane) or None),
        "state": state,
        "active": _is_active(state, finished),
        "alive": _is_alive(state, finished),
        "started": started,
        "finished": finished,
        "requested_at": requested,
        "commit": _text(_first(record, ("commit", "sha"))) or None,
        "branch": _text(_first(record, ("branch", "ref"))) or None,
    }


def _is_active(state: str, finished: Optional[float]) -> bool:
    """Does the run still occupy the card?

    A record without a state (the lane state of the dispatcher files the card
    under ``issue`` and the run under ``run``) still occupies the card: the
    dispatcher only drops the entry when the run is over.
    """
    if state in TERMINAL_STATES:
        return False
    if state in ACTIVE_STATES:
        return True
    if finished is not None:
        return False
    return True


def _is_alive(state: str, finished: Optional[float]) -> bool:
    """Does the record prove that the run still works?

    Only a record that says so proves it: a run whose state is ``running`` (or
    ``queued``) is alive.  A lane state entry does not: it says which card the
    lane holds, not that the run behind it still runs.  An expired lease whose
    run is alive must not be reclaimed, so this is the difference between a
    ghost lease and a lease that is only late.
    """
    if finished is not None:
        return False
    return state in ACTIVE_STATES


# ------------------------------------------------------------------- alerts

def _fingerprint(kind: str, issue: str, *parts: Any) -> str:
    """The stable identity of a finding: the same anomaly is the same string."""
    tail = "|".join(_text(part) for part in parts if _text(part))
    return "{}:{}:{}".format(kind, issue, tail) if tail else "{}:{}".format(kind, issue)


def _alert(kind: str, issue: str, fingerprint: str, summary: str,
           detail: Dict[str, Any], severity: str, moment: Optional[float]) -> Dict[str, Any]:
    """One structured finding for the control plane."""
    return {
        "kind": kind,
        "severity": severity,
        "issue": issue,
        "issue_number": issue_number(issue),
        "fingerprint": fingerprint,
        "summary": summary,
        "detail": detail,
        "at": iso(moment),
        "at_epoch": moment,
    }


def _lease_detail(lease: Dict[str, Any]) -> Dict[str, Any]:
    """The part of a lease record a finding carries."""
    return {
        "run_id": lease["run_id"],
        "lane": lease["lane"],
        "granted_at": lease["granted_at"],
        "expires_at": lease["expires_at"],
        "source": lease["source"],
    }


def _overlaps(first: Dict[str, Any], second: Dict[str, Any]) -> bool:
    """Do two leases live at the same time?

    The windows are half open: a lease that ends exactly when the next one
    starts is not a double claim -- the card was handed out again right after
    the first lease ran out, which is the normal path of the reclaim.

    A lease without a grant or without an expiry has no window, so it can not
    be shown to overlap with anything.
    """
    a_start, a_end = first["granted_at"], first["expires_at"]
    b_start, b_end = second["granted_at"], second["expires_at"]
    if None in (a_start, a_end, b_start, b_end):
        return False
    return a_start < b_end and b_start < a_end


# -------------------------------------------------------------------- audit

def audit(leases: Any, runs: Any, now: Any, grace_s: Any = 0.0) -> Dict[str, Any]:
    """Audit the leases against the runs and report what is wrong.

    ``leases`` is a list of lease records (the raw records, already merged
    from every lease store the caller knows); ``runs`` is a list of run
    records.  Both are normalised here, so the caller may pass the records in
    any of the shapes that are in the wild.

    Three findings are reported:

    ``dual_claim``
        two leases of the same card whose windows overlap: the card was
        claimed twice;
    ``dual_execution``
        two active runs of the same card (or two runs that wrote the same
        result back for one card), or a live lease whose holder is not the run
        that works on the card;
    ``expired_lease``
        a lease that expired and was never reclaimed: a ghost lease.  A lease
        that expired while its run is still alive is *not* a ghost: the run may
        still make progress, so the card must not be handed out again.

    The audit only reads.  It returns the alerts and the metrics of the
    moment; it changes no lease and no card.
    """
    moment = _number(now)
    grace = _number(grace_s) or 0.0
    lease_rows = _leases(leases)
    run_rows = [row for row in (_normalize_run(record) for record in _rows(runs)) if row]

    alerts: List[Dict[str, Any]] = []
    metrics, grants = _metrics(lease_rows, run_rows, alerts, moment, grace)
    return {"alerts": alerts, "metrics": metrics, "grants": grants, "now": moment}


def _rows(value: Any) -> List[Any]:
    """The records of a list, a mapping of records or a single record."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _leases(leases: Any) -> List[Dict[str, Any]]:
    """The normalised lease records of a mapping or a list of records."""
    rows: List[Dict[str, Any]] = []
    if isinstance(leases, dict):
        payload = leases.get("leases") if isinstance(leases.get("leases"), dict) else leases
        for key, record in payload.items():
            if not isinstance(record, dict):
                continue
            row = normalize_lease(key, record, leases.get("source") if isinstance(leases.get("source"), str) else None)
            if row is not None:
                rows.append(row)
        return rows
    for record in _rows(leases):
        if not isinstance(record, dict):
            continue
        row = normalize_lease(None, record)
        if row is not None:
            rows.append(row)
    return rows


def _normalize_run(record: Any) -> Optional[Dict[str, Any]]:
    """Normalise a run record; a record without a run id is not a run."""
    row = normalize_run(record)
    if row is None or not row["run_id"]:
        return None
    return row


def _metrics(lease_rows: List[Dict[str, Any]], run_rows: List[Dict[str, Any]],
             alerts: List[Dict[str, Any]], moment: Optional[float],
             grace: float) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Audit the leases and the runs, fill the alerts and return the metrics.

    The second value is the list of the grants: every lease with the wait it
    had, so the caller can keep them as the samples of the wait durations.
    """
    active_runs = [row for row in run_rows if row["active"]]
    active_by_issue: Dict[str, List[Dict[str, Any]]] = {}
    for row in active_runs:
        if row["issue"]:
            active_by_issue.setdefault(row["issue"], []).append(row)

    _detect_dual_claim(lease_rows, alerts, moment)
    _detect_dual_execution(lease_rows, run_rows, active_by_issue, alerts, moment)
    _detect_ghost_leases(lease_rows, run_rows, alerts, moment, grace)

    expired = 0
    live = 0
    ghosts = 0
    held = 0
    alive_ids = {row["run_id"] for row in run_rows if row["alive"] and row["run_id"]}
    waits: List[float] = []
    unknown_waits = 0
    grants: List[Dict[str, Any]] = []
    for lease in lease_rows:
        expires_at = lease["expires_at"]
        if expires_at is not None and moment is not None:
            if moment >= expires_at + grace:
                expired += 1
                if lease["run_id"] and lease["run_id"] in alive_ids:
                    held += 1
                else:
                    ghosts += 1
            else:
                live += 1
        wait = _wait_of(lease, run_rows)
        if wait is None:
            unknown_waits += 1
        else:
            waits.append(wait)
        grants.append({"issue": lease["issue"], "run_id": lease["run_id"],
                       "lane": lease["lane"], "granted_at": lease["granted_at"],
                       "wait_s": wait})

    dual_claims = sum(1 for alert in alerts if alert["kind"] == ALERT_DUAL_CLAIM)
    dual_executions = sum(1 for alert in alerts if alert["kind"] == ALERT_DUAL_EXECUTION)
    return {
        "leases": len(lease_rows),
        "active_leases": live,
        "expired_leases": expired,
        "ghost_leases": ghosts,
        "expired_held_leases": held,
        "dual_claims": dual_claims,
        "dual_executions": dual_executions,
        "active_runs": len(active_runs),
        "alerts": len(alerts),
        "wait_avg_s": _average(waits),
        "wait_max_s": max(waits) if waits else None,
        "wait_min_s": min(waits) if waits else None,
        "wait_samples": len(waits),
        "wait_unknown": unknown_waits,
        "ok": not alerts,
    }, grants


def _detect_dual_claim(lease_rows: List[Dict[str, Any]], alerts: List[Dict[str, Any]],
                        moment: Optional[float]) -> None:
    """Two leases of one card that live at the same time: the card was claimed twice."""
    by_issue: Dict[str, List[Dict[str, Any]]] = {}
    for lease in lease_rows:
        if lease["issue"]:
            by_issue.setdefault(lease["issue"], []).append(lease)
    for issue in sorted(by_issue):
        rows = by_issue[issue]
        for index, first in enumerate(rows):
            for second in rows[index + 1:]:
                if first["run_id"] and first["run_id"] == second["run_id"]:
                    continue  # the same run holding the lease twice is a renewal
                if not _overlaps(first, second):
                    continue
                fingerprint = _fingerprint(ALERT_DUAL_CLAIM, issue,
                                           _sorted_pair(first["run_id"], second["run_id"]))
                alerts.append(_alert(
                    ALERT_DUAL_CLAIM, issue, fingerprint,
                    "card {} holds two leases at the same time: {} and {}".format(
                        issue, first["run_id"] or "?", second["run_id"] or "?"),
                    {"leases": [_lease_detail(first), _lease_detail(second)]},
                    SEVERITY[ALERT_DUAL_CLAIM], moment))


def _detect_dual_execution(lease_rows: List[Dict[str, Any]], run_rows: List[Dict[str, Any]],
                            active_by_issue: Dict[str, List[Dict[str, Any]]],
                            alerts: List[Dict[str, Any]], moment: Optional[float]) -> None:
    """Two runs of one card: they work it at the same time, or they write it back twice."""
    reported: Set[Tuple[str, frozenset]] = set()
    for issue in sorted(active_by_issue):
        rows = active_by_issue[issue]
        runs = sorted({row["run_id"] for row in rows if row["run_id"]})
        if len(runs) > 1:
            reported.add((issue, frozenset(runs)))
            alerts.append(_alert(
                ALERT_DUAL_EXECUTION, issue,
                _fingerprint(ALERT_DUAL_EXECUTION, issue, "concurrent", *runs),
                "card {} is worked by {} runs at the same time: {}".format(issue, len(runs), ", ".join(runs)),
                {"reason": "concurrent", "runs": runs,
                 "lanes": sorted({row["lane"] for row in rows if row["lane"]})},
                SEVERITY[ALERT_DUAL_EXECUTION], moment))

    # Two runs that wrote the same card back: the same commit was written
    # twice.  The branch is not a signal: a card that is dispatched to the
    # same lane twice in a row carries the same branch and that is normal.
    by_issue: Dict[str, List[Dict[str, Any]]] = {}
    for row in run_rows:
        if row["issue"] and row["run_id"] and not row["active"]:
            by_issue.setdefault(row["issue"], []).append(row)
    for issue in sorted(by_issue):
        rows = by_issue[issue]
        for index, first in enumerate(rows):
            for second in rows[index + 1:]:
                if first["run_id"] == second["run_id"]:
                    continue
                commit = first["commit"]
                if not commit or commit != second["commit"]:
                    continue
                runs = sorted([first["run_id"], second["run_id"]])
                alerts.append(_alert(
                    ALERT_DUAL_EXECUTION, issue,
                    _fingerprint(ALERT_DUAL_EXECUTION, issue, "write_back", *runs),
                    "card {} was written back by two runs: {}".format(issue, ", ".join(runs)),
                    {"reason": "write_back", "runs": runs, "shared": "commit",
                     "value": commit,
                     "branches": sorted({row["branch"] for row in (first, second) if row["branch"]})},
                    SEVERITY[ALERT_DUAL_EXECUTION], moment))

    # A live lease whose holder is not the run that works on the card: the card
    # was handed out again while the first run may still be running.
    for lease in lease_rows:
        if not lease["issue"] or lease["expires_at"] is None or moment is None:
            continue
        if moment >= lease["expires_at"]:
            continue
        holders = active_by_issue.get(lease["issue"], [])
        others = sorted({row["run_id"] for row in holders
                         if row["run_id"] and row["run_id"] != lease["run_id"]})
        if not others:
            continue
        runs = sorted([lease["run_id"]] + others) if lease["run_id"] else others
        pair = (lease["issue"], frozenset(runs))
        if pair in reported:
            continue  # the two runs are already reported as working together
        reported.add(pair)
        alerts.append(_alert(
            ALERT_DUAL_EXECUTION, lease["issue"],
            _fingerprint(ALERT_DUAL_EXECUTION, lease["issue"], "lease_mismatch", *runs),
            "card {} holds the lease of {} but is worked by {}".format(
                lease["issue"], lease["run_id"] or "?", ", ".join(others)),
            {"reason": "lease_mismatch", "runs": runs, "lease": _lease_detail(lease)},
            "high", moment))


def _detect_ghost_leases(lease_rows: List[Dict[str, Any]], run_rows: List[Dict[str, Any]],
                         alerts: List[Dict[str, Any]], moment: Optional[float],
                         grace: float) -> None:
    """A lease that expired and was never reclaimed: the ghost lease of the card.

    A lease whose run is still alive is not a ghost: the run may still make
    progress, so the card must not be handed out again.  A lease whose run is
    known and over is a ghost for sure; a lease whose run is not known at all
    is a suspected one -- the card is stuck either way, but the audit says how
    sure it is.
    """
    alive = {row["run_id"] for row in run_rows if row["alive"] and row["run_id"]}
    over = {row["run_id"] for row in run_rows if row["run_id"] and not row["active"]}
    for lease in lease_rows:
        expires_at = lease["expires_at"]
        if expires_at is None or moment is None:
            continue
        if moment < expires_at + grace:
            continue
        if lease["run_id"] and lease["run_id"] in alive:
            continue  # the run is still alive: the lease is not reclaimable
        age = moment - expires_at
        confidence = "confirmed" if (lease["run_id"] and lease["run_id"] in over) else "suspected"
        alerts.append(_alert(
            ALERT_EXPIRED_LEASE, lease["issue"],
            _fingerprint(ALERT_EXPIRED_LEASE, lease["issue"], lease["run_id"]),
            "lease of card {} expired {}s ago and was not reclaimed".format(
                lease["issue"], int(age)),
            {"lease": _lease_detail(lease), "expired_for_s": age, "confidence": confidence},
            SEVERITY[ALERT_EXPIRED_LEASE], moment))


def _sorted_pair(first: str, second: str) -> str:
    """Two run ids in a stable order, so the fingerprint does not depend on it."""
    return "|".join(sorted([first, second]))


def _wait_of(lease: Dict[str, Any], run_rows: List[Dict[str, Any]]) -> Optional[float]:
    """How long the card waited for its lease.

    The wait runs from the moment the card asked for a lease to the moment the
    lease was granted.  The request moment is the one the lease record carries
    (``requested_at``); a lease record without it borrows the one of its run.
    A wait that cannot be told is not a wait of zero: it is no sample at all.
    """
    granted = lease["granted_at"]
    if granted is None:
        return None
    requested = lease["requested_at"]
    if requested is None:
        for row in run_rows:
            if row["run_id"] and row["run_id"] == lease["run_id"] and row["requested_at"] is not None:
                requested = row["requested_at"]
                break
    if requested is None:
        return None
    wait = granted - requested
    return wait if wait >= 0 else None


def _average(values: Sequence[float]) -> Optional[float]:
    """The average of the samples, or None when there is no sample."""
    if not values:
        return None
    return sum(values) / len(values)


# -------------------------------------------------------------------- state

def apply_round(state: Any, result: Any, now: Any,
                window_s: Any = WINDOW_S) -> Dict[str, Any]:
    """Fold one audit round into the audit state.

    The state holds three things: the ledger of the findings (with the moment
    they were first seen and how often they were seen since), the grants that
    were observed (the samples of the wait durations) and the rounds
    themselves (the numbers of every round, so the rolling window can say how
    bad it was at its worst).

    A finding that is in the ledger is not new: the caller reports it once.
    The ledger keeps the findings that are still there, and forgets the ones
    that are gone once they fell out of the window.
    """
    moment = _number(now)
    limit = _number(window_s) or WINDOW_S
    state = dict(state) if isinstance(state, dict) else {}
    ledger: Dict[str, Any] = dict(state.get("alerts") or {})
    rounds: List[Dict[str, Any]] = [row for row in (state.get("rounds") or []) if isinstance(row, dict)]
    waits: List[Dict[str, Any]] = [row for row in (state.get("waits") or []) if isinstance(row, dict)]

    alerts = [alert for alert in (result.get("alerts") if isinstance(result, dict) else None) or []
              if isinstance(alert, dict)]
    metrics = result.get("metrics") if isinstance(result, dict) else None
    metrics = metrics if isinstance(metrics, dict) else {}

    seen: Set[str] = set()
    new_alerts: List[Dict[str, Any]] = []
    for alert in alerts:
        fingerprint = _text(alert.get("fingerprint"))
        if not fingerprint:
            continue
        seen.add(fingerprint)
        entry = ledger.get(fingerprint)
        if isinstance(entry, dict):
            entry = dict(entry)
            entry["last_seen"] = moment
            entry["count"] = int(entry.get("count") or 0) + 1
            entry["severity"] = alert.get("severity", entry.get("severity"))
            entry["summary"] = alert.get("summary", entry.get("summary"))
        else:
            entry = {
                "kind": alert.get("kind"),
                "severity": alert.get("severity"),
                "issue": alert.get("issue"),
                "summary": alert.get("summary"),
                "first_seen": moment,
                "last_seen": moment,
                "count": 1,
            }
            new_alerts.append(alert)
        ledger[fingerprint] = entry

    # The findings that are gone: they stay in the ledger until they fall out
    # of the window, so the window still counts the round they happened in.
    start = None if moment is None else moment - limit
    for fingerprint in list(ledger):
        if fingerprint in seen:
            continue
        entry = ledger.get(fingerprint) or {}
        first_seen = _number(entry.get("first_seen"))
        if start is not None and first_seen is not None and first_seen < start:
            ledger.pop(fingerprint, None)

    if moment is not None:
        rounds.append({
            "at": moment,
            "ghost_leases": int(metrics.get("ghost_leases") or 0),
            "dual_claims": int(metrics.get("dual_claims") or 0),
            "dual_executions": int(metrics.get("dual_executions") or 0),
        })
        rounds = [row for row in rounds if _number(row.get("at")) is None or _number(row.get("at")) >= start]
        waits = [row for row in waits if _number(row.get("granted_at")) is None
                 or _number(row.get("granted_at")) >= start]

    # The grants of this round: a grant that is already known is not a new
    # sample, so the average does not count one card twice.
    known = {(row.get("issue"), row.get("run_id"), row.get("granted_at")) for row in waits}
    for lease in (result.get("grants") if isinstance(result, dict) else None) or []:
        if not isinstance(lease, dict):
            continue
        wait = lease.get("wait_s")
        if wait is None:
            continue
        key = (lease.get("issue"), lease.get("run_id"), lease.get("granted_at"))
        if key in known:
            continue
        known.add(key)
        waits.append({"issue": lease.get("issue"), "run_id": lease.get("run_id"),
                      "granted_at": lease.get("granted_at"), "wait_s": wait})

    return {
        "version": STATE_VERSION,
        "updated_at": moment,
        "alerts": ledger,
        "rounds": rounds,
        "waits": waits,
        "new_alerts": new_alerts,
    }


def window_metrics(state: Any, now: Any, window_s: Any = WINDOW_S) -> Dict[str, Any]:
    """The health numbers of the rolling window.

    The window counts the findings that *appeared* inside it and the grants
    that were observed inside it.  It never counts the findings that are
    older than the window as if they had just happened: a finding that is
    still there is counted by the live snapshot, not here.
    """
    moment = _number(now)
    limit = _number(window_s) or WINDOW_S
    start = None if moment is None else moment - limit
    state = state if isinstance(state, dict) else {}

    rounds = [row for row in (state.get("rounds") or []) if isinstance(row, dict)]
    rounds = [row for row in rounds if start is None or (_number(row.get("at")) or 0) >= start]
    ledger = state.get("alerts") if isinstance(state.get("alerts"), dict) else {}
    fresh = [entry for entry in ledger.values()
             if isinstance(entry, dict) and (start is None or (_number(entry.get("first_seen")) or 0) >= start)]
    waits = [row for row in (state.get("waits") or []) if isinstance(row, dict)]
    waits = [row for row in waits if start is None or (_number(row.get("granted_at")) or 0) >= start]

    samples = [float(row["wait_s"]) for row in waits
               if isinstance(row.get("wait_s"), (int, float)) and not isinstance(row.get("wait_s"), bool)]

    def count(kind: str) -> int:
        return sum(1 for entry in fresh if entry.get("kind") == kind)

    def worst(field: str) -> int:
        values = [int(row.get(field) or 0) for row in rounds]
        return max(values) if values else 0

    return {
        "window_s": limit,
        "from": iso(start),
        "to": iso(moment),
        "rounds": len(rounds),
        "ghost_leases": count(ALERT_EXPIRED_LEASE),
        "dual_claims": count(ALERT_DUAL_CLAIM),
        "dual_executions": count(ALERT_DUAL_EXECUTION),
        "max_ghost_leases": worst("ghost_leases"),
        "max_dual_claims": worst("dual_claims"),
        "max_dual_executions": worst("dual_executions"),
        "wait_avg_s": _average(samples),
        "wait_max_s": max(samples) if samples else None,
        "wait_min_s": min(samples) if samples else None,
        "wait_samples": len(samples),
    }


def health_report(result: Any, state: Any, now: Any, window_s: Any = WINDOW_S,
                  at_label: Any = None) -> Dict[str, Any]:
    """The health reading of the leases, in the shape of the health files.

    ``leases`` holds the numbers of the moment (the live snapshot) and
    ``window_24h`` the numbers of the rolling window.  The two are never
    mixed: the snapshot says what is there now, the window says what happened
    inside the window.
    """
    moment = _number(now)
    metrics = result.get("metrics") if isinstance(result, dict) else None
    metrics = metrics if isinstance(metrics, dict) else {}
    alerts = [alert for alert in (result.get("alerts") if isinstance(result, dict) else None) or []
              if isinstance(alert, dict)]
    window = window_metrics(state, moment, window_s)
    label = _text(at_label) or iso(moment) or ""
    return {
        "at": label,
        "at_iso": iso(moment),
        "at_epoch": moment,
        "updated_at": moment,
        "ok": not alerts,
        "leases": metrics,
        "window_24h": window,
        "alerts": alerts,
    }
