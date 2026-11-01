"""Weekly quota reserve throttle for dispatch.

This module is pure: it derives a dispatch multiplier from weekly quota
remaining and scales backend concurrency limits without mutating its input.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

SCALED_KEYS = ("concurrency", "review")


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def weekly_throttle(
    window: Optional[Mapping[str, Any]],
    now: float,
    cfg: Optional[Mapping[str, Any]],
) -> float:
    """Return a 0.25–1.0 throttle based on the weekly quota burn rate.

    ``weekly_pct`` and ``floor_pct`` use percentage points (0–100), while
    ``weekly_resets_at`` and ``now`` are Unix timestamps in seconds. Missing
    or invalid inputs leave dispatch unchanged.
    """
    if not isinstance(window, Mapping) or not isinstance(cfg, Mapping):
        return 1.0
    remaining_pct = _num(window.get("weekly_pct"))
    resets_at = _num(window.get("weekly_resets_at"))
    now_epoch = _num(now)
    floor_pct = _num(cfg.get("floor_pct"))
    target_per_day = _num(cfg.get("target_pct_per_day"))
    if None in (remaining_pct, resets_at, now_epoch, floor_pct, target_per_day):
        return 1.0
    if target_per_day <= 0:
        return 1.0
    if remaining_pct <= floor_pct:
        return 0.0

    days_left = (resets_at - now_epoch) / 86400
    if days_left <= 0:
        return 1.0
    sustainable_per_day = remaining_pct / days_left
    return min(1.0, max(0.25, sustainable_per_day / target_per_day))


def apply_throttle(limits: Mapping[str, Any], factor: float) -> Dict[str, Any]:
    """Scale concurrency/review by flooring, honoring a minimum of one.

    A zero factor stops both lanes. ``min_concurrency`` defaults to one and
    applies to both scaled values; unrelated keys and caps are preserved.
    """
    out: Dict[str, Any] = dict(limits)
    multiplier = _num(factor)
    if multiplier is None:
        return out
    if multiplier <= 0:
        for key in SCALED_KEYS:
            if _num(limits.get(key)) is not None:
                out[key] = 0
        return out

    minimum_value = _num(limits.get("min_concurrency"))
    minimum = max(0, int(minimum_value)) if minimum_value is not None else 1
    for key in SCALED_KEYS:
        base = _num(limits.get(key))
        if base is None:
            continue
        out[key] = max(minimum, math.floor(base * multiplier))
    return out
