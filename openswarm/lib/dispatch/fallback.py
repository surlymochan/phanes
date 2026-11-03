"""Cross backend fallback mapping for dispatch (pure module, no CLI wiring).

A dispatch run is pinned to one backend.  When that backend cannot serve the
issue the dispatcher may try another one, but only after the pinned backend
had its grace period.  The configuration says how long to wait and which
backends to try in which order::

    {"wait_s": 600, "map": {"codex": ["pi"], "agy": ["codex", "pi"]}}

``wait_s`` is the number of seconds the pinned backend is given before a
fallback is considered; ``map`` holds the fallback chain per pinned backend,
in the order the backends are tried.

The openswarm policy carries the same mapping under ``dispatch.fallback`` and the
grace period under ``dispatch.fallback_wait_s``; ``policy_config`` turns a policy
into the configuration above.

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
from typing import Any, Dict, Iterable, List, Mapping, Sequence

#: Configuration key holding the grace period of the pinned backend.
WAIT_KEY = "wait_s"
#: Configuration key holding the fallback chain per pinned backend.
MAP_KEY = "map"
#: Configuration key that switches the fallback off.
STRICT_KEY = "strict"
#: Policy key holding the dispatch section with the fallback mapping.
POLICY_KEY = "dispatch"
#: Policy key holding the mapping inside the dispatch section.
POLICY_MAP_KEY = "fallback"
#: Policy key holding the grace period inside the dispatch section.
POLICY_WAIT_KEY = "fallback_wait_s"
#: Policy key that switches the fallback off inside the dispatch section.
POLICY_STRICT_KEY = "fallback_strict"


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


def configured(cfg: Mapping[str, Any]) -> bool:
    """Whether a fallback mapping is configured at all.

    Without a mapping the dispatchers keep their plain cross-backend behaviour: a
    card pinned to one backend may run on any other one.
    """
    if not isinstance(cfg, abc.Mapping):
        return False
    mapping = cfg.get(MAP_KEY)
    return isinstance(mapping, abc.Mapping) and bool(mapping)


def has_mapping(cfg: Mapping[str, Any], pinned: str) -> bool:
    """Whether the configuration maps a fallback chain for ``pinned``."""
    if not isinstance(cfg, abc.Mapping):
        return False
    mapping = cfg.get(MAP_KEY)
    return isinstance(mapping, abc.Mapping) and pinned in mapping


def policy_config(policy: Any) -> Dict[str, Any]:
    """Build the fallback configuration from an openswarm policy.

    The policy carries the mapping under ``dispatch.fallback`` (pinned backend ->
    the backends its cards may fall back to, in the order they are tried) and the
    grace period under ``dispatch.fallback_wait_s``.  ``dispatch.fallback_strict``
    switches the fallback off.  A policy without a dispatch section, or without a
    mapping, yields an empty configuration: no card falls back to another backend.

    The dispatch section may also be given at the top level of the policy; a file
    that only holds the fallback mapping is accepted as it is.
    """
    if not isinstance(policy, abc.Mapping):
        return {}
    section = policy.get(POLICY_KEY)
    if not isinstance(section, abc.Mapping):
        section = policy if POLICY_MAP_KEY in policy else {}
    mapping = section.get(POLICY_MAP_KEY)
    if not isinstance(mapping, abc.Mapping):
        return {}
    chains: Dict[str, List[str]] = {}
    for pinned, chain in mapping.items():
        if not isinstance(pinned, str) or not pinned:
            continue
        if not isinstance(chain, (list, tuple)):
            continue
        chains[pinned] = [backend for backend in chain if isinstance(backend, str) and backend]
    config: Dict[str, Any] = {"map": chains}
    if POLICY_WAIT_KEY in section:
        config[WAIT_KEY] = section[POLICY_WAIT_KEY]
    if section.get(POLICY_STRICT_KEY):
        config[STRICT_KEY] = True
    return config


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
