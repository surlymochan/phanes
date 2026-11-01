"""Failure retry policy per error class (pure module, no CLI wiring).

A dispatch run ends with an error class (``stalled``, ``cli_too_old``,
``exec_error``, ...).  The retry table tells the dispatcher what to do with the
issue for every class::

    {
        "stalled": "requeue",
        "cli_too_old": "pause_lane",
        "exec_error": {"max": 2, "then": "lane_failed"},
        "default": "close",
    }

A policy is either one action -- ``requeue``, ``reroute``, ``pause_lane``,
``lane_failed`` or ``close`` -- or a bounded retry ``{"max": N, "then": ACTION}``.
The bounded form requeues while fewer than ``N`` attempts were made and switches
to ``then`` as soon as ``N`` attempts have been made.  ``reroute`` re-dispatches
once on a fallback backend; the caller picks that backend.

An error class that is not in the table falls back to the ``default`` entry.  A
table without a default closes the issue: an unknown failure is not something to
retry blindly.  ``DEFAULT_TABLE`` is the project default; ``merge_table`` applies
a project override on top of it.  A ``stopped``/``partial`` run without an error
class counts as ``exec_error``.

``decide`` returns ``(action, counted)``.  ``counted`` is False for error classes
in ``UNCOUNTED_CLASSES`` (a VM pause killed by the watchdog is not a failed try,
and a quota failure must not burn a retry).

The error class is normalized before the table lookup: case and hyphen
differences do not matter (``RATE-LIMIT`` is ``rate_limit``) and the known
aliases of a class are folded into their canonical name -- ``rate_limit``,
``usage_limit``, ``quota_exhausted`` and ``insufficient_quota`` are all
``quota``, ``budget_exceeded_daily`` is ``budget_exceeded`` (#1540).

The module is pure: it reads no files, touches no labels and knows nothing
about lanes, so the wiring stays with the caller (#1365).

Refs #1401 (part of #1365), #1540.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

#: The actions a policy may ask for.
ACTIONS = ("requeue", "reroute", "pause_lane", "lane_failed", "close")

#: The table key used for error classes without their own policy.
DEFAULT_CLASS = "default"

#: Run statuses that count as ``exec_error`` when no error class was recorded.
EXEC_ERROR_STATUSES = ("stopped", "partial")

#: Error classes whose attempts do not count towards the retry limit.
UNCOUNTED_CLASSES = frozenset({"vm_paused", "quota"})

#: Reported error classes that are another class under a different name.
ERROR_CLASS_ALIASES: Dict[str, str] = {
    "rate_limit": "quota",
    "usage_limit": "quota",
    "quota_exhausted": "quota",
    "insufficient_quota": "quota",
    "budget_exceeded_daily": "budget_exceeded",
}

#: The project default retry table.
DEFAULT_TABLE: Dict[str, Any] = {
    "stalled": "requeue",
    "risk_blocked": {"max": 1, "then": "reroute"},
    "exec_error": {"max": 2, "then": "lane_failed"},
    "vm_paused": "requeue",
    "cli_too_old": "pause_lane",
    "quota": "pause_lane",
    DEFAULT_CLASS: "lane_failed",
}


def _action_names() -> str:
    """The known actions, for error messages."""
    return ", ".join(ACTIONS)


def _normalize_form(raw: str) -> str:
    """Return the class with a stable spelling: lower case, underscores."""
    return raw.strip().lower().replace("-", "_")


def normalize_error_class(raw: Any) -> Any:
    """Return the canonical error class for ``raw``.

    The reported class is normalized before the table lookup: case and hyphen
    differences do not matter (``RATE-LIMIT`` is ``rate_limit``) and the known
    aliases of a class are folded into their canonical name -- ``rate_limit``,
    ``usage_limit``, ``quota_exhausted`` and ``insufficient_quota`` are all
    ``quota``, ``budget_exceeded_daily`` is ``budget_exceeded``.

    Args:
        raw: the reported error class.

    Returns:
        The canonical error class; a class without an alias is returned with
        its spelling normalized only.

    Raises:
        ValueError: the class is not a string.
    """
    if not isinstance(raw, str):
        raise ValueError(f"error_class must be a string, got {raw!r}")
    key = _normalize_form(raw)
    return ERROR_CLASS_ALIASES.get(key, key)


def _validate_action(value: Any, where: str) -> str:
    """Return the action, or raise ValueError for anything else."""
    if not isinstance(value, str) or value not in ACTIONS:
        raise ValueError(f"{where} must be one of {_action_names()}, got {value!r}")
    return value


def _validate_policy(policy: Any, error_class: str) -> None:
    """Raise ValueError unless the policy is an action or ``{"max", "then"}``."""
    if isinstance(policy, str):
        _validate_action(policy, f"policy for {error_class!r}")
        return
    if not isinstance(policy, dict) or set(policy) != {"max", "then"}:
        raise ValueError(
            f"policy for {error_class!r} must be an action or "
            f"{{max, then}}, got {policy!r}"
        )
    maximum = policy["max"]
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise ValueError(
            f"max for {error_class!r} must be a positive integer, got {maximum!r}"
        )
    _validate_action(policy["then"], f"then for {error_class!r}")


def _validate_table(table: Any) -> Dict[str, Any]:
    """Return the table after checking every entry; raise ValueError otherwise."""
    if not isinstance(table, dict) or not table:
        raise ValueError(f"table must be a non-empty dict, got {table!r}")
    for error_class, policy in table.items():
        if not isinstance(error_class, str) or not error_class:
            raise ValueError(
                f"table keys must be non-empty error class strings, "
                f"got {error_class!r}"
            )
        _validate_policy(policy, error_class)
    return table


def _validate_attempts(attempts: Any) -> int:
    """Return the attempt count; raise ValueError for anything but a count."""
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError(f"attempts must be a non-negative integer, got {attempts!r}")
    return attempts


def merge_table(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """Return ``base`` with the ``override`` entries replacing matching keys.

    Keys the override does not mention keep their base policy.  An override key
    is normalized before it is merged, so ``rate_limit`` replaces the canonical
    ``quota`` entry.  The result is validated like any table; raise ValueError
    if it is invalid.
    """
    if not isinstance(override, Mapping):
        raise ValueError(f"override must be a dict, got {override!r}")
    merged = dict(base)
    aliased = {}
    for error_class, policy in override.items():
        if not isinstance(error_class, str):
            continue  # a broken key is reported by the table validation
        canonical = normalize_error_class(error_class)
        if canonical != error_class:
            aliased[canonical] = policy
    merged.update(aliased)
    merged.update(override)
    return _validate_table(merged)


def _lookup_policy(policies: Mapping[str, Any], error_class: str) -> Any:
    """Return the policy for ``error_class``; ``None`` when the table has none.

    The class is looked up under its reported spelling first, so a table that
    mentions ``rate_limit`` wins over the canonical ``quota`` entry, and under
    its canonical name second.
    """
    alias = _normalize_form(error_class)
    if alias in policies:
        return policies[alias]
    canonical = normalize_error_class(error_class)
    if canonical in policies:
        return policies[canonical]
    return None


def decide(
    error_class: Any,
    attempts: int,
    table: Mapping[str, Any] = DEFAULT_TABLE,
    status: Any = None,
) -> Tuple[str, bool]:
    """Return the ``(action, counted)`` for a failed run.

    Args:
        error_class: the class the run ended with; unknown classes use the
            ``default`` policy of the table.  ``None`` or ``""`` with a
            ``stopped``/``partial`` status counts as ``exec_error``.  The class
            is normalized first, so ``rate_limit`` and the other quota aliases
            use the ``quota`` policy.
        attempts: how many attempts were already made for this issue.
        table: the retry table, see the module docstring for its shape.
        status: the run status, used only to classify a missing error class.

    Returns:
        A tuple of the action (one of ``ACTIONS``) and whether this failure
        counts as an attempt (False for ``UNCOUNTED_CLASSES``).

    Raises:
        ValueError: the table, the error class or the attempt count is invalid.
    """
    policies = _validate_table(table)
    _validate_attempts(attempts)
    if error_class in (None, "") and status in EXEC_ERROR_STATUSES:
        error_class = "exec_error"
    if not isinstance(error_class, str):
        raise ValueError(f"error_class must be a string, got {error_class!r}")

    counted = normalize_error_class(error_class) not in UNCOUNTED_CLASSES
    policy = _lookup_policy(policies, error_class)
    if policy is None:
        if DEFAULT_CLASS not in policies:
            return "close", counted
        policy = policies[DEFAULT_CLASS]

    if isinstance(policy, str):
        return policy, counted
    if attempts < policy["max"]:
        return "requeue", counted
    return policy["then"], counted
