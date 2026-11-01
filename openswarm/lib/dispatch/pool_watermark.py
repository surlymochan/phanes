"""Dispatch pool watermark: is the dispatchable pool deep enough, and which backend is short.

PAUSE-MINT stops the minting of new cards, so the pool of dispatchable cards can run dry
while the lanes stay idle (06:11: 0 dispatchable cards, 10 idle MiniMax lanes, found by a
human supervisor).  This module gives that fact a pure home: it takes the cards and the
lanes the caller already holds and answers two questions.

* **is the pool deep enough?**  ``ready`` counts the dispatchable cards, ``deficit`` is
  ``max(0, target - ready)`` and ``level`` is ``ok`` (ready >= target), ``low``
  (0 < ready < target) or ``empty`` (ready == 0).
* **which backend is short?**  ``per_backend_deficit`` counts, per backend, the idle
  lanes that have no card of their own backend.

A card is dispatchable when it carries the ready label (``lane-ready``), carries no
``in-progress`` label, is not owned by an external lane (``backend:external*`` and
``backend:chatgpt-agent`` belong to the external agents, not to the fleet) and has no
open dependency.

Counting rules:

* ``by_backend`` counts the dispatchable cards per ``backend:<name>`` label.  A card
  without a backend label is unpinned: it counts in ``ready`` (and therefore in
  ``deficit``) and is reported as ``unpinned``, but it is not attributed to a backend,
  because the backend that finally takes it is only decided at dispatch time.
* ``per_backend_deficit[backend]`` is ``max(0, idle_lanes[backend] - cards[backend])``:
  the idle lanes of that backend that have no card of their own.

Nothing here touches the network, the filesystem or the clock: the caller passes the
clock in, so the result is reproducible and can be written to a state file as is
(Refs #1559).
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterator, List, Optional

READY_LABEL = "lane-ready"
IN_PROGRESS_LABEL = "in-progress"
BACKEND_PREFIX = "backend:"

# Labels owned by the external lanes: their cards are not dispatchable by the fleet.
EXCLUDED_BACKEND_PREFIXES = ("backend:external",)
EXCLUDED_BACKEND_LABELS = ("backend:chatgpt-agent",)

# Keys that mark a dict as a lane record rather than a mapping of lane name -> record.
LANE_RECORD_KEYS = ("lane", "name", "id", "backend", "idle", "busy", "state", "status",
                    "drain", "paused", "issue", "host")

# Keys that mark a dict as a card record rather than a mapping of issue number -> record.
ISSUE_RECORD_KEYS = ("number", "labels", "deps_open", "title", "body")

__all__ = [
    "READY_LABEL",
    "IN_PROGRESS_LABEL",
    "BACKEND_PREFIX",
    "EXCLUDED_BACKEND_PREFIXES",
    "EXCLUDED_BACKEND_LABELS",
    "pool_watermark",
]


def _time(value: Any, name: str) -> float:
    """Return a finite float, or raise for anything that is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("{} must be a number, got {!r}".format(name, value))
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("{} must be finite, got {!r}".format(name, value))
    return number


def _target(value: Any) -> int:
    """Return the wanted pool size as a non-negative int."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("target must be a number, got {!r}".format(value))
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("target must be an integer, got {!r}".format(value))
    number = int(value)
    if number < 0:
        raise ValueError("target must not be negative, got {!r}".format(value))
    return number


def _label_names(issue: Any) -> List[str]:
    """The label names of one card (strings or ``{"name": ...}`` dicts)."""
    if not isinstance(issue, dict):
        return []
    labels = issue.get("labels")
    if isinstance(labels, str):
        labels = [labels]
    if not isinstance(labels, (list, tuple, set, frozenset)):
        return []
    names = []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _backend_label(labels: List[str]) -> Optional[str]:
    """The backend of a card, taken from its first ``backend:<name>`` label."""
    for name in labels:
        if name.casefold().startswith(BACKEND_PREFIX):
            backend = name[len(BACKEND_PREFIX):].strip()
            if backend:
                return backend
    return None


def _is_external(labels: List[str]) -> bool:
    """Whether a card belongs to an external lane instead of the fleet."""
    for name in labels:
        folded = name.casefold()
        if any(folded == excluded.casefold() for excluded in EXCLUDED_BACKEND_LABELS):
            return True
        if any(folded.startswith(prefix) for prefix in EXCLUDED_BACKEND_PREFIXES):
            return True
    return False


def _deps_open(issue: Dict[str, Any]) -> int:
    """How many dependencies of a card are still open (0 when they are all closed)."""
    value = issue.get("deps_open")
    if value is None or value is False:
        return 0
    if value is True:
        return 1
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value)
    return 1


def _lane_backend(lane: Any) -> Optional[str]:
    """The backend a lane runs, or None when the lane does not say."""
    backend = lane.get("backend") if isinstance(lane, dict) else None
    if not isinstance(backend, str) or not backend.strip():
        return None
    name = backend.strip()
    if name.casefold().startswith(BACKEND_PREFIX):
        name = name[len(BACKEND_PREFIX):].strip()
    return name or None


def _lane_idle(lane: Any) -> bool:
    """Whether a lane is free to take a card.

    A lane is idle unless it says otherwise: an explicit ``idle`` / ``busy`` flag wins,
    then a ``state`` / ``status`` string, then a card it already holds.  A lane that
    drains or is paused never takes new work, so it does not count as idle.
    """
    if not isinstance(lane, dict):
        return True
    if lane.get("drain") or lane.get("paused"):
        return False
    if isinstance(lane.get("idle"), bool):
        return lane["idle"]
    if isinstance(lane.get("busy"), bool):
        return not lane["busy"]
    for key in ("state", "status"):
        value = lane.get(key)
        if isinstance(value, str):
            return value.strip().casefold() == "idle"
    if lane.get("issue"):
        return False
    return True


def _iter_issues(issues: Any) -> Iterator[Any]:
    """Yield the card records of a list, of a mapping issue number -> record or of one record."""
    if isinstance(issues, dict):
        if set(issues) & set(ISSUE_RECORD_KEYS):
            yield issues
            return
        for number, record in issues.items():
            if isinstance(record, dict):
                merged = dict(record)
                merged.setdefault("number", number)
                yield merged
            else:
                yield {"number": number}
        return
    for issue in issues or []:
        yield issue


def _iter_lanes(lanes: Any) -> Iterator[Any]:
    """Yield the lane records of a list, of a mapping lane name -> record or of one record."""
    if isinstance(lanes, dict):
        if set(lanes) & set(LANE_RECORD_KEYS) and not any(isinstance(v, dict) for v in lanes.values()):
            yield lanes
            return
        for name, record in lanes.items():
            if isinstance(record, dict):
                merged = dict(record)
                merged.setdefault("lane", name)
                yield merged
            else:
                yield {"lane": name}
        return
    for lane in lanes or []:
        yield lane


def pool_watermark(issues: Any, lanes: Any, target: Any, now: Any) -> Dict[str, Any]:
    """Measure the dispatchable pool and the gap per backend.

    ``issues`` are the card records ``{"number", "labels", "deps_open"}`` (a mapping of
    issue number -> record works too): a card is
    dispatchable when it carries ``lane-ready``, carries no ``in-progress`` label, is
    not owned by an external lane and has no open dependency.  ``lanes`` are the lane
    records ``{"lane", "backend", ...}`` (a mapping of lane name -> record works too);
    a lane is idle when it is not busy, not draining and not paused.  ``target`` is the
    wanted pool size and ``now`` the clock reading the snapshot belongs to.

    The result is a plain dict, ready to be written to a state file or turned into an
    alert: ``ready`` / ``level`` / ``deficit`` describe the pool, ``by_backend`` /
    ``idle_by_backend`` / ``per_backend_deficit`` describe the gap per backend, and
    ``blocked`` counts why a card is not dispatchable.
    """
    moment = _time(now, "now")
    wanted = _target(target)

    ready = 0
    unpinned = 0
    cards_by_backend: Dict[str, int] = {}
    blocked = {"not_ready": 0, "in_progress": 0, "external": 0, "deps_open": 0}

    for issue in _iter_issues(issues):
        if not isinstance(issue, dict):
            continue
        labels = _label_names(issue)
        folded = {name.casefold() for name in labels}
        if READY_LABEL.casefold() not in folded:
            blocked["not_ready"] += 1
            continue
        if IN_PROGRESS_LABEL.casefold() in folded:
            blocked["in_progress"] += 1
            continue
        if _is_external(labels):
            blocked["external"] += 1
            continue
        if _deps_open(issue):
            blocked["deps_open"] += 1
            continue
        ready += 1
        backend = _backend_label(labels)
        if backend is None:
            unpinned += 1
        else:
            cards_by_backend[backend] = cards_by_backend.get(backend, 0) + 1

    total_lanes = 0
    idle_lanes = 0
    idle_by_backend: Dict[str, int] = {}
    for lane in _iter_lanes(lanes):
        total_lanes += 1
        if not _lane_idle(lane):
            continue
        idle_lanes += 1
        backend = _lane_backend(lane)
        if backend is not None:
            idle_by_backend[backend] = idle_by_backend.get(backend, 0) + 1

    backends = sorted(set(cards_by_backend) | set(idle_by_backend))
    by_backend = {backend: cards_by_backend.get(backend, 0) for backend in backends}
    idle = {backend: idle_by_backend.get(backend, 0) for backend in backends}
    per_backend_deficit = {backend: max(0, idle[backend] - by_backend[backend]) for backend in backends}

    if ready == 0:
        level = "empty"
    elif ready >= wanted:
        level = "ok"
    else:
        level = "low"

    return {
        "now": moment,
        "target": wanted,
        "ready": ready,
        "level": level,
        "deficit": max(0, wanted - ready),
        "unpinned": unpinned,
        "idle_lanes": idle_lanes,
        "total_lanes": total_lanes,
        "by_backend": by_backend,
        "idle_by_backend": idle,
        "per_backend_deficit": per_backend_deficit,
        "blocked": blocked,
    }
