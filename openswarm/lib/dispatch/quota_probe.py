"""Read-only MiniMax Token Plan quota-window probe for dispatch (Refs #1532).

Calls the free ``coding_plan/remains`` endpoint instead of sending a model
request, so probing spends no tokens. ``fetch`` is injected so the parser can
be tested without the network. Failures are returned as ``ok=False`` and never
raised; the API key is sent only in the Authorization header and is scrubbed
from any text that leaves this module.

The fields are the ones the repository already reads for the same endpoint,
``openswarm/lib/dispatch/quota.py`` (issue #1536, PR #1584):
``current_interval_remaining_percent`` and ``current_weekly_remaining_percent``
are the REMAINING share of the 5h window and of the weekly window, and
``end_time`` / ``weekly_end_time`` are the window ends in epoch milliseconds.
The counts (``current_*_usage_count`` / ``current_*_total_count``) are only
read when the percentages are missing; they are read as USED counts (the field
name says usage), so the remaining share is ``(total - usage) / total``.  That
direction is an assumption and not a live reading, and the tests pin it so it
can not be flipped silently.

``resets_at`` is the end of the window that is actually spent: the 5h window
when it is used up, otherwise the weekly window when only the week is used up
(the same order ``openswarm/lib/dispatch/quota.py`` uses).  A plan whose week
is spent does not come back at the end of the 5h window, so a pause that ends
there would unpause a plan that still answers 429.

The result is not the ``mm_state`` of the health-check contract
(``tools/check-health-quota-fresh.py``), which is ``{exhausted, until,
backend}``: a consumer maps ``resets_at`` to ``until`` and adds the backend
that consumes the provider (``pi``).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

MINIMAX_REMAINS_URL = "https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains"
EXIT_OK = 0
EXIT_EXHAUSTED = 3
EXIT_FAILED = 2

#: Epoch values above this are milliseconds, not seconds (1e11 s is year 5138).
#: The same threshold ``openswarm/lib/dispatch/quota.py`` uses.
_MS_EPOCH_THRESHOLD = 1e11

#: A duration of at least this many units is read as milliseconds: 1e6 ms is
#: 16.7 minutes, 1e6 s is 11.6 days.  A quota window is never shorter than a
#: few minutes, so no window this module sees is read in the wrong unit (the
#: 5h window is 18000 s or 1.8e7 ms, the week is 604800 s or 6.048e8 ms).
_MS_DURATION_THRESHOLD = 1e6

#: The containers that may hold the model_remains list.  The endpoint nests
#: the list differently from call to call, so the same containers
#: ``openswarm/lib/dispatch/quota.py`` looks into are read here.
_REMAINS_CONTAINERS = ("coding_plan", "data", "result")

#: The fields that carry a quota reading of one window.
_WINDOW_FIELDS = ("current_interval_remaining_percent",
                  "current_weekly_remaining_percent",
                  "current_interval_usage_count",
                  "current_weekly_usage_count")

FetchFn = Callable[[str, Dict[str, str]], Tuple[int, str]]


def _num(value: Any) -> Optional[float]:
    """Return the value as a float, or None when it is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    num = float(value)
    return num if math.isfinite(num) else None


def _epoch_s(value: Any) -> Optional[int]:
    """Accept epoch seconds or milliseconds and return seconds."""
    num = _num(value)
    if num is None or num <= 0:
        return None
    return int(num / 1000) if num > _MS_EPOCH_THRESHOLD else int(num)


def _duration_s(value: Any, now: float) -> Optional[int]:
    """Turn a remaining duration into an epoch-seconds reset time.

    A duration is read as milliseconds when it is at least 1e6 (16.7 minutes
    expressed in milliseconds) and as seconds below that.  Quota windows are
    never shorter than a few minutes, so the two readings do not overlap for
    any window this module sees: the 5h window is 18000 s or 1.8e7 ms, the
    weekly window is 604800 s or 6.048e8 ms.
    """
    num = _num(value)
    if num is None or num < 0:
        return None
    return int(now + (num / 1000.0 if num >= _MS_DURATION_THRESHOLD else num))


def _pct_field(entry: Mapping[str, Any], window: str) -> Optional[float]:
    """The remaining percentage of a window (``interval`` or ``weekly``)."""
    value = _num(entry.get(f"current_{window}_remaining_percent"))
    if value is None:
        return None
    return round(max(0.0, min(100.0, value)), 2)


def _pct_count(entry: Mapping[str, Any], window: str) -> Optional[float]:
    """The remaining share of a window from the count fields.

    ``current_<window>_usage_count`` is read as the USED count, so the
    remaining share is ``(total - usage) / total``.  The direction is an
    assumption (the field name says usage), and the tests pin it.
    """
    usage = _num(entry.get(f"current_{window}_usage_count"))
    total = _num(entry.get(f"current_{window}_total_count"))
    if usage is None or total is None or total <= 0:
        return None
    return round(max(0.0, min(100.0, (total - usage) / total * 100.0)), 2)


def _window_pct(entry: Mapping[str, Any], window: str) -> Optional[float]:
    """The remaining share of a window.

    The percentage is the primary reading: it is the field the repository
    already merged for this endpoint.  The counts are only read when the
    percentage is missing.
    """
    pct = _pct_field(entry, window)
    if pct is not None:
        return pct
    return _pct_count(entry, window)


def _containers(body: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The dicts that may hold the model_remains list."""
    containers: List[Mapping[str, Any]] = [body]
    for key in _REMAINS_CONTAINERS:
        nested = body.get(key)
        if isinstance(nested, Mapping):
            containers.append(nested)
    return containers


def _remains_entry(containers: List[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """The model_remains entry that carries the quota reading."""
    fallback: Optional[Mapping[str, Any]] = None
    for container in containers:
        remains = container.get("model_remains")
        if not isinstance(remains, list):
            continue
        for entry in remains:
            if not isinstance(entry, Mapping):
                continue
            if fallback is None:
                fallback = entry
            if any(field in entry for field in _WINDOW_FIELDS):
                return entry
    return fallback


def _fail(error: str) -> Dict[str, Any]:
    """A failed probe, with the same keys as a successful one."""
    return {
        "ok": False,
        "error": error,
        "interval_pct": None,
        "weekly_pct": None,
        "interval_start": None,
        "resets_at": None,
        "weekly_resets_at": None,
        "exhausted": None,
    }


def _scrub(text: str, key: str) -> str:
    """Mask the API key in text that leaves this module.

    The whole key is masked, and so are its ends: an upstream that echoes a
    truncated or slightly changed key must not leak it either.
    """
    if not key:
        return text
    out = text.replace(key, "***")
    for fragment in (key[:8], key[-8:]):
        if len(fragment) >= 8:
            out = out.replace(fragment, "***")
    return out


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse cross-host redirects so the Authorization header never leaves the host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_host = urllib.parse.urlsplit(req.full_url).hostname
        if urllib.parse.urlsplit(newurl).hostname != old_host:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def probe_minimax(fetch: FetchFn, key: str, now: float) -> Dict[str, Any]:
    """Return the current MiniMax coding-plan window state.

    ``interval_pct`` and ``weekly_pct`` are the remaining percentages of the
    5h window and of the weekly window, ``resets_at`` is the epoch second at
    which the spent window resets and ``exhausted`` is true when the 5h window
    or the weekly quota is used up.  ``interval_start`` is None when the
    response carries no window start.

    ``resets_at`` is epoch seconds, the field ``lib/dispatch/burn.py`` reads
    out of a quota window.
    """
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        status, text = fetch(MINIMAX_REMAINS_URL, headers)
    except Exception as exc:  # noqa: BLE001 - report type only; message may echo the key
        return _fail(f"network error: {type(exc).__name__}")
    if status != 200:
        return _fail(f"HTTP {status}")
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        return _fail("invalid JSON")
    if not isinstance(body, Mapping):
        return _fail("unexpected response shape")

    containers = _containers(body)
    base = next((container.get("base_resp") for container in containers
                 if isinstance(container.get("base_resp"), Mapping)), None)
    if not isinstance(base, Mapping):
        return _fail("missing base_resp")
    code = str(base.get("status_code")).strip()
    if code != "0":
        msg = str(base.get("status_msg") or "")[:200]
        return _fail(_scrub(f"base_resp status_code={code} {msg}".strip(), key))

    entry = _remains_entry(containers)
    if entry is None:
        return _fail("no model_remains")

    interval_pct = _window_pct(entry, "interval")
    weekly_pct = _window_pct(entry, "weekly")
    if interval_pct is None:
        return _fail("missing interval quota fields")

    interval_start = _epoch_s(entry.get("start_time"))
    interval_end = _epoch_s(entry.get("end_time"))
    if interval_end is None:
        interval_end = _duration_s(entry.get("remains_time"), now)
    weekly_end = _epoch_s(entry.get("weekly_end_time"))
    if weekly_end is None:
        weekly_end = _duration_s(entry.get("weekly_remains_time"), now)

    interval_exhausted = interval_pct <= 0
    weekly_exhausted = weekly_pct is not None and weekly_pct <= 0
    # The spent window decides when the plan may be used again: the weekly
    # window ends long after the 5h one, so the weekly end is only used when
    # the week is the spent window.
    resets_at = interval_end
    if weekly_exhausted and not interval_exhausted:
        resets_at = weekly_end

    return {
        "ok": True,
        "error": None,
        "interval_pct": interval_pct,
        "weekly_pct": weekly_pct,
        "interval_start": interval_start,
        "resets_at": resets_at,
        "weekly_resets_at": weekly_end,
        "exhausted": interval_exhausted or weekly_exhausted,
    }


def _http_fetch(url: str, headers: Dict[str, str]) -> Tuple[int, str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_SameHostRedirect)
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m lib.dispatch.quota_probe")
    parser.add_argument("--minimax", action="store_true", help="probe MiniMax coding_plan/remains")
    parser.add_argument("--key-env", default="MINIMAX_CN_API_KEY", help="env var holding the API key")
    args = parser.parse_args(argv)
    if not args.minimax:
        parser.error("--minimax is required")

    key = os.environ.get(args.key_env, "")
    if not key:
        result = _fail(f"env {args.key_env} is not set")
    else:
        result = probe_minimax(_http_fetch, key, time.time())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result["ok"]:
        return EXIT_FAILED
    return EXIT_EXHAUSTED if result["exhausted"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
