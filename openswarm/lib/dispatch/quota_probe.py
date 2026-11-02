"""Read-only MiniMax Token Plan quota-window probe for dispatch.

Calls the free ``coding_plan/remains`` endpoint instead of sending a model
request, so probing spends no tokens. ``fetch`` is injected so the parser can
be tested without the network. Failures are returned as ``ok=False`` and never
raised; the API key is sent only in the Authorization header and is scrubbed
from any text that leaves this module.
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
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

MINIMAX_REMAINS_URL = "https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains"
EXIT_OK = 0
EXIT_EXHAUSTED = 3
EXIT_FAILED = 2

FetchFn = Callable[[str, Dict[str, str]], Tuple[int, str]]


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    num = float(value)
    return num if math.isfinite(num) else None


def _epoch_s(value: Any) -> Optional[int]:
    """Accept epoch seconds or milliseconds and return seconds."""
    num = _num(value)
    if num is None or num <= 0:
        return None
    return int(num / 1000) if num > 1e11 else int(num)


def _resets_from_remains(value: Any, now: float) -> Optional[int]:
    """Turn a ``remains_time`` duration into an epoch-seconds reset time.

    Values >= 1e7 are read as milliseconds: every window in seconds is far
    below that, so a misread can only push the reset later (a longer pause),
    never earlier.
    """
    num = _num(value)
    if num is None or num < 0:
        return None
    return int(now + (num / 1000.0 if num >= 1e7 else num))


def _pct(remaining: Any, total: Any) -> Optional[float]:
    rem, tot = _num(remaining), _num(total)
    if rem is None or tot is None or tot <= 0:
        return None
    return round(max(0.0, min(100.0, rem / tot * 100.0)), 2)


def _fail(error: str) -> Dict[str, Any]:
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
    return text.replace(key, "***") if key else text


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse cross-host redirects so the Authorization header never leaves the host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_host = urllib.parse.urlsplit(req.full_url).hostname
        if urllib.parse.urlsplit(newurl).hostname != old_host:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def probe_minimax(fetch: FetchFn, key: str, now: float) -> Dict[str, Any]:
    """Return the current MiniMax coding-plan window state.

    ``interval_pct`` and ``weekly_pct`` are the remaining percentages. The
    ``*_usage_count`` fields are read as remaining counts; this is an
    assumption not yet confirmed against a live response (see the test file).
    ``exhausted`` is true when either the 5h interval or the weekly quota is
    used up.
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

    base = body.get("base_resp")
    if not isinstance(base, Mapping):
        return _fail("missing base_resp")
    code = str(base.get("status_code")).strip()
    if code != "0":
        msg = str(base.get("status_msg") or "")[:200]
        return _fail(_scrub(f"base_resp status_code={code} {msg}".strip(), key))

    remains = body.get("model_remains")
    if not isinstance(remains, list) or not remains or not all(isinstance(r, Mapping) for r in remains):
        return _fail("no model_remains")
    entry = next(
        (r for r in remains if "minimax" in str(r.get("model_name", "")).lower()),
        remains[0],
    )

    interval_pct = _pct(entry.get("current_interval_usage_count"), entry.get("current_interval_total_count"))
    weekly_pct = _pct(entry.get("current_weekly_usage_count"), entry.get("current_weekly_total_count"))
    if interval_pct is None:
        return _fail("missing interval quota fields")

    interval_start = _epoch_s(entry.get("start_time"))
    resets_at = _epoch_s(entry.get("end_time"))
    if resets_at is None:
        resets_at = _resets_from_remains(entry.get("remains_time"), now)
    weekly_resets_at = _epoch_s(entry.get("weekly_end_time"))
    if weekly_resets_at is None:
        weekly_resets_at = _resets_from_remains(entry.get("weekly_remains_time"), now)

    return {
        "ok": True,
        "error": None,
        "interval_pct": interval_pct,
        "weekly_pct": weekly_pct,
        "interval_start": interval_start,
        "resets_at": resets_at,
        "weekly_resets_at": weekly_resets_at,
        "exhausted": interval_pct <= 0 or (weekly_pct is not None and weekly_pct <= 0),
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
