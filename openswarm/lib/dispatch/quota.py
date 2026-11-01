"""
Dispatch quota and rate-limit classification.

Identifies quota exhaustion errors from backend error outputs and determines
the appropriate cooldown duration and breaker scope.
"""

from __future__ import annotations

import datetime
import email.utils
import re
from typing import Any, Dict, Optional

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
