"""Cross backend fallback mapping for dispatch (pure module, no CLI wiring).

A dispatch run is pinned to one backend.  When that backend cannot serve the
issue the dispatcher may try another one, but only after the pinned backend
had its grace period.  The configuration says how long to wait and which
backends to try in which order::

    {"wait_s": 600, "map": {"codex": ["pi"], "agy": ["codex", "pi"]}}

``wait_s`` is the number of seconds the pinned backend is given before a
fallback is considered; ``map`` holds the fallback chain per pinned backend,
in the order the backends are tried.

The rules:

* while the pinned backend did not wait ``wait_s`` seconds yet, only the
  pinned backend is returned;
* once the wait is over the mapped backends are appended in configuration
  order;
* a paused backend is skipped, a paused backend must not get new work;
* ``strict`` switches the fallback off completely, whatever the wait is.

The module is pure: it reads no configuration file, knows nothing about lanes
and never talks to a backend, so the wiring stays with the caller (#1361).

Refs #1397 (part of #1361).
"""
from __future__ import annotations

from collections import abc
from typing import Any, Iterable, List, Mapping, Sequence

#: Configuration key holding the grace period of the pinned backend.
WAIT_KEY = "wait_s"
#: Configuration key holding the fallback chain per pinned backend.
MAP_KEY = "map"
#: Configuration key that switches the fallback off.
STRICT_KEY = "strict"


def _wait_seconds(cfg: Mapping[str, Any]) -> float:
    """The grace period in seconds; a missing or broken value means no wait."""
    value = cfg.get(WAIT_KEY, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _chain(cfg: Mapping[str, Any], pinned: str) -> Sequence[str]:
    """The fallback chain configured for ``pinned``; empty when there is none."""
    mapping = cfg.get(MAP_KEY)
    if not isinstance(mapping, abc.Mapping):
        return ()
    chain = mapping.get(pinned)
    if not isinstance(chain, (list, tuple)):
        return ()
    return chain


def fallback_backends(
    pinned: str,
    waited_s: float,
    cfg: Mapping[str, Any],
    paused_backends: Iterable[str] = (),
) -> List[str]:
    """Return the pinned backend followed by the backends it may fall back to.

    Args:
        pinned: the backend the run is pinned to; it is always the first entry
            of the result.
        waited_s: how long the pinned backend waited already, in seconds.
        cfg: the fallback configuration, see the module docstring for its
            shape.  A configuration without ``wait_s`` has no grace period, so
            the fallbacks are eligible right away.
        paused_backends: the backends that are paused; a paused backend is
            skipped in the fallback chain, the pinned backend stays in the
            result.

    Returns:
        The backends to try, in order: the pinned backend first, then its
        fallbacks in configuration order.  Only the pinned backend is returned
        while the wait is not over, when the configuration is strict or when
        no fallback is configured for the pinned backend.
    """
    result: List[str] = [pinned]
    if not isinstance(cfg, abc.Mapping):
        return result
    if cfg.get(STRICT_KEY):
        return result
    try:
        waited = float(waited_s)
    except (TypeError, ValueError):
        return result
    if waited < _wait_seconds(cfg):
        return result

    paused = set(paused_backends or ())
    seen = {pinned}
    for backend in _chain(cfg, pinned):
        if not isinstance(backend, str) or not backend:
            continue
        if backend in paused or backend in seen:
            continue
        result.append(backend)
        seen.add(backend)
    return result
