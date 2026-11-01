#!/usr/bin/env python3
"""Executable contract check for claim-time task JSON and digest mutation."""

import hashlib
import json


def digest(task):
    payload = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def claim_snapshot(task):
    return {"task": json.loads(json.dumps(task)), "digest": digest(task)}


def claim_is_unchanged(snapshot, task, digest_comment):
    expected = f"openswarm-task-digest: sha256:{snapshot['digest']}"
    return task == snapshot["task"] and digest_comment == expected


def assert_abort_on_mutation(before, after):
    snapshot = claim_snapshot(before)
    changed_digest_comment = f"openswarm-task-digest: sha256:{digest(after)}"
    assert not claim_is_unchanged(snapshot, after, changed_digest_comment)
    result = {"status": "failed", "aborted": True, "error_class": "claim_task_mutated"}
    assert result["aborted"] is True
    assert result["error_class"] == "claim_task_mutated"


def main():
    before = {"run_id": "sample-927", "repo": "org/target", "base": "a" * 40,
              "budget_minutes": 10}
    after = {**before, "budget_minutes": 11}
    snapshot = claim_snapshot(before)
    assert claim_is_unchanged(snapshot, before,
                              f"openswarm-task-digest: sha256:{snapshot['digest']}")
    assert_abort_on_mutation(before, after)
    print("ok: claim task JSON/digest mutation aborts with error_class=claim_task_mutated")


if __name__ == "__main__":
    main()
