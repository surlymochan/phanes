"""Fix-round cards: one card per issue and no stale cards in the pool (Refs #1713).

A *fix card* is the card the dispatcher queues for an issue that already has a pull
request.  A review round that returned ``CHANGES``, a red CI head and a conflicting
head all re-label the original issue ``lane-ready`` and add a section to its body
that tells the lane what to do.  The issue itself is the card, so every extra round
that is queued while the previous one is still around piles up in the pool.

Two rules keep the pool honest:

* **One card per issue.**  A new fix round is queued only when the issue has no card
  in flight: ``in-progress`` means a lane works on the previous round, ``lane-ready``
  means the previous round is still queued and nobody took it yet.  The next round is
  queued once the old card was finished (``lane-pr``) or failed (``lane-failed``).
* **No stale cards.**  A fix card whose pull request was merged (or closed) is
  obsolete, and so is a card of a closed issue.  The dispatcher drops the
  ``lane-ready`` label of such a card, so the pool watermark counts work that is
  still real instead of cards nobody is going to work on.

Everything here is pure: the caller passes the labels and the body of an issue and
gets a decision back.  Nothing here touches the network, the clock or the filesystem.
"""

import re

READY_LABEL = "lane-ready"
IN_PROGRESS_LABEL = "in-progress"

# The section titles the dispatcher writes into an issue body (the defaults of the
# dispatch config; a config may rename them).
DEFAULT_SECTION_TITLES = ("Lane review requested changes", "Auto fix CI (red)", "Auto rebase (conflict)")

_REBASE_LINE_RE = re.compile(r"^\s*(?:重基PR|Rebase-PR)\s*[:：]\s*#(\d+)\b", re.M | re.I)
_PR_REF_RE = re.compile(r"\bPR\s*#(\d+)\b", re.I)

__all__ = [
    "READY_LABEL",
    "IN_PROGRESS_LABEL",
    "DEFAULT_SECTION_TITLES",
    "section_titles",
    "fix_section",
    "is_fix_card",
    "source_pr",
    "blocking_label",
    "plan_fix_card",
    "stale_reason",
    "stale_cards",
    "audit_line",
]


def section_titles(templates):
    """The fix-round section titles of a dispatch config.

    ``templates`` is the ``templates`` block of the config; every section the
    dispatcher writes into an issue body marks the issue as a fix card.
    """
    titles = []
    for key in ("changes_section_title", "cifix_section_title", "rebase_section_title"):
        title = (templates or {}).get(key)
        if isinstance(title, str) and title.strip():
            titles.append(title.strip())
    return titles


def fix_section(body, titles=None):
    """Return ``(title, text)`` of the fix-round section of a body.

    ``(None, "")`` when the body carries no fix-round section, i.e. when the issue is
    a plain card and not a fix round.
    """
    text = body or ""
    for title in (titles if titles is not None else DEFAULT_SECTION_TITLES):
        if not isinstance(title, str) or not title.strip():
            continue
        head = re.compile(r"^#{1,6}[ \t]*" + re.escape(title.strip()) + r"[ \t]*$", re.M)
        match = head.search(text)
        if not match:
            continue
        rest = text[match.end():]
        nxt = re.search(r"^#{1,6}[ \t]+\S", rest, re.M)
        return title.strip(), rest[:nxt.start()] if nxt else rest
    return None, ""


def is_fix_card(body, titles=None):
    """Whether the body carries a fix-round section (the card came from the dispatcher)."""
    return fix_section(body, titles)[0] is not None


def source_pr(body, titles=None):
    """The pull request a fix card works on.

    The ``Rebase-PR: #12 branch`` line the dispatcher writes is authoritative; a
    ``PR #12`` reference inside the fix-round section is the fallback.  ``None`` when
    the body names no pull request.
    """
    text = body or ""
    match = _REBASE_LINE_RE.search(text)
    if match:
        return int(match.group(1))
    _, section = fix_section(text, titles)
    match = _PR_REF_RE.search(section)
    return int(match.group(1)) if match else None


def label_names(labels):
    """The label names of an issue (strings or ``{"name": ...}`` dicts)."""
    names = set()
    for label in labels or ():
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def blocking_label(labels, ready=READY_LABEL, in_progress=IN_PROGRESS_LABEL):
    """The label that shows a card of this issue is still in flight.

    ``in-progress`` wins: a lane works on the previous round right now.  ``lane-ready``
    comes second: the previous round is queued but nobody took it yet.  ``None`` when
    the issue has no card in flight (it was finished or failed).
    """
    names = label_names(labels)
    if in_progress in names:
        return in_progress
    if ready in names:
        return ready
    return None


def plan_fix_card(labels, body=None, titles=None, ready=READY_LABEL, in_progress=IN_PROGRESS_LABEL,
                  held=False):
    """Decide whether a new fix round may be queued for an issue.

    ``labels`` are the label names of the issue, ``body`` its body and ``titles`` the
    fix-round section titles of the config.  ``held`` says that a lane holds the issue
    even when the ``in-progress`` label is missing (a failed label write).

    The result is a dict:

    ``queue``
        Whether the fix round may be queued.
    ``reason``
        ``"queued"`` when it may, otherwise the reason it may not: ``"in-progress"``
        (a lane works on the previous round), ``"lane-ready"`` (the previous round is
        still queued) or ``"lane-held"`` (a lane holds the issue).
    ``blocking``
        The label that blocks the card, or ``None``.
    ``pr``
        The pull request the card works on, when the body names one.
    ``fix_card``
        Whether the issue already carries a fix-round section.
    """
    blocking = blocking_label(labels, ready, in_progress)
    if blocking is None and held:
        blocking = "lane-held"
    pr = source_pr(body, titles)
    card = is_fix_card(body, titles)
    if blocking:
        return {"queue": False, "reason": blocking, "blocking": blocking, "pr": pr, "fix_card": card}
    return {"queue": True, "reason": "queued", "blocking": None, "pr": pr, "fix_card": card}


def stale_reason(issue_state, pr_state):
    """Why a fix card is obsolete, or ``None`` when it is still real.

    ``issue-closed``: the issue itself is closed.  ``pr-merged`` / ``pr-closed``: the
    pull request the card works on is gone, so there is nothing left to fix.
    """
    if str(issue_state or "").upper() == "CLOSED":
        return "issue-closed"
    state = str(pr_state or "").upper()
    if state == "MERGED":
        return "pr-merged"
    if state == "CLOSED":
        return "pr-closed"
    return None


def stale_cards(cards, pr_states, titles=None):
    """The fix cards of a pool that are obsolete.

    ``cards`` are the issue rows of the pool (``{"number", "state", "labels", "body"}``)
    and ``pr_states`` maps a pull request number to its state (``"MERGED"``,
    ``"CLOSED"``, ``"OPEN"``).  The result is a list of ``{"number", "reason", "pr"}``
    dicts, one per card that has to leave the pool.
    """
    out = []
    for card in cards or ():
        if not isinstance(card, dict):
            continue
        body = card.get("body") or ""
        if not is_fix_card(body, titles):
            continue
        pr = source_pr(body, titles)
        if pr is None:
            continue
        reason = stale_reason(card.get("state"), (pr_states or {}).get(pr))
        if reason:
            out.append({"number": card.get("number"), "reason": reason, "pr": pr})
    return out


def audit_line(action, number, detail=""):
    """The brain-log line of a dedup or cleanup decision.

    The supervisor greps for ``fix-card`` to review the decisions of a night.
    """
    line = "fix-card {action} #{number}".format(action=action, number=number)
    return "{line} ({detail})".format(line=line, detail=detail) if detail else line
