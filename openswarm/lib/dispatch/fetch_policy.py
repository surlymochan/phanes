"""Fetch resilience for the dispatch path (Refs #1719).

The dispatch path runs three git network operations: the base (``main``) fetch, the PR head
fetch and the seed push (the box pulls the objects and pushes them into a host that cannot
reach GitHub).  A flaky host (govpn/govpn2 TLS handshakes, gores1 direct connections) turned a
single failed fetch into a dead run, so every operation runs under one policy:

* a per-host attempt budget with exponential backoff,
* a threshold after which the seed push path takes over (the box fetches, the host never talks
  to GitHub),
* a per-host failure attribution (how many recent failures, which error type) that the health
  output and the blocking diagnostics read.

The module is pure: no I/O and no clock of its own.  The engine (``lib/dispatch/engine.py``) and
the box autodispatcher (``tools/lane-autodispatch/autodispatch.py``) apply it to their own
transport, so both dispatchers agree on the thresholds, the backoff and the error types.
"""

import re
import time

DEFAULT_POLICY = {
    "attempts": 3,            # attempts per git operation
    "backoff_s": 5,           # first backoff between two attempts (doubles, capped)
    "backoff_cap_s": 60,
    "fallback_after": 3,      # recent failures on the host that trigger the seed push (0 = never)
    "health_window_s": 1800,  # the recent window of the per-host failure counters (seconds)
    "health_max_events": 50,  # events kept per host
}

# The diagnosable types of a git network failure.  The order matters: the first match wins.
# A TLS handshake failure is a network problem, not a fatal one: another attempt (or the seed
# push) can still deliver the objects.
ERROR_TYPES = (
    ("tls", r"gnutls_handshake|gnutls|SSL (?:error|connect|handshake)|TLS handshake|certificate (?:verify|problem)|handshake failure"),
    ("dns", r"could not resolve|name or service not known|temporary failure in name resolution|nodename nor servname"),
    ("timeout", r"timed out|timeout|deadline exceeded"),
    ("network", r"connection reset|network is unreachable|failed to connect|recv failure|early eof|"
                r"connection closed|reset by peer|broken pipe|unexpected disconnect|connection refused"),
    ("auth", r"authentication failed|could not read username|terminal prompts disabled|403 forbidden|"
             r"permission denied \(publickey\)"),
    ("not_found", r"repository not found|remote branch .* not found|404 not found"),
)

# The types a retry (and the seed push fallback) can still fix.
RETRYABLE = {"tls", "dns", "timeout", "network", "unknown"}


def classify_error(error):
    """The diagnosable type of a git network failure ("tls", "dns", "timeout", ... "unknown")."""
    text = error or ""
    for name, pattern in ERROR_TYPES:
        if re.search(pattern, text, re.I):
            return name
    return "unknown"


def is_retryable(error_type):
    """True when another attempt (or the seed push fallback) can still fix the failure."""
    return (error_type or "unknown") in RETRYABLE


def policy_for(config, host=None):
    """The effective policy of one host: defaults < config < per-host overrides."""
    policy = dict(DEFAULT_POLICY)
    config = config if isinstance(config, dict) else {}
    for key in DEFAULT_POLICY:
        if key in config:
            policy[key] = config[key]
    hosts = config.get("hosts")
    override = hosts.get(host) if isinstance(hosts, dict) and host else None
    if isinstance(override, dict):
        for key, value in override.items():
            policy[key] = value
    policy["attempts"] = max(1, int(policy.get("attempts") or 1))
    policy["backoff_s"] = max(0, int(policy.get("backoff_s") or 0))
    policy["backoff_cap_s"] = max(policy["backoff_s"], int(policy.get("backoff_cap_s") or 0))
    policy["fallback_after"] = max(0, int(policy.get("fallback_after") or 0))
    policy["health_window_s"] = max(0, int(policy.get("health_window_s") or 0))
    policy["health_max_events"] = max(1, int(policy.get("health_max_events") or 1))
    return policy


def backoff_seconds(attempt, policy):
    """The wait before the next attempt: the base delay, doubled per failed attempt."""
    attempt = max(1, int(attempt or 1))
    base = int(policy.get("backoff_s") or 0)
    cap = int(policy.get("backoff_cap_s") or base)
    return min(cap, base * 2 ** (attempt - 1))


def should_fallback(failures, policy):
    """True when the host's recent failure count reached the seed push threshold."""
    threshold = int(policy.get("fallback_after") or 0)
    return threshold > 0 and int(failures or 0) >= threshold


def record_event(events, host, op, error, error_type=None, now=None, fallback=False):
    """Append one failed git operation to the per-host attribution."""
    if not isinstance(events, dict):
        return events
    moment = time.time() if now is None else float(now)
    row = events.setdefault(str(host), [])
    row.append({"at": moment, "op": str(op or ""), "type": error_type or classify_error(error),
                "detail": (error or "").strip()[-200:], "fallback": bool(fallback)})
    return events


def prune_events(events, policy, now=None):
    """Drop the events outside the recent window and the oldest ones beyond the cap."""
    if not isinstance(events, dict):
        return {}
    moment = time.time() if now is None else float(now)
    window = int(policy.get("health_window_s") or 0)
    cap = int(policy.get("health_max_events") or 1)
    pruned = {}
    for host, rows in events.items():
        if not isinstance(rows, list):
            continue
        kept = [row for row in rows
                if isinstance(row, dict) and (not window or moment - float(row.get("at") or 0) <= window)]
        pruned[str(host)] = kept[-cap:]
    return pruned


def health_rows(events, now=None, window_s=None):
    """The per-host fetch health: the recent failure count and the main error types.

    ``events`` is the per-host event list written by :func:`record_event`; the rows answer the
    two questions the blocking diagnosis asks: how often did the host fail to fetch recently,
    and what kind of failure was it.  ``window_s`` keeps only the events of the recent window
    (the caller prunes with the same window).
    """
    moment = time.time() if now is None else float(now)
    rows = {}
    if not isinstance(events, dict):
        return rows
    for host, raw in events.items():
        if not isinstance(raw, list) or not raw:
            continue
        events_in_window = [row for row in raw
                            if isinstance(row, dict)
                            and (not window_s or moment - float(row.get("at") or 0) <= float(window_s))]
        if not events_in_window:
            continue
        # a fallback marker records the decision (the seed push took over), not another failure
        failures = [row for row in events_in_window if not row.get("fallback")]
        types = {}
        for row in failures:
            name = str(row.get("type") or "unknown")
            types[name] = types.get(name, 0) + 1
        last = failures[-1] if failures else events_in_window[-1]
        rows[str(host)] = {
            "failures": len(failures),
            "types": types,
            "last_type": str(last.get("type") or "unknown"),
            "last_op": str(last.get("op") or ""),
            "last_at": last.get("at"),
            "fallbacks": sum(1 for row in events_in_window if row.get("fallback")),
        }
    return rows


def summarize(row):
    """One short line per host: ``3 failures (tls x2, network x1); last tls``."""
    if not isinstance(row, dict) or not row.get("failures"):
        return "no recent fetch failure"
    types = row.get("types") or {}
    parts = ", ".join(f"{name} x{count}" for name, count in sorted(types.items(), key=lambda kv: (-kv[1], kv[0])))
    return f"{row['failures']} failure(s) ({parts}); last {row.get('last_type') or 'unknown'}"
