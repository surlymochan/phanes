"""Quota-window burn factor for dispatch.

When a backend's quota window is about to reset, unused capacity is lost, so
dispatch may run harder during the last N hours before the reset. This module
is pure: it computes the factor and applies it to limits, and does no I/O.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

SCALED_KEYS = ("concurrency", "review")
CAP_KEYS = {"concurrency": "max_concurrency", "review": "max_review"}


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def burn_factor(
    backend: str,
    now: float,
    window: Optional[Mapping[str, Any]],
    cfg: Optional[Mapping[str, Any]],
) -> float:
    """Return the configured multiplier in the hours before ``resets_at``.

    The window includes its reset instant; once the reset has passed the caller
    should supply the next window, and this function returns one for old data.
    """
    if not isinstance(cfg, Mapping) or not isinstance(window, Mapping):
        return 1
    backend_cfg = cfg.get(backend)
    if not isinstance(backend_cfg, Mapping):
        return 1

    hours = _num(backend_cfg.get("burn_before_reset_h"))
    factor = _num(backend_cfg.get("factor"))
    resets_at = _num(window.get("resets_at"))
    now_epoch = _num(now)
    if hours is None or factor is None or resets_at is None or now_epoch is None:
        return 1
    if hours <= 0 or factor <= 1:
        return 1

    remaining = resets_at - now_epoch
    if 0 <= remaining <= hours * 3600:
        return factor
    return 1


def apply(limits: Mapping[str, Any], factor: float) -> Dict[str, Any]:
    """Scale concurrency and review quotas, round them, and apply optional caps.

    Other values are preserved and the input mapping is never mutated. Caps
    are provided as ``max_concurrency`` and ``max_review`` in ``limits``.
    """
    out: Dict[str, Any] = dict(limits)
    multiplier = _num(factor)
    if multiplier is None or multiplier <= 1:
        return out

    for key in SCALED_KEYS:
        base = _num(limits.get(key))
        if base is None:
            continue
        scaled = int(round(base * multiplier))
        cap = _num(limits.get(CAP_KEYS[key]))
        if cap is not None:
            scaled = min(scaled, int(cap))
        out[key] = scaled
    return out
