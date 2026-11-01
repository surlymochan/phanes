"""The external-lane contract: the single source of truth for labels, branch names, comments and PR rules.

Both sides import this module:
  * the agent side (this SDK: `external_lane.agent` / `python3 -m external_lane`), and
  * the dispatcher side (`openswarm external tick`, openswarm/bin/openswarm_external.py).

Changing a label or the branch format here changes it for both. Pure functions only (no I/O).
"""
from __future__ import annotations

import re
import time

CONTRACT_VERSION = 1

# Issue state labels shared with the fleet dispatcher.
READY = "lane-ready"          # dispatchable
CLAIMED = "lane-claimed"      # an external agent owns it (in flight)
IN_PROGRESS = "in-progress"   # a fleet (ssh) lane owns it
PR_OPEN = "lane-pr"           # a PR exists; review/merge is the dispatcher's job
FAILED = "lane-failed"        # gave up; reason is in a comment
NEEDS_HUMAN = "needs-human"
BACKEND_PREFIX = "backend:"
# Self-registration: any `backend:<name>` whose name is not an internal (SSH fleet) backend is an external lane.
INTERNAL_BACKENDS = ("pi", "codex", "agy", "copilot")
POOL_NAME = "external"
POOL = BACKEND_PREFIX + POOL_NAME   # starter cards any external agent may claim (that is how a new name registers)
DEFAULT_AGENT = "chatgpt-agent"
# Inbox PR: a draft PR per agent that never merges; new cards are announced as comments on it, so agents whose platform
# only delivers pull-request events (PR comments/reviews/pushes) can be woken by an event instead of polling.
INBOX_LABEL = "external-inbox"
INBOX_PREFIX = "external-inbox/"
NOTIFY_DEDUP_S = 24 * 3600

LABEL_COLORS = {READY: "0e8a16", CLAIMED: "fbca04", PR_OPEN: "1d76db", FAILED: "b60205"}

MIN_EFFECTIVE_LINES = 30
CODE_EXT = (".py", ".ts", ".tsx", ".js", ".mjs", ".go", ".rs", ".sh")
DOC_EXT = (".md", ".txt", ".rst", ".adoc")

AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")
CLOSES_RE = re.compile(r"\b(?:Closes|Fixes|Resolves)\s+#(\d+)\b", re.I)
# Rework marker written by the dispatcher (review CHANGES, CI red, conflict): push to that PR's branch, no new PR.
REWORK_RE = re.compile(r"^\s*(?:重基PR|Rework PR)\s*[:：]\s*#(\d+)\s+(\S+)", re.M)
DEPENDS_RE = re.compile(r"^\s*(?:依赖|Depends on)\s*[:：](.*)$", re.M)
HOT_RE = re.compile(r"^\s*(?:热文件|Hot files)\s*[:：](.*)$", re.M)
VERIFY_RE = re.compile(r"^\s*(?:验收|Verify|Acceptance)\s*[:：].*`([^`]+)`", re.M)


def check_agent(agent: str) -> str:
    agent = str(agent or "").strip()
    if not AGENT_RE.fullmatch(agent):
        raise ValueError(f"agent name must match {AGENT_RE.pattern}: {agent!r}")
    if agent in INTERNAL_BACKENDS or agent == POOL_NAME:
        raise ValueError(f"agent name {agent!r} is reserved (internal backends: {', '.join(INTERNAL_BACKENDS)}; pool: {POOL_NAME})")
    return agent


def external_name(label: str, internal=INTERNAL_BACKENDS):
    """`backend:<name>` -> name when it is an external lane (not internal, not the pool), else None."""
    if not isinstance(label, str) or not label.startswith(BACKEND_PREFIX):
        return None
    name = label[len(BACKEND_PREFIX):]
    return name if name not in internal and name != POOL_NAME and AGENT_RE.fullmatch(name) else None


def fleet_must_skip(labels, internal=INTERNAL_BACKENDS) -> bool:
    """True when an SSH-fleet dispatcher must leave the card alone: claimed, pooled, or pinned to an external name."""
    labels = set(labels)
    return CLAIMED in labels or POOL in labels or any(external_name(l, internal) for l in labels)


def backend_label(agent: str) -> str:
    return BACKEND_PREFIX + check_agent(agent)


def claim_comment(agent: str) -> str:
    return f"claimed by {check_agent(agent)}"


def branch_prefix(agent: str) -> str:
    return f"fleet/{check_agent(agent)}-"


def branch_name(agent: str, issue: int, now: float | None = None) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() if now is None else now))
    return f"{branch_prefix(agent)}issue{int(issue)}-{ts}"


BRANCH_RE = re.compile(r"^fleet/([a-z0-9][a-z0-9-]*?)-issue(\d+)-[0-9-]+$")


def parse_branch(ref: str):
    """`fleet/<agent>-issue<N>-<ts>` -> (agent, N); anything else -> None."""
    m = BRANCH_RE.match(ref or "")
    return (m.group(1), int(m.group(2))) if m else None


def pr_body(issue: int, agent: str, summary: str = "") -> str:
    text = f"Closes #{int(issue)}\n\nExternal lane **{check_agent(agent)}** (contract v{CONTRACT_VERSION})."
    return text + (f"\n\n{summary.strip()}" if summary and summary.strip() else "")


def linked_issue(body: str, head_ref: str = ""):
    """Issue a PR closes: `Closes #N` in the body wins, else the branch name."""
    m = CLOSES_RE.search(body or "")
    if m:
        return int(m.group(1))
    parsed = parse_branch(head_ref)
    return parsed[1] if parsed else None


def rework_target(body: str):
    m = REWORK_RE.search(body or "")
    return (int(m.group(1)), m.group(2)) if m else None


def label_names(issue) -> set:
    out = set()
    for label in (issue or {}).get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            out.add(name)
    return out


def claimable(issue, agent: str) -> bool:
    """Open issue (not a PR) labelled lane-ready + (backend:<agent> or the shared pool backend:external), unowned."""
    if "pull_request" in (issue or {}) or (issue or {}).get("state", "open") != "open":
        return False
    labels = label_names(issue)
    if READY not in labels or labels & {CLAIMED, IN_PROGRESS, PR_OPEN, FAILED, NEEDS_HUMAN}:
        return False
    pins = {l for l in labels if l.startswith(BACKEND_PREFIX)}
    return pins in ({backend_label(agent)}, {POOL})


def claim_labels(labels, agent: str | None = None) -> set:
    """Claiming swaps lane-ready for lane-claimed; a pool card is also re-pinned to backend:<agent>."""
    out = (set(labels) - {READY, FAILED}) | {CLAIMED}
    if agent:
        out = (out - {POOL}) | {backend_label(agent)}
    return out


def pr_opened_labels(labels) -> set:
    return (set(labels) - {READY, CLAIMED, FAILED}) | {PR_OPEN}


def failed_labels(labels) -> set:
    return (set(labels) - {READY, CLAIMED, PR_OPEN}) | {FAILED}


def requeue_labels(labels) -> set:
    return (set(labels) - {CLAIMED, FAILED}) | {READY}


def diff_weight(files):
    """files: [{filename, additions, deletions}] (GitHub pulls/N/files or git numstat). -> (effective, docs, other)."""
    eff = docs = other = 0
    for f in files or []:
        path = str(f.get("filename") or f.get("path") or "").lower()
        n = int(f.get("additions") or 0) + int(f.get("deletions") or 0)
        if path.endswith(DOC_EXT) or path.startswith("docs/") or "/docs/" in path:
            docs += n
        elif path.endswith(CODE_EXT) or "/tests/" in path or path.startswith("tools/"):
            eff += n
        else:
            other += n
    return eff, docs, other


def thin_reason(files, minimum: int = MIN_EFFECTIVE_LINES) -> str:
    """'' when the change is substantial enough; else why it is thin (same rule the fleet harvest uses)."""
    eff, docs, other = diff_weight(files)
    if eff + docs + other == 0:
        return "empty-diff"
    if eff < minimum:
        return f"effective<{minimum}: effective={eff} docs={docs} other={other}"
    if docs > eff and eff < minimum * 2:
        return f"mostly-docs: docs={docs} effective={eff}"
    return ""


def hot_files(body: str) -> set:
    m = HOT_RE.search(body or "")
    return {x.strip().strip("`") for x in re.split(r"[,，\s]+", m.group(1)) if x.strip()} if m else set()


def depends_on(body: str) -> list:
    m = DEPENDS_RE.search(body or "")
    return [int(x) for x in re.findall(r"#(\d+)", m.group(1))] if m else []


def verify_command(body: str):
    found = VERIFY_RE.findall(body or "")
    return found[-1] if found else None


def inbox_branch(agent: str) -> str:
    return INBOX_PREFIX + check_agent(agent)


def notify_marker(issue: int) -> str:
    return f"<!-- external-lane-notify:#{int(issue)} -->"


def notify_target(labels, internal=INTERNAL_BACKENDS):
    """Agent to wake for a card with these labels: lane-ready + exactly one external backend:<name>, not taken. Else None."""
    labels = set(labels)
    if READY not in labels or labels & {CLAIMED, IN_PROGRESS, PR_OPEN, FAILED, NEEDS_HUMAN}:
        return None
    names = {external_name(l, internal) for l in labels if l.startswith(BACKEND_PREFIX)}
    return names.pop() if len(names) == 1 and None not in names else None
