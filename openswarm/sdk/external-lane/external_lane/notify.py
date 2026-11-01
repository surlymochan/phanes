"""Event-driven wake-ups: post a short notice on an agent's inbox PR when a card becomes ready for it.

Runs in GitHub Actions on `issues: [opened, reopened, labeled]` (.github/workflows/external-lane-notify.yml):

    python3 -m external_lane notify --event "$GITHUB_EVENT_PATH"

The allowlist is the set of open inbox PRs (head `external-inbox/<name>`, created only for allowlisted names by
`openswarm external init`): a card pinned to a name without an inbox is ignored. The same card is announced at most
once per 24 h per inbox (a hidden marker in the comment). Needs only `pull-requests: write` + read access to issues.
"""
from __future__ import annotations

import json
import time

from . import contract as C


def decide(issue, inbox, recent_comments, now=None):
    """-> (action, detail). action is 'comment' (detail = body) or 'skip' (detail = reason)."""
    if not issue or "pull_request" in issue or issue.get("state", "open") != "open":
        return "skip", "not an open issue"
    agent = C.notify_target(C.label_names(issue))
    if not agent:
        return "skip", "not a ready card for an external agent"
    if not inbox:
        return "skip", f"no inbox PR for {agent} (not allowlisted)"
    marker = C.notify_marker(issue["number"])
    now = time.time() if now is None else now
    for c in recent_comments or []:
        if marker in (c.get("body") or "") and now - _ts(c.get("created_at")) < C.NOTIFY_DEDUP_S:
            return "skip", f"#{issue['number']} already announced to {agent} within 24h"
    title = (issue.get("title") or "").replace("\n", " ")[:120]
    kind = "rework" if C.rework_target(issue.get("body") or "") else "new card"
    body = (f"Wake-up for **{agent}**: {kind} #{issue['number']} is `lane-ready` — {title}\n{issue.get('html_url', '')}\n\n"
            f"Run `openswarm external check --agent {agent}` (or the GitHub-only rule in the guide), then one round.\n{marker}")
    return "comment", body


def _ts(value):
    try:
        import calendar
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0


def find_inbox(gh, agent):
    owner = gh.repo.split("/")[0]
    rows = gh.request("GET", gh.r(f"/pulls?state=open&head={owner}:{C.inbox_branch(agent)}")) or []
    return rows[0] if rows else None


def run(gh, event, now=None, log=print):
    """Handle one `issues` event payload. Returns (action, detail)."""
    issue = (event or {}).get("issue") or {}
    agent = C.notify_target(C.label_names(issue)) if issue else None
    inbox = find_inbox(gh, agent) if agent else None
    recent = []
    if inbox:
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((time.time() if now is None else now) - C.NOTIFY_DEDUP_S))
        recent = gh.request("GET", gh.r(f"/issues/{inbox['number']}/comments?since={since}&per_page=100")) or []
    action, detail = decide(issue, inbox, recent, now)
    if action == "comment":
        gh.comment(inbox["number"], detail)
        detail = f"announced #{issue['number']} on inbox PR #{inbox['number']}"
    log(json.dumps({"action": action, "detail": detail}))
    return action, detail
