"""Dispatch priority scoring: a big card must not wait behind small ones (Refs #1705).

The dispatch pool used to be served first-in-first-out: every card carried the same
weight, so the small cards (30-minute tier) that arrive in bulk kept the big cards
(90-minute tier) at the tail of the queue.  A big card that never starts never burns
its 90 minutes of quota, so the hourly burn bar stays low even while lanes idle.

This module replaces the FIFO order with a *score* per card:

    score = wait_weight    * minutes the card waited in the pool
          + size_weight    * size points of the card (a card marked 90 gets a bonus)
          + backend_weight * backend match points (own backend > unpinned > fallback)
          + starvation lift (a card that waited past the threshold is lifted)

The order only decides *which* card a free lane takes.  It never relaxes the backend
constraints (``backend:<name>`` pins, ``strict-backend``, the ``never_take_re`` rules)
and never relaxes the capacity checks (hot-file limits, lane occupancy): those stay
in the dispatcher (``engine.py``).

A card's own size is read from its budget marker:

* a label ``budget:90`` (``priority.budget_label_prefix``), or
* a body line ``预算：90`` / ``Budget: 90`` (``priority.budget_line_re``).

A card without a marker has no size: it gets no size points and no big-card bonus,
so the marker is what makes a card a big card.

Pure functions only, standard library only (Python 3.9+), so the module can be unit
tested offline and replayed on a recorded pool sequence (``replay_pool``).
"""

from __future__ import annotations

import datetime
import re
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_WEIGHTS",
    "weights",
    "label_names",
    "budget_minutes",
    "created_at",
    "waited_seconds",
    "size_points",
    "backend_match",
    "score_card",
    "sort_key",
    "fifo_key",
    "replay_pool",
]

# The knobs of the scoring.  Every one of them is configurable in the dispatch
# config (``priority`` section); the defaults are the values the fleet runs with.
DEFAULT_WEIGHTS: Dict[str, Any] = {
    "enabled": True,
    "wait_weight": 1.0,             # points per minute the card waited in the pool
    "size_weight": 1.0,             # points per size unit above the small-card baseline
    "size_unit_minutes": 30.0,      # one size unit: a 30-minute card is the baseline
    "big_budget_minutes": 90.0,     # a card at or above this budget is a big card
    "big_bonus": 12.0,              # extra points for a big card
    "backend_weight": 12.0,         # points for an exact backend match
    "unpinned_ratio": 0.5,          # share of backend_weight for an unpinned card
    "fallback_ratio": 0.1,          # share of backend_weight for a cross-backend fallback
    "starvation_threshold_s": 1800.0,   # a card that waited longer than this is lifted
    "starvation_boost": 60.0,       # points added to a lifted card
    "starvation_lift_big_cards": False,  # lift big cards too (they are favoured anyway)
    "budget_label_prefix": "budget:",      # label that carries the card budget
    "budget_line_re": r"^\s*(?:预算|Budget)\s*[:：]\s*(\d+)",  # body line that carries it
}

_LINE_CACHE: Dict[str, "re.Pattern"] = {}


def _line_re(pattern: str) -> "re.Pattern":
    """Compile (and cache) a body-line pattern; the lines are matched multiline."""
    compiled = _LINE_CACHE.get(pattern)
    if compiled is None:
        compiled = re.compile(pattern, re.M | re.I)
        _LINE_CACHE[pattern] = compiled
    return compiled


def weights(cfg=None) -> Dict[str, Any]:
    """Merge a configured weight set over the defaults (unknown keys are ignored)."""
    out = dict(DEFAULT_WEIGHTS)
    if not isinstance(cfg, dict):
        return out
    for key, value in cfg.items():
        if key not in out:
            continue
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def label_names(card) -> List[str]:
    """The label names of one card (strings or ``{"name": ...}`` dicts)."""
    names = []
    for label in (card or {}).get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def budget_minutes(card, cfg=None) -> Optional[int]:
    """The budget a card asks for, in minutes (``None`` when the card does not say).

    The card's own ``budget`` field wins; otherwise the ``budget:<minutes>`` label
    and then the ``预算：<minutes>`` body line are read.
    """
    card = card or {}
    w = weights(cfg)
    value = card.get("budget")
    if isinstance(value, bool):
        value = None
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    prefix = str(w["budget_label_prefix"])
    for name in label_names(card):
        if name.casefold().startswith(prefix.casefold()):
            raw = name[len(prefix):].strip()
            if raw.isdigit():
                return int(raw)
    match = _line_re(str(w["budget_line_re"])).search(card.get("body") or "")
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None
    return None


def created_at(value) -> Optional[float]:
    """The creation time of a card as epoch seconds (``None`` when unknown).

    GitHub hands out ISO-8601 timestamps (``2026-11-02T09:15:00Z``); a card that was
    created long ago but only labelled ready now has waited for minutes, not for days,
    so the creation time is only a fallback for the in-pool wait.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


def waited_seconds(card, now) -> float:
    """How long the card has been waiting in the pool (seconds).

    ``waited_s`` is what the dispatcher measured (the time since the card entered the
    ready pool); ``created_at`` is the fallback for a card the dispatcher never saw
    before.
    """
    card = card or {}
    value = card.get("waited_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = None
    if value is None:
        born = card.get("created_at")
        if isinstance(born, (int, float)) and not isinstance(born, bool):
            value = float(now) - float(born)
    if value is None:
        return 0.0
    return max(0.0, float(value))


def size_points(budget, cfg=None) -> Tuple[float, bool]:
    """The size points of a card and whether it is a big card.

    The points are the units the card has above the small-card baseline (a 30-minute
    card is the baseline and scores nothing extra), so a card marked ``90`` scores two
    units plus the big-card bonus.
    """
    w = weights(cfg)
    if not budget:
        return 0.0, False
    unit = float(w["size_unit_minutes"]) or 30.0
    points = max(0.0, float(budget) / unit - 1.0) * float(w["size_weight"])
    big = float(budget) >= float(w["big_budget_minutes"])
    if big:
        points += float(w["big_bonus"])
    return points, big


def _fallback_order(backend, fallback) -> List[str]:
    """The fallback chain of a backend (``fallback`` maps a backend to its fallbacks)."""
    table = fallback if isinstance(fallback, dict) else {}
    order = table.get(backend)
    if not order:
        for key, value in table.items():
            if str(key).casefold() == str(backend or "").casefold():
                order = value
                break
    return [str(name) for name in (order or [])]


def _fallback_index(pin, backend, fallback) -> int:
    """How far a pinned backend is down the fallback chain of the lane's backend."""
    order = _fallback_order(backend, fallback)
    for index, name in enumerate(order):
        if name.casefold() == str(pin or "").casefold():
            return index
    return len(order)


def backend_match(card, backend, cfg=None, fallback=None) -> Tuple[float, str]:
    """How well a card fits the lane's backend: the points and the reason.

    An exact pin is worth the full ``backend_weight``, an unpinned card half of it and
    a cross-backend fallback a tenth of it, less the further down the fallback chain
    the pinned backend sits.
    """
    w = weights(cfg)
    weight = float(w["backend_weight"])
    pin = (card or {}).get("backend")
    if not pin:
        return weight * float(w["unpinned_ratio"]), "unpinned"
    if backend and str(pin).casefold() == str(backend).casefold():
        return weight, "match:" + str(pin)
    index = _fallback_index(pin, backend, fallback)
    points = weight * float(w["fallback_ratio"]) / (1.0 + index)
    return points, "fallback:%s(%d)" % (pin, index)


def score_card(card, backend, now, cfg=None, fallback=None) -> Dict[str, Any]:
    """Score one card for one lane.

    The result carries the total score and its breakdown, so the dispatcher can log
    why a card won the queue.
    """
    w = weights(cfg)
    card = card or {}
    waited = waited_seconds(card, now)
    wait_points = float(w["wait_weight"]) * (waited / 60.0)
    budget = budget_minutes(card, w)
    size, big = size_points(budget, w)
    back_points, match = backend_match(card, backend, w, fallback)
    threshold = float(w["starvation_threshold_s"])
    lift = 0.0
    starved = False
    if threshold > 0 and waited >= threshold and (not big or w["starvation_lift_big_cards"]):
        lift = float(w["starvation_boost"])
        starved = True
    return {
        "score": wait_points + size + back_points + lift,
        "wait": wait_points,
        "size": size,
        "backend": back_points,
        "starvation": lift,
        "waited_s": waited,
        "budget": budget,
        "big": big,
        "backend_match": match,
        "starved": starved,
    }


def sort_key(card, backend, now, cfg=None, fallback=None):
    """The dispatch sort key: the score first (descending), then priority and number."""
    score = score_card(card, backend, now, cfg, fallback)["score"]
    return (-score, (card or {}).get("prio", 99), (card or {}).get("n", 0))


def fifo_key(card, backend, cfg=None, fallback=None):
    """The order the pool used before the score: own backend, unpinned, fallback chain.

    Kept for the comparison in ``replay_pool`` and as the fallback order of the
    dispatcher (``priority.enabled: false``).
    """
    card = card or {}
    pin = card.get("backend")
    if pin and backend and str(pin).casefold() == str(backend).casefold():
        group = 0
    elif not pin:
        group = 1
    else:
        group = 2 + _fallback_index(pin, backend, fallback)
    return (group, card.get("prio", 99), card.get("n", 0))


def replay_pool(sequence, backend="pi", lanes_per_tick=1, cfg=None, fallback=None,
                mode="score") -> Dict[str, Any]:
    """Replay a recorded pool sequence and report how long every card waited.

    ``sequence`` is a list of ticks; every tick is ``{"at": <epoch seconds>,
    "cards": [<card record>, ...]}`` and lists the cards that sit in the pool at that
    moment.  A card's wait is measured from the first tick it appears in.  Every tick
    frees ``lanes_per_tick`` lanes, and each free lane takes the card with the best
    order.

    ``mode`` is ``"score"`` (the priority score) or ``"fifo"`` (the old order), so
    both can be measured on the same recorded pool.

    The result carries the dispatched cards with their wait, the waits of the big and
    of the small cards, their median / maximum and the cards that never got a lane.
    """
    pool: Dict[Any, Dict[str, Any]] = {}
    arrived: Dict[Any, float] = {}
    dispatched: List[Dict[str, Any]] = []
    for tick in sequence or []:
        at = float((tick or {}).get("at") or 0.0)
        for card in (tick or {}).get("cards") or []:
            number = card.get("n")
            if number is None or number in pool:
                continue
            pool[number] = dict(card)
            arrived[number] = at
        for number, card in pool.items():     # every card ages while it waits
            card["waited_s"] = at - arrived[number]
        for _ in range(max(0, int(lanes_per_tick))):
            if not pool:
                break
            if mode == "fifo":
                pick = min(pool.values(), key=lambda card: fifo_key(card, backend, cfg, fallback))
            else:
                pick = min(pool.values(), key=lambda card: sort_key(card, backend, at, cfg, fallback))
            number = pick.get("n")
            score = score_card(pick, backend, at, cfg, fallback)["score"]
            budget = budget_minutes(pick, cfg)
            dispatched.append({
                "n": number,
                "at": at,
                "wait_s": at - arrived[number],
                "budget": budget,
                "big": size_points(budget, cfg)[1],
                "score": score,
            })
            del pool[number]
    big_waits = [row["wait_s"] for row in dispatched if row["big"]]
    small_waits = [row["wait_s"] for row in dispatched if not row["big"]]
    return {
        "mode": mode,
        "dispatched": dispatched,
        "big_waits": big_waits,
        "small_waits": small_waits,
        "big_wait_median": statistics.median(big_waits) if big_waits else None,
        "small_wait_max": max(small_waits) if small_waits else None,
        "left": sorted(pool),
    }
