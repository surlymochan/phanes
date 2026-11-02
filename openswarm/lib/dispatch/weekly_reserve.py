"""Weekly quota reserve throttling helpers. Pure functions; no I/O."""

from __future__ import annotations

import datetime
import math
from typing import Any, Dict, Mapping, Optional


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _epoch(value: Any) -> Optional[float]:
    number = _num(value)
    if number is not None:
        return number
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (ValueError, OverflowError):
        return None


def weekly_throttle(window: Mapping[str, Any], now: Any, cfg: Mapping[str, Any]) -> float:
    """Return reserve multiplier from remaining percentage per remaining day.

    Missing/invalid data disables throttling (1). At/below ``floor_pct`` the
    lane stops (0); otherwise the daily burn rate is scaled against
    ``target_pct_per_day`` and clamped to 0.25..1.
    """
    if not isinstance(window, Mapping) or not isinstance(cfg, Mapping):
        return 1.0
    pct = _num(window.get("weekly_pct"))
    reset = _epoch(window.get("weekly_resets_at"))
    now_epoch = _epoch(now)
    floor = _num(cfg.get("floor_pct"))
    target = _num(cfg.get("target_pct_per_day"))
    if None in (pct, reset, now_epoch, floor, target) or target <= 0:
        return 1.0
    if pct <= floor:
        return 0.0
    days = (reset - now_epoch) / 86400
    if days <= 0:
        return 1.0
    return max(0.25, min(1.0, pct / days / target))


def apply_throttle(limits: Mapping[str, Any], factor: float) -> Dict[str, Any]:
    """Scale concurrency/review down, preserving disabled lanes.

    A positive ``min_concurrency`` (default 1) is a lower bound even when it
    exceeds the configured base; factor zero always stops. Apply burn first,
    then this throttle; existing max_* caps are preserved and should be applied
    again after both factors if they cap the combined result.
    """
    out = dict(limits)
    multiplier = _num(factor)
    if multiplier is None:
        return out
    minimum = _num(limits.get("min_concurrency", 1))
    minimum = max(0, int(minimum)) if minimum is not None else 1
    for key in ("concurrency", "review"):
        base = _num(limits.get(key))
        if base is None:
            continue
        if multiplier <= 0 or base <= 0:
            out[key] = 0
        else:
            out[key] = max(minimum, math.floor(base * multiplier))
    return out
