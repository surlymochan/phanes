#!/usr/bin/env python3
"""Offline contract check for result idempotency keys bound to claim rounds (Refs #953)."""

import hashlib
import json
import re
from typing import Any


RESULT_RE = re.compile(r"```openswarm-result\s*\n(.*?)\n```", re.S)


def result_key(issue: int, claim_id: int, run_id: str) -> str:
    """Derive the stable result idempotency key for one claim round."""
    material = f"issue:{issue}:claim:{claim_id}:run:{run_id}".encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def fake_claim(issue: int, claim_id: int, worker: str, round_no: int) -> dict[str, Any]:
    return {"issue": issue, "id": claim_id, "worker": worker, "round": round_no}


def fake_result(claim: dict[str, Any], status: str = "done") -> dict[str, Any]:
    run_id = f"round-{claim['round']}"
    return {
        "status": status,
        "run_id": run_id,
        "claim_round": claim["round"],
        "idempotency_key": result_key(claim["issue"], claim["id"], run_id),
        "summary": f"result from {claim['worker']}",
    }


def result_comment(payload: dict[str, Any]) -> str:
    return "```openswarm-result\n" + json.dumps(payload, sort_keys=True) + "\n```"


def parse_result_for_claim(comment: str, claim: dict[str, Any]) -> dict[str, Any] | None:
    """Accept a result only when its round and idempotency key match the winner."""
    match = RESULT_RE.fullmatch(comment.strip())
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    expected_run = f"round-{claim['round']}"
    expected_key = result_key(claim["issue"], claim["id"], expected_run)
    if (payload.get("run_id") != expected_run
            or payload.get("claim_round") != claim["round"]
            or payload.get("idempotency_key") != expected_key):
        return None
    return payload


def main() -> None:
    issue = 953
    first_claim = fake_claim(issue, 101, "worker-a", 1)
    second_claim = fake_claim(issue, 202, "worker-b", 2)
    first_comment = result_comment(fake_result(first_claim))
    second_comment = result_comment(fake_result(second_claim))

    # Before takeover, the first round's result is valid and retries are idempotent.
    assert parse_result_for_claim(first_comment, first_claim)["claim_round"] == 1
    assert parse_result_for_claim(first_comment, first_claim) == parse_result_for_claim(first_comment, first_claim)
    assert result_key(issue, first_claim["id"], "round-1") == fake_result(first_claim)["idempotency_key"]

    # After takeover, a delayed result from round 1 must not close round 2.
    assert parse_result_for_claim(first_comment, second_claim) is None
    assert parse_result_for_claim(second_comment, second_claim)["claim_round"] == 2
    assert first_claim["id"] != second_claim["id"]

    # A forged round/key pair is rejected even if the worker name is reused.
    forged = {**fake_result(first_claim), "claim_round": 2}
    assert parse_result_for_claim(result_comment(forged), second_claim) is None
    print("ok: result idempotency keys are bound to claim rounds; stale results are ignored")


if __name__ == "__main__":
    main()
