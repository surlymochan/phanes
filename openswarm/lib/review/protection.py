#!/usr/bin/env python3
"""Check a saved GitHub branch-protection response; never contact GitHub."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import sys
from urllib.parse import quote


def _enabled(value) -> bool:
    return value.get("enabled") is True if isinstance(value, dict) else value is True


def check_protection(protection: dict | None, context: str = "lane-review") -> list[dict]:
    """Check both status-check formats and enforcement for administrators."""
    if protection is None:
        return [{"level": "warn", "code": "unprotected",
                 "message": "Branch has no protection; review is not enforced."}]
    checks = protection.get("required_status_checks") or {}
    contexts = set(checks.get("contexts") or [])
    contexts.update(item["context"] for item in checks.get("checks") or [])
    findings = []
    if context not in contexts:
        findings.append({"level": "warn", "code": "missing_context",
                         "message": f"Required status checks do not include {context!r}."})
    if not _enabled(protection.get("enforce_admins")):
        findings.append({"level": "warn", "code": "admins_not_enforced",
                         "message": "Administrators can bypass branch protection."})
    return findings or [{"level": "ok", "code": "ok",
                         "message": f"{context!r} is required, including for administrators."}]


def _actors(value: dict) -> dict:
    """Translate GET's user/team/app objects to PUT's login/slug arrays."""
    return {
        kind: [item[field] if isinstance(item, dict) else item
               for item in value.get(kind, [])]
        for kind, field in (("users", "login"), ("teams", "slug"), ("apps", "slug"))
    }


def _payload(protection: dict | None, context: str) -> dict:
    protection = protection or {}
    status = protection.get("required_status_checks") or {}
    contexts = list(dict.fromkeys([*(status.get("contexts") or []), context]))
    required = {"strict": status.get("strict", False), "contexts": contexts}
    if "checks" in status:
        required["checks"] = [
            {key: item[key] for key in ("context", "app_id")
             if key in item and item[key] is not None}
            for item in status["checks"] or []
        ]
    reviews = protection.get("required_pull_request_reviews")
    if reviews is not None:
        reviews = {
            key: reviews[key] for key in (
                "dismiss_stale_reviews", "require_code_owner_reviews",
                "required_approving_review_count", "require_last_push_approval",
                "dismissal_restrictions", "bypass_pull_request_allowances",
            ) if key in reviews
        }
        for key in ("dismissal_restrictions", "bypass_pull_request_allowances"):
            if key in reviews:
                reviews[key] = _actors(reviews[key])
    restrictions = protection.get("restrictions")
    payload = {
        "required_status_checks": required,
        "enforce_admins": True,
        "required_pull_request_reviews": reviews,
        "restrictions": _actors(restrictions) if restrictions is not None else None,
    }
    for key in ("required_linear_history", "allow_force_pushes", "allow_deletions",
                "block_creations", "required_conversation_resolution", "lock_branch",
                "allow_fork_syncing"):
        if key in protection:
            payload[key] = _enabled(protection[key])
    return payload


def fix_command(repo, branch, protection, context="lane-review") -> str:
    """Generate a quoted PUT command with a merged, writable JSON payload only."""
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts) or not branch or not context:
        raise ValueError("repo must be OWNER/REPO; branch and context must not be empty")
    endpoint = "repos/{}/branches/{}/protection".format(
        "/".join(quote(part, safe="") for part in parts), quote(branch, safe=""))
    payload = json.dumps(_payload(protection, context), indent=2, ensure_ascii=True)
    return (f"gh api -X PUT {shlex.quote(endpoint)} --input - <<'OPENSWARM_PROTECTION_JSON'\n"
            f"{payload}\nOPENSWARM_PROTECTION_JSON")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", required=True, metavar="FILE", dest="json_file",
                        help="saved protection JSON; - reads stdin, null means unprotected")
    parser.add_argument("--context", default="lane-review")
    parser.add_argument("--repo", default="OWNER/REPO", help="target repository for the suggested command")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.json_file == "-" else Path(args.json_file).read_text(encoding="utf-8")
        protection = json.loads(raw)
        if protection is not None and not isinstance(protection, dict):
            raise ValueError("protection must be a JSON object or null")
        if not args.context:
            raise ValueError("context must not be empty")
        findings = check_protection(protection, args.context)
        command = fix_command(args.repo, args.branch, protection, args.context)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))
    for finding in findings:
        print(f"{finding['level']}: {finding['code']}: {finding['message']}")
    warned = any(finding["level"] == "warn" for finding in findings)
    if warned:
        print("\nSuggested fix (not executed; replace OWNER/REPO if omitted):")
        print(command)
    return int(warned)


if __name__ == "__main__":
    raise SystemExit(main())
