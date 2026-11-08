"""Regression tests for malformed usage data in agent event streams."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.metrics import parse_agent_stream  # noqa: E402


def _result(usage: dict) -> str:
    return json.dumps({"type": "result", "subtype": "success", "usage": usage})


def test_malformed_usage_is_missing_instead_of_raising():
    for value in ({"nested": 1}, "", "not-a-number"):
        call = parse_agent_stream(_result({"inputTokens": value}), "bad.jsonl")

        assert call["saw_result"] is True
        assert call["missing_usage"] is True
        assert call["usage"]["inputTokens"] == 0


def test_malformed_result_does_not_discard_later_valid_result():
    text = "\n".join(
        [
            _result({"inputTokens": "bad"}),
            _result({"inputTokens": 17, "outputTokens": 3}),
        ]
    )

    call = parse_agent_stream(text, "mixed.jsonl")

    assert call["saw_result"] is True
    assert call["missing_usage"] is False
    assert call["usage"]["inputTokens"] == 17
    assert call["usage"]["outputTokens"] == 3
