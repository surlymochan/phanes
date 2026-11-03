"""Tiered SSH backoff for flaky fleet hosts (Refs #1689).

A host whose SSH probe failed is never dropped from the fleet: the *consecutive*
failure streak decides how often it is probed again.

* **soft tier** (``streak < soft_limit``): a short retry interval.  The host is
  probed again after 30 s, 60 s, ... so a host that comes back is dispatched to
  again within the next minute instead of waiting for a long window.
* **hard tier** (``streak >= soft_limit``): a long interval.  The host is probed
  once per backoff window (the ticks in between are skipped), so a host that is
  really down is not hammered with probes.

The streak counts consecutive failures only: **one successful probe clears it**.
A host is never judged by its historical peak -- a host that failed 58 times in
a row and answers again is a normal host again.

The host is judged by its **SSH results**, not by its lanes: one lane of a host
that answered is enough to clear the streak, so a lane that died while another
lane of the same host answered never extends the backoff (``host_reachable``).

Two gates follow from the tier:

* ``probe_at`` -- the next probe of the host.  While the host sits in the hard
  tier this is the low-frequency probe that keeps the liveness signal alive.
* the dispatch gate -- open in the soft tier once the short backoff expired,
  closed in the hard tier until a probe succeeds (the success clears the
  streak, so the dispatch resumes in the very same tick).

The module is pure: it reads no file, knows no lane and owns no clock.  The
caller keeps the per-host state dictionary and passes the time in.

The same policy is mirrored into the box-side autodispatch shim
(``tools/lane-autodispatch/autodispatch.py``), which runs as a standalone
script; ``openswarm/tests/test_autodispatch.py`` asserts that both agree.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

#: The backoff tiers of a host.
SOFT = "soft"
HARD = "hard"

#: The prefix of a probe error that means the SSH session to the host *worked* and
#: the status script of the lane crashed on the host.  Such an error is a lane
#: level failure: the host answered, so it never counts against the streak.
LANE_ERROR_PREFIX = "remote status exc:"

#: Consecutive failures below this count stay in the soft tier (short retries).
DEFAULT_SOFT_LIMIT = 3
#: The soft tier waits 30 s, 60 s, ... (capped at ``soft_max_s``).
DEFAULT_SOFT_BASE_S = 30
DEFAULT_SOFT_MAX_S = 120
#: The hard tier waits 240 s, 480 s, ... (capped at ``hard_max_s``).
DEFAULT_HARD_BASE_S = 240
DEFAULT_HARD_MAX_S = 600

#: The tunable limits of the policy (the dispatch config may override them).
LIMITS: Dict[str, int] = {
    "soft_limit": DEFAULT_SOFT_LIMIT,
    "soft_base_s": DEFAULT_SOFT_BASE_S,
    "soft_max_s": DEFAULT_SOFT_MAX_S,
    "hard_base_s": DEFAULT_HARD_BASE_S,
    "hard_max_s": DEFAULT_HARD_MAX_S,
}


def limits(overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, int]:
    """The backoff limits: the defaults with the caller's overrides.

    Only known keys with a positive number are applied; anything else keeps the
    default, so a broken config never disables the backoff.
    """
    out = dict(LIMITS)
    for key, value in (overrides or {}).items():
        if key in out and isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            out[key] = int(value)
    return out


def _count(streak: Any) -> int:
    """The failure count of a streak value; a broken value counts as no failure."""
    try:
        return max(0, int(streak or 0))
    except (TypeError, ValueError):
        return 0


def _epoch(value: Any) -> float:
    """A timestamp of a state value; a broken value counts as "now" (no wait)."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def tier(streak: Any, soft_limit: int = DEFAULT_SOFT_LIMIT) -> str:
    """The backoff tier of a failure streak: soft below the limit, hard from it on."""
    limit = _count(soft_limit) or DEFAULT_SOFT_LIMIT
    return SOFT if _count(streak) < limit else HARD


def backoff_seconds(streak: Any, overrides: Optional[Mapping[str, Any]] = None) -> int:
    """The wait in seconds before the next probe of a host that failed ``streak`` times in a row.

    Soft tier: ``soft_base_s * 2 ** (streak - 1)``, capped at ``soft_max_s``.
    Hard tier: ``hard_base_s * 2 ** (streak - soft_limit)``, capped at
    ``hard_max_s``.  A streak of zero means "no failure" and waits for nothing.
    """
    lim = limits(overrides)
    count = _count(streak)
    if count <= 0:
        return 0
    if tier(count, lim["soft_limit"]) == SOFT:
        return int(min(lim["soft_max_s"], lim["soft_base_s"] * 2 ** (count - 1)))
    exponent = min(count - lim["soft_limit"], 16)
    return int(min(lim["hard_max_s"], lim["hard_base_s"] * 2 ** exponent))


def record_failure(state: Optional[Mapping[str, Any]], now: float,
                   overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The backoff state of a host after one more SSH failure at ``now``.

    The state carries the streak, the tier, the wait, the end of the backoff
    (``until``) and the next probe time (``probe_at``).
    """
    lim = limits(overrides)
    state = dict(state or {})
    count = _count(state.get("n")) + 1
    wait = backoff_seconds(count, lim)
    state.update({"n": count, "tier": tier(count, lim["soft_limit"]), "wait": wait,
                  "until": now + wait, "probe_at": now + wait})
    return state


def record_success(state: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The backoff state of a host after a successful probe: the streak is cleared.

    One success is enough -- the host is never judged by its historical peak, so
    the next failure starts the ladder from the beginning again.
    """
    return {}


def lane_error(error: Any) -> bool:
    """Whether a probe error is a lane level failure (the SSH session to the host worked)."""
    return str(error or "").startswith(LANE_ERROR_PREFIX)


def host_reachable(results: Optional[Mapping[str, Any]]) -> bool:
    """Whether the probes of one host show that the host answered.

    ``results`` maps the lanes of the host to their probe results.  The host
    answered when at least one lane came back without an error, or when the only
    errors are lane level ones (the SSH session worked, the status script of the
    lane crashed).  Only when *every* probe of the host failed with an SSH level
    error the host counts as down.

    One successful probe is enough: the failure streak of the host is cleared, a
    lane that failed while another lane of the same host answered must never
    extend the backoff (Refs #1689).
    """
    probed = False
    for result in (results or {}).values():
        if not isinstance(result, dict) or result.get("skip"):
            continue                              # the lane was not probed at all
        probed = True
        error = result.get("error")
        if not error or lane_error(error):
            return True                           # the host answered
    return not probed                           # no probe at all: no evidence of a failure


def probe_due(state: Optional[Mapping[str, Any]], now: float) -> bool:
    """Whether the host may be probed again now.

    A host without a backoff is always due.  A host in the soft tier is due
    when its short backoff expired; a host in the hard tier is due once per
    backoff window (the low-frequency probe).
    """
    state = state or {}
    if not state:
        return True
    until = _epoch(state.get("until"))
    return now >= _epoch(state.get("probe_at", until))


def dispatchable(state: Optional[Mapping[str, Any]], now: float) -> bool:
    """Whether the host may receive a dispatch now.

    The soft tier opens when its short backoff expired.  The hard tier stays
    closed: only a successful probe (which clears the streak) opens it again.
    """
    state = state or {}
    if not state:
        return True
    if state.get("tier", SOFT) == HARD:
        return False
    return now >= _epoch(state.get("until"))


def describe(state: Optional[Mapping[str, Any]], now: float) -> Dict[str, Any]:
    """The observable backoff row of a host (for the status snapshot)."""
    state = state or {}
    if not state:
        return {}
    until = _epoch(state.get("until"))
    probe_at = _epoch(state.get("probe_at", until))
    return {"streak": _count(state.get("n")), "tier": state.get("tier") or SOFT,
            "wait_s": int(state.get("wait") or 0), "until": until, "next_probe_at": probe_at,
            "probe_due_in_s": max(0, int(probe_at - now)),
            "dispatchable": dispatchable(state, now)}


def snapshot(states: Optional[Mapping[str, Mapping[str, Any]]], now: float) -> Dict[str, Dict[str, Any]]:
    """The backoff rows of every host that is backing off, keyed by host name."""
    rows = {}
    for host, state in (states or {}).items():
        row = describe(state, now)
        if row:
            rows[host] = row
    return dict(sorted(rows.items()))
