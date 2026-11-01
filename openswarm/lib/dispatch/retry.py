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

A policy is either one action -- ``requeue``, ``pause_lane``, ``lane_failed`` or
``close`` -- or a bounded retry ``{"max": N, "then": ACTION}``.  The bounded form
requeues while fewer than ``N`` attempts were made and switches to ``then`` as
soon as ``N`` attempts have been made.

An error class that is not in the table falls back to the ``default`` entry.  A
table without a default closes the issue: an unknown failure is not something to
retry blindly.

The module is pure: it reads no files, touches no labels and knows nothing
about lanes, so the wiring stays with the caller (#1365).

Refs #1401 (part of #1365).
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

#: The actions a policy may ask for.
ACTIONS = ("requeue", "pause_lane", "lane_failed", "close")

#: The table key used for error classes without their own policy.
DEFAULT_CLASS = "default"


def _action_names() -> str:
    """The known actions, for error messages."""
    return ", ".join(ACTIONS)


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


def decide(
    error_class: Any, attempts: int, table: Mapping[str, Any]
) -> Tuple[str, str]:
    """Return the ``(action, reason)`` for a failed run.

    Args:
        error_class: the class the run ended with; unknown classes use the
            ``default`` policy of the table.
        attempts: how many attempts were already made for this issue.
        table: the retry table, see the module docstring for its shape.

    Returns:
        A tuple of the action (``requeue``, ``pause_lane``, ``lane_failed`` or
        ``close``) and a human readable reason naming the class and the policy
        that decided.

    Raises:
        ValueError: the table, the error class or the attempt count is invalid.
    """
    policies = _validate_table(table)
    _validate_attempts(attempts)
    if not isinstance(error_class, str):
        raise ValueError(f"error_class must be a string, got {error_class!r}")

    if error_class in policies:
        selected = error_class
    elif DEFAULT_CLASS in policies:
        selected = DEFAULT_CLASS
    else:
        return (
            "close",
            f"error class {error_class!r}: no policy and no default, closing",
        )

    policy = policies[selected]
    if isinstance(policy, str):
        return policy, f"error class {error_class!r}: {selected} policy says {policy}"
    if attempts < policy["max"]:
        return (
            "requeue",
            f"error class {error_class!r}: attempt {attempts + 1} of "
            f"{policy['max']}, requeue",
        )
    return (
        policy["then"],
        f"error class {error_class!r}: retry limit {policy['max']} reached, "
        f"{policy['then']}",
    )
