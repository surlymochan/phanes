"""
Dispatch quota and rate-limit classification.

Identifies quota exhaustion errors from backend error outputs and determines
the appropriate cooldown duration and breaker scope.

MiniMax (the ``minimax-cn`` provider) needs a second look.  A spent Token Plan
window answers with HTTP 429 and a body like::

    {"type":"rate_limit_error","message":"已达到 Token Plan 用量上限…(2056)"}

The body carries neither ``Retry-After`` nor a "resets in ..." phrase, so the
generic classifier cannot tell it apart from a plain requests-per-minute limit
and the dispatcher holds the lane for a fixed 30 minutes.  ``classify_minimax``
separates the two cases (``kind`` is ``quota_window`` or ``rate_limit``) and
``reset_from_minimax_remains`` reads the coding-plan remains API to get the
real window reset time.

Refs #1536.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import math
import re
import time
from typing import Any, Dict, List, Optional

# Matches 'resets in ...' with composite (1h20m30s), unit variants (54s, 10m, 2 hours),
# or plain seconds.
_RESETS_IN_PATTERNS = [
    re.compile(
        r"\bresets in\s+(?:(\d+)\s*h(?:(?:ou)?r)?s?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?\s*(?:(\d+)\s*s(?:ec(?:ond)?s?)?)?\b",
        re.I,
    ),
    re.compile(r"\bresets in\s+(\d+)\b", re.I),
]

# Matches 'Retry-After: ...' header or JSON keys like "retry_after": ...
# Handles quoted strings, HTTP-date format, duration with units, or plain numbers.
_RETRY_AFTER_PAT = re.compile(
    r"""(?:\bRetry[-_]After|\bretryAfter)["']?\s*[:=]\s*(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z]{3},\s*\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}(?:\s+[A-Z]{3,4})?)|(\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?)|([^,\r\n;}\]]+))""",
    re.I,
)

_DURATION_UNITS_PAT = re.compile(
    r"^(?:(\d+)\s*h(?:(?:ou)?r)?s?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?\s*(?:(\d+)\s*s(?:ec(?:ond)?s?)?)?$",
    re.I,
)

# A spent MiniMax Token Plan window is marked in the error body: the plan name,
# the Chinese wording or the upstream error code 2056.
_MINIMAX_QUOTA_WINDOW_RE = re.compile(r"Token\s*Plan|用量上限|\b2056\b", re.I)

# A plain MiniMax 429 has no marker above: the error type or the HTTP status.
_MINIMAX_RATE_LIMIT_RE = re.compile(r"rate_limit_error|\b429\b", re.I)

#: A plain MiniMax rate limit (requests per minute) clears within a minute.
MINIMAX_RATE_LIMIT_COOLDOWN_S = 60

#: A spent Token Plan window has no cooldown in the error body; the real reset
#: time comes from the coding-plan remains API (see
#: :func:`reset_from_minimax_remains`).
MINIMAX_QUOTA_WINDOW_COOLDOWN_S = 0

#: The Token Plan window is provider wide, so the breaker scope is the backend.
MINIMAX_SCOPE = "backend"

#: Epoch values above this are milliseconds, not seconds (1e11 s is year 5138).
_MS_EPOCH_THRESHOLD = 1e11


def _parse_duration_val(val: str) -> Optional[int]:
    """Parse a duration string from Retry-After into seconds."""
    val = val.strip().strip("'\"}")
    if not val:
        return None
    if val.isdigit():
        return int(val)
    try:
        return int(float(val))
    except ValueError:
        pass

    m = _DURATION_UNITS_PAT.match(val)
    if m and any(m.groups()):
        h, m_min, s = (int(x or 0) for x in m.groups())
        return h * 3600 + m_min * 60 + s

    # HTTP-date (RFC 7231 / RFC 2616)
    try:
        dt = email.utils.parsedate_to_datetime(val)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            diff = int((dt - now).total_seconds())
            return max(0, diff)
    except Exception:
        pass
    return None


def extract_cooldown(text: Optional[str]) -> Optional[int]:
    """
    Extract cooldown duration in seconds from text containing 'Retry-After' or 'resets in ...'.

    Returns:
        Integer seconds if found, otherwise None.
    """
    if not isinstance(text, str) or not text:
        return None

    # 1. Retry-After header / JSON key
    m_retry = _RETRY_AFTER_PAT.search(text)
    if m_retry:
        val = next((x for x in m_retry.groups() if x is not None), None)
        if val:
            dur = _parse_duration_val(val)
            if dur is not None:
                return dur

    # 2. "resets in ..." phrase
    for pat in _RESETS_IN_PATTERNS:
        m = pat.search(text)
        if m:
            groups = m.groups()
            if len(groups) == 3 and any(groups):
                h, m_min, s = (int(x or 0) for x in groups)
                return h * 3600 + m_min * 60 + s
            elif len(groups) == 1 and groups[0]:
                return int(groups[0])

    return None


def classify(
    backend: str,
    text: Optional[str],
    cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Classify whether text contains a quota error for backend based on cfg.

    Args:
        backend: Backend name (e.g. 'copilot', 'agy').
        text: Error message, stderr, or response text to inspect.
        cfg: Configuration dict mapping backend name to its quota configuration,
             e.g.:
             {
                 "copilot": {
                     "quota_error_re": "402|quota exceeded",
                     "cooldown_s": 3600,
                     "scope": "lane"
                 },
                 "agy": {
                     "quota_error_re": "429|RESOURCE_EXHAUSTED",
                     "cooldown_s": 1800,
                     "scope": "backend"
                 }
             }

    Returns:
        Dict with {'scope': str, 'ttl_s': int, 'reason': str} if matched,
        otherwise None.
    """
    if not isinstance(cfg, dict) or not backend:
        return None
    if not isinstance(text, str) or not text.strip():
        return None

    bcfg = cfg.get(backend)
    if bcfg is None and isinstance(backend, str):
        bcfg = cfg.get(backend.lower())
    if not isinstance(bcfg, dict):
        return None

    quota_re = bcfg.get("quota_error_re")
    if not quota_re:
        return None

    if isinstance(quota_re, str):
        match = re.search(quota_re, text, re.IGNORECASE)
    else:
        match = quota_re.search(text)

    if not match:
        return None

    scope = str(bcfg.get("scope", "backend"))
    default_cooldown = bcfg.get("cooldown_s", 0)
    try:
        ttl_s = int(default_cooldown) if default_cooldown is not None else 0
    except (ValueError, TypeError):
        ttl_s = 0

    extracted = extract_cooldown(text)
    if extracted is not None:
        ttl_s = extracted

    reason = str(bcfg.get("reason") or match.group(0).strip())

    return {
        "scope": scope,
        "ttl_s": ttl_s,
        "reason": reason,
    }


def classify_minimax(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Classify a MiniMax provider error body.

    A spent Token Plan window and a plain requests-per-minute limit both answer
    with HTTP 429, but only the first one lasts for hours.  The body tells them
    apart: the window error names the plan (or the upstream code 2056), a plain
    rate limit does not.

    Args:
        text: The provider response body, or any part of the error output.

    Returns:
        Dict with {'kind': 'quota_window'|'rate_limit', 'scope': str,
        'ttl_s': int, 'reason': str} when the text is a MiniMax rate limit
        error, otherwise None.

        For ``kind`` 'quota_window' the ``ttl_s`` is 0: the error body does not
        tell when the window resets, the coding-plan remains API does (see
        :func:`reset_from_minimax_remains`).
    """
    if not isinstance(text, str) or not text.strip():
        return None

    window = _MINIMAX_QUOTA_WINDOW_RE.search(text)
    if window:
        return {
            "kind": "quota_window",
            "scope": MINIMAX_SCOPE,
            "ttl_s": MINIMAX_QUOTA_WINDOW_COOLDOWN_S,
            "reason": window.group(0).strip(),
        }

    rate_limit = _MINIMAX_RATE_LIMIT_RE.search(text)
    if rate_limit:
        return {
            "kind": "rate_limit",
            "scope": MINIMAX_SCOPE,
            "ttl_s": MINIMAX_RATE_LIMIT_COOLDOWN_S,
            "reason": rate_limit.group(0).strip(),
        }

    return None


def _parse_minimax_payload(payload: Any) -> Optional[Dict[str, Any]]:
    """Return the remains payload as a dict, or None when it cannot be read."""
    data = payload
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8")
        except Exception:
            return None
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _num(value: Any) -> Optional[float]:
    """Return the value as a float, or None when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _epoch_seconds(value: Any) -> Optional[float]:
    """Return an epoch timestamp in seconds, accepting milliseconds."""
    seconds = _num(value)
    if seconds is None or seconds <= 0:
        return None
    if seconds > _MS_EPOCH_THRESHOLD:
        seconds = seconds / 1000.0
    return seconds


def _now_epoch(now: Any) -> float:
    """Return the reference time in epoch seconds."""
    if isinstance(now, datetime.datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
        return now.timestamp()
    seconds = _num(now)
    return seconds if seconds is not None else time.time()


def _minimax_containers(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The dicts that may hold the model_remains list."""
    containers = [data]
    for key in ("coding_plan", "data", "result"):
        nested = data.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    return containers


def _minimax_model_entry(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The model_remains entry that carries the remaining percentages."""
    for container in _minimax_containers(data):
        remains = container.get("model_remains")
        if not isinstance(remains, list):
            continue
        for entry in remains:
            if not isinstance(entry, dict):
                continue
            if ("current_interval_remaining_percent" in entry
                    or "current_weekly_remaining_percent" in entry):
                return entry
    return None


def _minimax_end_time(data: Dict[str, Any], entry: Dict[str, Any], key: str) -> Optional[float]:
    """The end time for ``key`` in epoch seconds, from the entry or the payload."""
    for source in (entry, data):
        seconds = _epoch_seconds(source.get(key))
        if seconds is not None:
            return seconds
    return None


def _minimax_reset_epoch(payload: Any) -> Optional[float]:
    """The epoch second at which the exhausted MiniMax window resets."""
    data = _parse_minimax_payload(payload)
    if data is None:
        return None

    entry = _minimax_model_entry(data)
    if entry is None:
        return None

    interval_pct = _num(entry.get("current_interval_remaining_percent"))
    weekly_pct = _num(entry.get("current_weekly_remaining_percent"))
    if interval_pct is None and weekly_pct is None:
        return None

    if interval_pct is not None and interval_pct <= 0:
        # The 5 hour interval window is spent, it resets before the weekly one.
        return _minimax_end_time(data, entry, "end_time")
    if weekly_pct is not None and weekly_pct <= 0:
        return _minimax_end_time(data, entry, "weekly_end_time")
    return None


def minimax_reset_at(payload: Any) -> Optional[int]:
    """
    Return the epoch second at which the exhausted MiniMax window resets.

    Args:
        payload: The coding-plan remains response, a JSON string or a dict.

    Returns:
        The reset time in epoch seconds, or None when no window is exhausted
        or the response cannot be read.
    """
    epoch = _minimax_reset_epoch(payload)
    return int(epoch) if epoch is not None else None


def reset_from_minimax_remains(payload: Any, now: Any = None) -> Optional[int]:
    """
    Return the seconds until the exhausted MiniMax window resets.

    The coding-plan remains API reports how much of the current interval
    (5 hours) and of the current week is left.  When the interval is spent the
    window resets at ``model_remains[0].end_time``, when only the week is spent
    it resets at ``weekly_end_time``.  Both timestamps are epoch milliseconds.

    Args:
        payload: The coding-plan remains response, a JSON string or a dict.
        now: The reference time, epoch seconds or a datetime.  Defaults to the
            current time.

    Returns:
        The seconds from ``now`` until the window resets (0 when it has already
        reset), or None when no window is exhausted, the fields are missing or
        have the wrong type.
    """
    epoch = _minimax_reset_epoch(payload)
    if epoch is None:
        return None
    return max(0, int(math.ceil(epoch - _now_epoch(now))))
