"""Regression tests for malformed usage data in agent event streams."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.metrics import looks_like_usage, parse_agent_stream  # noqa: E402
from harness.swrm_finish import executor_calls  # noqa: E402


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


def test_malformed_executor_attempt_does_not_discard_later_valid_usage():
    for value in ({"nested": 1}, "", "bad", [], True, -1, 1.5, float("inf")):
        calls = executor_calls({"attempts": [
            {"meta": {"usage": {"inputTokens": value}}},
            {"meta": {"usage": {"inputTokens": 17}}},
        ]})

        assert len(calls) == 1
        assert calls[0]["usage"]["inputTokens"] == 17
        assert calls[0]["missing_usage"] is False


def test_malformed_executor_usage_is_missing_and_allows_fallback():
    malformed = {"inputTokens": "bad", "outputTokens": 3}
    assert executor_calls({"usage": malformed}) == []
    calls = executor_calls({
        "attempts": [{"meta": {"usage": malformed}}],
        "usage": {"inputTokens": 17},
    })
    assert len(calls) == 1
    assert calls[0]["source"] == "swrm-result:usage"
    assert calls[0]["usage"]["inputTokens"] == 17


def test_malformed_counter_types_are_missing_without_losing_valid_usage():
    for value in (True, False, -1, 1.5, "1.5", float("nan"), float("inf")):
        usage = {"inputTokens": value, "outputTokens": 3}
        assert looks_like_usage(usage) is False
        assert parse_agent_stream(_result(usage), "bad.jsonl")["missing_usage"] is True

    for value in (0, 17, 17.0, "17"):
        usage = {"inputTokens": value}
        assert looks_like_usage(usage) is True
        call = parse_agent_stream(_result(usage), "valid.jsonl")
        assert call["missing_usage"] is False
        assert call["usage"]["inputTokens"] == int(value)
