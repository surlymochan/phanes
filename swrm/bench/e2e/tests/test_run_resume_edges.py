"""Malformed resume rows should not prevent later runs from being resumed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from run import existing_keys  # noqa: E402


@pytest.mark.parametrize(
    "bad_row",
    [
        {"type": "run", "task": "first", "arm": "baseline", "repeat": "not-an-integer"},
        ["not", "an", "object"],
        {"type": "run", "task": "missing-repeat", "arm": "baseline"},
        {"type": "run", "task": "first", "arm": "baseline", "repeat": None},
        {"type": "run", "task": [], "arm": "baseline", "repeat": 1},
        {"type": "run", "task": "first", "arm": {}, "repeat": 1},
        {"type": "run", "task": "first", "arm": "baseline", "repeat": 1.5},
        {"type": "run", "task": "first", "arm": "baseline", "repeat": True},
    ],
    ids=["non-integer-repeat", "non-object-json", "missing-field", "null-repeat",
         "list-task", "object-arm", "fractional-repeat", "boolean-repeat"],
)
def test_malformed_resume_rows_are_skipped(tmp_path: Path, bad_row: object) -> None:
    path = tmp_path / "resume.jsonl"
    rows = [
        {"type": "run", "task": "before", "arm": "baseline", "repeat": 1},
        bad_row,
        {"type": "run", "task": "after", "arm": "baseline", "repeat": 2},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    assert existing_keys(path) == {
        ("before", "baseline", 1),
        ("after", "baseline", 2),
    }


def test_integer_string_repeat_remains_resumable(tmp_path: Path) -> None:
    path = tmp_path / "resume.jsonl"
    path.write_text(json.dumps({"task": "task", "arm": "baseline", "repeat": "2"}), encoding="utf-8")

    assert existing_keys(path) == {("task", "baseline", 2)}
