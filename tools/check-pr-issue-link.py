#!/usr/bin/env python3
"""Find open fleet/brain PRs missing an issue reference in an offline JSON snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


CLOSING = re.compile(r"\b(?:Closes|Fixes|Resolves)\s+#([1-9]\d*)\b", re.I)
BRANCH = re.compile(r"fleet/[^/]+-issue([1-9]\d*)-.+")
REASON = "missing issue link: use Closes/Fixes/Resolves #N or fleet/<lane>-issue<N>-..."


def link(pr: dict) -> int | None:
    """Return the first closing reference, falling back to a fleet branch reference."""
    match = CLOSING.search(pr.get("body") or "")
    if match:
        return int(match.group(1))
    match = BRANCH.fullmatch((pr.get("head") or {}).get("ref", ""))
    return int(match.group(1)) if match else None


def audit(prs: list[dict]) -> list[dict]:
    """Report unlinked, non-draft fleet/brain PRs in stable number order."""
    problems = []
    for pr in sorted(prs, key=lambda item: item["number"]):
        head = (pr.get("head") or {}).get("ref", "")
        if pr.get("draft") or pr.get("state", "open") != "open":
            continue
        if head.startswith(("fleet/", "brain/")) and link(pr) is None:
            problems.append({"number": pr["number"], "head": head, "reason": REASON})
    return problems


def self_test() -> None:
    fixtures = [
        {"number": 1, "body": "Details\nCloses #11", "head": {"ref": "fleet/a-work"}},
        {"number": 2, "body": None, "head": {"ref": "fleet/a-issue12-stamp"}},
        {"number": 3, "body": "fIxEs #13", "head": {"ref": "brain/work"}},
        {"number": 4, "draft": True, "head": {"ref": "fleet/draft"}},
        {"number": 5, "head": {"ref": "feature/work"}},
        {"number": 6, "head": {"ref": "brain/unlinked"}},
    ]
    assert [link(pr) for pr in fixtures[:3]] == [11, 12, 13]
    assert audit(fixtures) == [{"number": 6, "head": "brain/unlinked", "reason": REASON}]
    print("ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prs", type=Path, help="JSON array from gh api repos/O/R/pulls?state=open")
    source.add_argument("--self-test", action="store_true", help="run built-in offline fixtures")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    try:
        prs = json.loads(args.prs.read_text(encoding="utf-8"))
        if not isinstance(prs, list) or any(not isinstance(pr, dict) for pr in prs):
            raise ValueError("expected a JSON array of PR objects")
        problems = audit(prs)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    if args.as_json:
        print(json.dumps(problems, ensure_ascii=False))
    else:
        print("PR     HEAD  REASON")
        for problem in problems:
            print(f"#{problem['number']:<5} {problem['head']}  {problem['reason']}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
