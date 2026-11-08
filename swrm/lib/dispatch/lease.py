"""In-progress lease bookkeeping for the dispatch loop (Refs #1400).

The dispatch loop labels an issue `in-progress` before the run is really alive.
When the executor dies in that window the label stays behind and the issue is
never scheduled again (a "ghost" in-progress).  The lease store bounds that
window: a lease is taken when the issue is dispatched, it expires after
``ttl_s`` seconds unless the holder renews it, and an expired lease is only
reclaimable when the run it points at is not running any more.

The module is deliberately free of side effects beyond one JSON file: it knows
nothing about GitHub labels, lanes or the CLI, so the wiring stays with the
caller (#1364).

The file is written atomically (temporary file + ``os.replace``), so a crashed
writer can never leave a half-written lease file behind.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_TTL_S = 600.0
LEASES_FILENAME = "leases.json"
SCHEMA_VERSION = 1


def _number(value: Any, name: str) -> float:
    """A finite float, or a ValueError: the lease clock must never be NaN."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number


def _issue_key(issue: Any) -> str:
    """The JSON key of an issue: its number as text."""
    if isinstance(issue, bool) or not isinstance(issue, (int, str)):
        raise ValueError(f"issue must be an int or a str, got {issue!r}")
    text = str(issue).strip()
    if not text:
        raise ValueError("issue must not be empty")
    return text


def _issue_value(key: str) -> Any:
    """The issue as the caller passed it: a number when the key looks like one."""
    try:
        return int(key)
    except ValueError:
        return key


def _run_key(run_id: Any) -> str:
    """The text form of a run id; run ids are opaque."""
    if isinstance(run_id, bool) or not isinstance(run_id, (int, str)):
        raise ValueError(f"run_id must be an int or a str, got {run_id!r}")
    text = str(run_id).strip()
    if not text:
        raise ValueError("run_id must not be empty")
    return text


def _run_text(value: Any) -> str:
    """The text form of a stored run id; a missing one stays empty."""
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _issue_sort_key(value: Any) -> Any:
    """Sort issues by number, keeping non numeric keys at the end."""
    if isinstance(value, int):
        return (0, value, "")
    return (1, 0, str(value))


class LeaseStore:
    """The leases of the issues that are currently dispatched.

    One JSON file (``leases.json``) inside ``directory`` holds every lease::

        {"version": 1,
         "leases": {"1400": {"issue": 1400, "run_id": "run-1", "lane": "M12",
                             "granted_at": 1000.0, "expires_at": 1600.0,
                             "ttl_s": 600.0}}}

    The clock is always passed in by the caller (``now``), so the behaviour is
    reproducible offline and independent of the wall clock of the host.
    """

    def __init__(self, directory: Any, filename: str = LEASES_FILENAME) -> None:
        self.dir = Path(directory)
        self.path = self.dir / filename

    # ------------------------------------------------------------------ read

    def load(self) -> Dict[str, Dict[str, Any]]:
        """Every lease record, keyed by the issue key (empty when unreadable)."""
        try:
            with open(self.path, encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        leases = payload.get("leases")
        if not isinstance(leases, dict):
            # Tolerate a bare mapping of records (hand edited or older file).
            leases = payload if all(isinstance(row, dict) for row in payload.values()) else {}
        return {str(key): row for key, row in leases.items() if isinstance(row, dict)}

    def get(self, issue: Any) -> Optional[Dict[str, Any]]:
        """The lease of `issue`, or None when the issue holds no lease."""
        record = self.load().get(_issue_key(issue))
        return dict(record) if record else None

    def expired(
        self, now: Any, running_runs: Optional[Iterable[Any]] = None, grace_s: Any = 0
    ) -> List[Any]:
        """The issues whose lease expired and whose run is not running any more.

        A lease that expired while its run is still alive is *not* reclaimable:
        the run may still make progress, so the issue must stay `in-progress`.
        The lease is over from its ``expires_at`` on (``now == expires_at`` is
        already expired).  The returned issues are the ones the caller may
        relabel and dispatch again; the leases themselves are dropped with
        :meth:`release`.
        """
        moment = _number(now, "now")
        grace = _number(grace_s, "grace_s")
        if isinstance(running_runs, (str, bytes)):
            running_runs = [running_runs]
        alive = set()
        for run_id in running_runs or ():
            alive.add(_run_text(run_id))
        reclaimable = []
        for key, record in self.load().items():
            expires_at = record.get("expires_at")
            if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
                continue
            if moment < float(expires_at) + grace:
                continue
            if _run_text(record.get("run_id")) in alive:
                continue
            reclaimable.append(_issue_value(key))
        return sorted(reclaimable, key=_issue_sort_key)

    def shift(self, paused_s: Any, now: Any) -> int:
        """Extend leases that were still live when a pause began.

        ``now`` is the resume time. Leases already expired before the pause
        are deliberately left expired. The amount applied is accumulated in
        each lease's ``shifted_s`` field.
        """
        paused = _number(paused_s, "paused_s")
        moment = _number(now, "now")
        if paused <= 0:
            return 0
        records = self.load()
        cutoff = moment - paused
        shifted = 0
        for record in records.values():
            expiry = record.get("expires_at")
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
                continue
            if float(expiry) <= cutoff:
                continue
            record["expires_at"] = float(expiry) + paused
            previous = record.get("shifted_s", 0)
            if isinstance(previous, bool) or not isinstance(previous, (int, float)):
                previous = 0
            record["shifted_s"] = float(previous) + paused
            shifted += 1
        if shifted:
            self._write(records)
        return shifted

    # ----------------------------------------------------------------- write

    def grant(self, issue: Any, run_id: Any, lane: Any, ttl_s: Any, now: Any) -> Dict[str, Any]:
        """Take the lease of `issue` for `run_id` on `lane` until ``now + ttl_s``.

        A previous lease of the same issue is replaced: taking a lease over is
        the caller's decision (the reclaim path of :meth:`expired` is the
        intended one).
        """
        key = _issue_key(issue)
        run = _run_key(run_id)
        ttl = _number(ttl_s, "ttl_s")
        if ttl <= 0:
            raise ValueError(f"ttl_s must be positive, got {ttl_s!r}")
        moment = _number(now, "now")
        records = self.load()
        record = {
            "issue": _issue_value(key),
            "run_id": run,
            "lane": None if lane is None else str(lane),
            "granted_at": moment,
            "expires_at": moment + ttl,
            "ttl_s": ttl,
        }
        records[key] = record
        self._write(records)
        return dict(record)

    def renew(self, issue: Any, run_id: Any, now: Any) -> Optional[Dict[str, Any]]:
        """Push the deadline of the lease of `issue` away, keeping its length.

        Only the run that holds the lease may renew it: a renewal with another
        ``run_id`` is rejected and returns None, the deadline of the holder
        stays untouched.  None is also returned when the issue holds no lease.
        """
        key = _issue_key(issue)
        run = _run_key(run_id)
        moment = _number(now, "now")
        records = self.load()
        record = records.get(key)
        if not isinstance(record, dict):
            return None
        if _run_text(record.get("run_id")) != run:
            return None
        ttl = record.get("ttl_s")
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
            ttl = DEFAULT_TTL_S
        renewed = dict(record)
        renewed["renewed_at"] = moment
        renewed["expires_at"] = moment + float(ttl)
        records[key] = renewed
        self._write(records)
        return dict(renewed)

    def release(self, issue: Any) -> bool:
        """Drop the lease of `issue`; True when a lease was actually removed."""
        key = _issue_key(issue)
        records = self.load()
        if key not in records:
            return False
        records.pop(key)
        self._write(records)
        return True

    def _write(self, records: Dict[str, Dict[str, Any]]) -> None:
        """Write the lease file atomically (temporary file + ``os.replace``)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": SCHEMA_VERSION, "leases": records}
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.dir),
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        temporary = handle.name
        try:
            with handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def shift_from_pauses(
    store: LeaseStore, pauses_jsonl: Any, now: Any, state_file: Any
) -> int:
    """Apply each valid, not-yet-applied pause record exactly once."""
    moment = _number(now, "now")
    state_path = Path(state_file)
    try:
        with open(state_path, encoding="utf-8") as stream:
            state = json.load(stream)
        applied = set(state.get("resumed", [])) if isinstance(state, dict) else set()
    except (OSError, ValueError, TypeError):
        applied = set()

    pending = []
    try:
        stream = open(pauses_jsonl, encoding="utf-8")
    except OSError:
        return 0
    with stream:
        for line in stream:
            try:
                row = json.loads(line)
                resumed = _number(row["resumed"], "resumed")
                gap = _number(row["gap_s"], "gap_s")
                _number(row["started"], "started")
                if resumed > moment or gap <= 0 or resumed in applied:
                    continue
                pending.append((resumed, gap))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue

    for resumed, gap in pending:
        store.shift(gap, resumed)
        applied.add(resumed)
    if pending:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_name(state_path.name + ".tmp")
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump({"resumed": sorted(applied)}, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path)
    return len(pending)
