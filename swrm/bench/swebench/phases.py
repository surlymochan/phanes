"""Split a run's model calls into the planner phase and the executor phase.

The openswarm arm runs a planner call before the executor; the other arms make a single agent
call. Both phases are reported separately, so the planner's share of the wall-clock time and
of the tokens is visible per cell (optimization backlog #4, P1-1).
"""
from __future__ import annotations

TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
PLANNER = "planner"
EXECUTOR = "executor"


def phase_of(label: str) -> str:
    """The planner call is labelled `planner`; every other call is executor work."""
    return PLANNER if str(label or "").startswith(PLANNER) else EXECUTOR


def phase_split(calls) -> dict:
    """Wall-clock and tokens per phase for one run's recorded calls."""
    phases: dict[str, dict] = {}
    for call in calls or []:
        phase = phase_of(call.get("label"))
        entry = phases.setdefault(phase, {"wall_s": 0.0, "calls": 0,
                                          "tokens": {key: 0 for key in TOKEN_KEYS}})
        entry["wall_s"] += float(call.get("wall_s") or 0)
        entry["calls"] += 1
        usage = call.get("usage") or {}
        for key in TOKEN_KEYS:
            entry["tokens"][key] += usage.get(key, 0) or 0
    for entry in phases.values():
        entry["wall_s"] = round(entry["wall_s"], 1)
        entry["tokens_in_out"] = entry["tokens"]["input"] + entry["tokens"]["output"]
        entry["tokens_total"] = sum(entry["tokens"].values())
    return phases
