"""Carry risk-blocked operations into the next dispatch attempt.

A lane run that was stopped by the risk guard writes the blocked operation into
``result.json`` (``risk_blocks``: a list of ``{"risk", "command"}`` pairs, the
command already redacted).  The dispatcher keeps those pairs per card and renders
them into the next task so the next lane does not run the same command again.

Refs #1597.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

MAX_NOTES = 5
COMMAND_LIMIT = 120
HEADING = "上次被风控拦截的操作（不要再做，换只读替代）"


def extract(result: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Return the blocked ``(risk, command)`` pairs of a result payload."""
    notes = []
    for block in (result or {}).get("risk_blocks") or []:
        if not isinstance(block, Mapping):
            continue
        risk, command = block.get("risk"), block.get("command")
        if not isinstance(risk, str) or not isinstance(command, str):
            continue
        note = {"risk": risk.strip()[:COMMAND_LIMIT], "command": command.strip()[:COMMAND_LIMIT]}
        if note["risk"] and note["command"] and note not in notes:
            notes.append(note)
    return notes


def accumulate(existing: Iterable[Mapping[str, str]] | None, result: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Merge the blocked operations of ``result`` into the stored ones.

    Duplicates are dropped and only the last :data:`MAX_NOTES` pairs are kept,
    so a card that is requeued again and again does not grow without bound.
    """
    notes = []
    for note in list(existing or []) + extract(result):
        pair = {"risk": str(note.get("risk", "")).strip()[:COMMAND_LIMIT],
                "command": str(note.get("command", "")).strip()[:COMMAND_LIMIT]}
        if pair["risk"] and pair["command"] and pair not in notes:
            notes.append(pair)
    return notes[-MAX_NOTES:]


def render(notes: Iterable[Mapping[str, str]] | None) -> str:
    """Render the stored notes as a task section (empty when there are none)."""
    clean = accumulate(notes, None)
    if not clean:
        return ""
    return "\n\n## " + HEADING + "\n" + "\n".join(
        f"- {note['risk']}: `{note['command']}`" for note in clean
    ) + "\n"
