"""Wake gate: a zero-model-cost check an agent's timer runs before waking the (expensive) agent.

    rc 0  there is a card to take: wake the agent once and let it run one `tick` (claims at most 1 card)
    rc 1  nothing to do: already holding an unfinished claim, hourly wake cap reached, or no claimable card
          (cards this agent failed within `fail_cooldown_s` do not count)
    rc 2  error (bad config, auth, network): do not wake; the timer just tries again next period

Read-only on GitHub. The only write is a small local state file (wake timestamps, recent failures) so the hourly cap and
the failure cooldown hold across runs; delete it at any time to reset. Running `check` twice in a row is safe: the second
call either sees the same card (and counts one more wake against the cap) or rc 1.
"""
from __future__ import annotations

import json
import os
import time

from . import contract as C

MAX_WAKES_PER_HOUR = 4
FAIL_COOLDOWN_S = 2 * 3600
WAKE = 0
IDLE = 1
ERROR = 2


def state_path(agent: str) -> str:
    base = os.environ.get("EXTERNAL_LANE_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "openswarm-external")
    return os.path.join(base, f"{C.check_agent(agent)}.json")


def load_state(path: str) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def note_failure(path: str, issue: int, now: float | None = None) -> None:
    """Called by the agent side when it gives up on a card, so `check` will not wake for it again during the cooldown."""
    state = load_state(path)
    state.setdefault("failed", {})[str(int(issue))] = time.time() if now is None else now
    save_state(path, state)


def decide(inflight, claimable, state, now, max_wakes_per_hour=MAX_WAKES_PER_HOUR, fail_cooldown_s=FAIL_COOLDOWN_S):
    """Pure decision. inflight: this agent's lane-claimed issues; claimable: candidate issues (best first).
    Returns (rc, summary, new_state)."""
    wakes = [t for t in state.get("wakes", []) if now - t < 3600]
    failed = {k: t for k, t in (state.get("failed") or {}).items() if now - t < fail_cooldown_s}
    new = dict(state, wakes=wakes, failed=failed)
    cooling = [i["number"] for i in claimable if str(i["number"]) in failed]
    ready = [i["number"] for i in claimable if str(i["number"]) not in failed]
    summary = {"inflight": [i["number"] for i in inflight], "claimable": ready, "cooling": cooling,
               "wakes_last_hour": len(wakes), "max_wakes_per_hour": max_wakes_per_hour}
    if inflight:
        return IDLE, dict(summary, reason="holding-claim"), new
    if not ready:
        return IDLE, dict(summary, reason="no-card"), new
    if len(wakes) >= max_wakes_per_hour:
        return IDLE, dict(summary, reason="wake-cap"), new
    new["wakes"] = wakes + [now]
    return WAKE, dict(summary, reason="card", next=ready[0], wakes_last_hour=len(wakes) + 1), new


def check(lane, state_file=None, now=None, max_wakes_per_hour=MAX_WAKES_PER_HOUR, fail_cooldown_s=FAIL_COOLDOWN_S):
    """(rc, one-line summary dict). `lane` is an ExternalLane; only its read calls are used."""
    path = state_file or state_path(lane.agent)
    now = time.time() if now is None else now
    try:
        inflight, claimable = lane.inflight(), lane.poll()
    except Exception as error:  # noqa: BLE001 - any read failure means "do not wake"
        return ERROR, {"agent": lane.agent, "reason": "error", "error": str(error)[:300]}
    rc, summary, state = decide(inflight, claimable, load_state(path), now, max_wakes_per_hour, fail_cooldown_s)
    try:
        save_state(path, state)
    except OSError as error:
        return ERROR, {"agent": lane.agent, "reason": "error", "error": f"state file: {error}"[:300]}
    return rc, dict(summary, agent=lane.agent, rc=rc)
