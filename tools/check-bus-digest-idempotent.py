#!/usr/bin/env python3
"""Check that a repeated task-digest comment cannot change claim arbitration."""

import hashlib
import json


def task_digest(task):
    payload = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def digest_comment(task):
    return f"openswarm-task-digest: sha256:{task_digest(task)}"


def trusted_digest(comments, task, allowed_planners):
    """The first digest from a trusted planner is authoritative."""
    digests = [comment for comment in comments
               if comment["author"] in allowed_planners
               and comment["body"].startswith("openswarm-task-digest: sha256:")]
    return bool(digests) and digests[0]["body"] == digest_comment(task)


def winning_claim(comments):
    claims = [comment for comment in comments if comment["body"].startswith("/claim ")]
    return min(claims, key=lambda comment: comment["id"])["body"]


def main():
    task = {"run_id": "digest-idempotent", "repo": "org/target", "base": "a" * 40}
    planner = {"id": 1, "author": "planner", "body": digest_comment(task)}
    before = [planner, {"id": 2, "author": "executor-a", "body": "/claim executor-a"},
              {"id": 3, "author": "executor-b", "body": "/claim executor-b"}]
    after = [before[0], {"id": 4, "author": "planner", "body": digest_comment(task)}, *before[1:]]

    assert trusted_digest(before, task, {"planner"})
    assert trusted_digest(after, task, {"planner"})
    assert winning_claim(before) == winning_claim(after) == "/claim executor-a"
    print("ok: repeated identical task digest leaves claim arbitration unchanged")


if __name__ == "__main__":
    main()
