"""Pure decisions for stale in-progress labels (part of #1364).

Timestamp convention: every naive timestamp (``%F %T``, ``%FT%T``, without a
zone) is read as UTC; epochs are plain seconds.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import os
from typing import Any, List, Optional


def _epoch(value: Any) -> Optional[float]:
    """Parse an epoch or timestamp; every naive timestamp is interpreted as UTC."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if not isinstance(value, str):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except ValueError:
        pass
    for pattern in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
            number = parsed.timestamp()
            return number if math.isfinite(number) else None
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        number = parsed.timestamp()
        return number if math.isfinite(number) else None
    except (ValueError, OverflowError):
        return None


def _issue(row: Any) -> Any:
    if isinstance(row, dict):
        for key in ("issue", "number", "n"):
            if key in row:
                return row[key]
        return None
    return row


def _updated(row: dict) -> Any:
    for key in ("updatedAt", "updated_at", "updated"):
        if key in row:
            return row[key]
    return None


def decide(labelled, held, leases, resume_at, now, grace_s=900,
           resume_grace_s=600, debt=()):
    """Return a deterministic keep/strip decision for every labelled issue."""
    moment = _epoch(now)
    resumed = _epoch(resume_at)
    held_keys = {str(_issue(item)) for item in held}
    debt_keys = {str(_issue(item)) for item in debt}
    decisions: List[dict] = []
    for row in labelled:
        number = _issue(row)
        key = str(number)
        updated = _epoch(_updated(row)) if isinstance(row, dict) else None
        reason = None
        if number is None:
            reason = "invalid_issue"
        elif moment is None:
            reason = "clock_rollback"
        elif updated is None:
            reason = "no_timestamp"
        elif moment < updated:
            reason = "clock_rollback"
        elif resumed is not None and resumed > moment:
            reason = "clock_rollback"
        elif resumed is not None and moment - resumed < resume_grace_s:
            reason = "resume_grace"
        elif key in debt_keys:
            reason = "label_debt"
        elif key in held_keys:
            reason = "held"
        else:
            try:
                lease = leases.get(number) if leases is not None else None
                if lease is None and leases is not None:
                    lease = leases.get(key)
            except ValueError:
                reason = "invalid_issue"
            else:
                expiry = _epoch(lease.get("expires_at")) if isinstance(lease, dict) else None
                if expiry is not None and moment < expiry:
                    reason = "lease_alive"
                elif moment - updated <= grace_s:
                    reason = "within_grace"
        decisions.append({"issue": number, "action": "keep" if reason else "strip",
                          "reason": reason or "stale"})
    return decisions


def load_resume_at(path):
    """Read box-resume's first line: an epoch or a timestamp, naive values as UTC."""
    try:
        with open(os.fspath(path), encoding="utf-8") as stream:
            line = stream.readline().strip()
    except OSError:
        return None
    return _epoch(line)
