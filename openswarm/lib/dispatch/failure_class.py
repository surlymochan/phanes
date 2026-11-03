"""Classify why a dispatched run failed and when its card may be requeued (Refs #1668).

The harvest used to treat every run that died without a commit as a card
failure: the issue was labelled ``lane-failed`` and a human had to re-add
``lane-ready``.  A run that dies because a backend budget window is spent (the
MiniMax 5 hour Token Plan window, for example) is not a card failure -- the
card never got a fair try, and the same card dies again while the window is
closed.

This module is pure: it classifies the failure and computes the moment the
card may return to the queue.  The wiring (labels, comments, state) stays with
the dispatcher (``lib/dispatch/engine.py``).

The reason classes are:

``budget``
    A budget/quota window was spent (``budget_exceeded`` and friends).  The
    card is held until the window recovers and then returns to the queue.
``quota``
    The backend refused the run for quota/rate-limit reasons
    (``rate_limit`` and friends).
``real``
    A genuine failure of the run (``exec_error``, ``stalled``, ...): the card
    keeps the normal failure flow.

The module also knows the window a held card waits for: a probe answer
(``{"exhausted": true|false}``) wins, a recovery time that has passed means the
window is back, and anything else is unknown -- an unknown window never puts a
card back into the queue.

Refs #1668.
"""

from __future__ import annotations

import datetime
import math
import re
from typing import Any, Optional

#: A budget/quota window was spent: the card never ran to the end.
REASON_BUDGET = "budget"

#: The backend refused the run for quota/rate-limit reasons.
REASON_QUOTA = "quota"

#: A genuine failure of the run (exec error, stall, protocol error, ...).
REASON_REAL = "real"

#: Error classes that mean "a budget window was spent", not "the card is broken".
BUDGET_CLASSES = frozenset({
    "budget_exceeded",
    "budget_exceeded_daily",
    "budget_exceeded_weekly",
    "budget_exceeded_monthly",
    "budget_exhausted",
})

#: Error classes that mean "the backend refused for quota reasons".
QUOTA_CLASSES = frozenset({
    "rate_limit",
    "quota",
    "quota_exhausted",
    "usage_limit",
    "insufficient_quota",
})

#: Wait before a budget-failed card returns to the queue when the window reset
#: time is unknown.  The MiniMax 5h window resets within five hours; half an
#: hour is a conservative first retry.
DEFAULT_BUDGET_REQUEUE_DELAY_S = 1800

#: A card is never held longer than this, even when the window reset time is
#: far away: a stale probe must not park a card forever.
MAX_BUDGET_HOLD_S = 6 * 3600

#: The window is open again: a held card may return to the queue.
WINDOW_OPEN = "open"

#: The window is still closed: the card stays held.
WINDOW_EXHAUSTED = "exhausted"

#: The window state could not be confirmed: the card stays held.
WINDOW_UNKNOWN = "unknown"

#: "resets in 2h13m": the provider error may say when the window recovers.
RESETS_IN_RE = re.compile(
    r"\bresets in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\b", re.I)

#: "resets in 900": the same phrase without a unit.
RESETS_IN_PLAIN_RE = re.compile(r"\bresets in\s+(\d+)\b", re.I)

#: "Retry-After: 120": the same information as an HTTP header or a JSON key.
RETRY_AFTER_RE = re.compile(r"\bretry[-_ ]?after\"?\s*[:=]\s*\"?(\d+)", re.I)


def normalize(error_class: Any) -> str:
    """Return the error class with a stable spelling (lower case, underscores)."""
    if not isinstance(error_class, str):
        return ""
    return error_class.strip().lower().replace("-", "_")


def classify(error_class: Any) -> str:
    """Return the failure reason class: ``budget``, ``quota`` or ``real``.

    The reported class is normalized first, so ``BUDGET-EXCEEDED`` and
    ``budget_exceeded`` are the same class.  An unknown class is a real
    failure: only a class that names a spent window is treated as a budget
    failure.
    """
    key = normalize(error_class)
    if key in BUDGET_CLASSES:
        return REASON_BUDGET
    if key in QUOTA_CLASSES:
        return REASON_QUOTA
    return REASON_REAL


def is_budget(error_class: Any) -> bool:
    """True when the failure was caused by a spent budget window."""
    return classify(error_class) == REASON_BUDGET


def epoch(value: Any) -> Optional[float]:
    """Return an epoch second from an epoch number or an ISO-8601 string.

    ``None`` is returned for anything that is not a timestamp, so a caller can
    fall back to its own default instead of guessing.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        stamp = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def requeue_at(now: Any, resets_at: Any = None,
               delay_s: float = DEFAULT_BUDGET_REQUEUE_DELAY_S,
               max_hold_s: float = MAX_BUDGET_HOLD_S) -> float:
    """Return the epoch second at which a budget-failed card may be requeued.

    The window reset time wins when it is known (``resets_at``, epoch seconds
    or ISO-8601): the card waits for the window to recover.  Without it the
    card waits ``delay_s`` seconds.  The wait is capped by ``max_hold_s`` so a
    stale reset time cannot park a card forever.

    Args:
        now: the current time (epoch seconds).
        resets_at: when the quota window recovers; ``None`` when unknown.
        delay_s: fallback wait when the reset time is unknown.
        max_hold_s: upper bound of the hold.

    Returns:
        The epoch second at which the card may return to the queue.
    """
    now_epoch = epoch(now)
    if now_epoch is None:
        now_epoch = 0.0
    reset = epoch(resets_at)
    if reset is None:
        target = now_epoch + max(0.0, float(delay_s))
    else:
        target = max(now_epoch, reset)
    return min(target, now_epoch + max(0.0, float(max_hold_s)))


def reset_seconds_from_text(text: Any) -> Optional[int]:
    """Return the seconds until the window recovers according to the run text.

    The provider error often says when the window recovers ("resets in 2h13m",
    "Retry-After: 120").  That is a trustworthy recovery time, so the card can
    wait for it instead of guessing.

    Args:
        text: the run text (error, summary, status).

    Returns:
        The seconds until the window recovers, or None when the text does not
        say.
    """
    if not isinstance(text, str) or not text:
        return None
    match = RESETS_IN_RE.search(text)
    if match and any(match.groups()):
        hours, minutes, seconds = (int(x or 0) for x in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    match = RESETS_IN_PLAIN_RE.search(text)
    if match:
        return int(match.group(1))
    match = RETRY_AFTER_RE.search(text)
    if match:
        return int(match.group(1))
    return None


def probe_state(probe: Any) -> str:
    """Classify a window probe answer: open, exhausted or unknown.

    A probe answers ``{"exhausted": true|false}``.  Only an explicit answer is
    a state: a missing, empty or unreadable answer is unknown, and an unknown
    state never puts a card back into the queue.

    Args:
        probe: the probe answer (a dict) or None.

    Returns:
        ``WINDOW_OPEN``, ``WINDOW_EXHAUSTED`` or ``WINDOW_UNKNOWN``.
    """
    if not isinstance(probe, dict):
        return WINDOW_UNKNOWN
    exhausted = probe.get("exhausted")
    if exhausted is True:
        return WINDOW_EXHAUSTED
    if exhausted is False:
        return WINDOW_OPEN
    return WINDOW_UNKNOWN


def window_state(probe: Any = None, reset_passed: bool = False) -> str:
    """Return the window state a harvester may act on.

    The probe knows the window that is closed right now, so its answer wins.
    Without a probe answer a recovery time that has passed means the window has
    recovered; anything else is unknown and the card stays held.

    Args:
        probe: the window probe answer (a dict) or None.
        reset_passed: True when a known recovery time has passed.

    Returns:
        ``WINDOW_OPEN``, ``WINDOW_EXHAUSTED`` or ``WINDOW_UNKNOWN``.
    """
    state = probe_state(probe)
    if state != WINDOW_UNKNOWN:
        return state
    return WINDOW_OPEN if reset_passed else WINDOW_UNKNOWN
