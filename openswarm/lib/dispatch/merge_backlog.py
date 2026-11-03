"""Merge backlog: the open lane PRs that wait for something (Refs #1696).

The 2026-11-02 night shift saw several pull requests that were approved or
green and still open: the external lane pull request #1632 (card #1626) waited
for a re-review and for a human decision about its thin diff, several fleet
pull requests waited for a review.  Nothing said *where* they waited, so a
queue that was not moving looked exactly like a queue that was moving.

This module answers that question for every pull request that is still in the
merge queue: **which stage is it in and how long has it been there**.  It is
the pure half of the health check section "合并积压" (the collector lives in
``tools/fleet-status-page.py``).

The rules are deliberately conservative:

* a pull request is in the queue when its **card** carries ``lane-pr``.  That
  label is put on the card, never on the pull request
  (``tools/lane-autodispatch/autodispatch.py:629``), so the link is read from
  the pull request body (``Closes #N``) or from its branch
  (``fleet/<lane>-issue<N>-…``);
* a merged, closed or draft pull request is not in the queue;
* a stage is only named when the evidence for it is there.  A missing review
  record or a missing CI rollup is **not** evidence: the stage is ``unknown``
  and the row keeps the signals it does have;
* the dwell time is counted from the moment the pull request entered its
  current stage, never from the moment it was opened.  A pull request that was
  approved a minute ago is not "approved and waiting for two hours".  When the
  moment cannot be read, the dwell time is ``None`` and the row says so.

Every stage has its own clock, and the clock is only read when the evidence
for it is in the reading:

======================== ==============================================
stage                    the moment the stage started
======================== ==============================================
等首审                   the pull request was opened
等复审                   the head moved after the last verdict
审查 CHANGES 待修复       the verdict that asked for changes
CI 未绿                   the failing check that finished
已批待合并                the approval *and* the green checks
等人工裁决                the ``needs-human`` label was put on
======================== ==============================================

The stage "已批待合并" needs two things at the same time, so it starts at the
**later** of the two moments: an approval that came three hours before the
checks turned green did not enter the stage three hours ago.  When one of the
two moments cannot be read the stage has no dwell time either -- an approval
without a time is not a guess.

The module is pure: it reads no file, runs no command and never touches the
clock.  The caller passes the collected data and the time the reading belongs
to, so a whole night can be replayed from a snapshot.

Usage::

    rows = merge_backlog.backlog(prs, cards, ledger, now)
    for line in merge_backlog.brain_log_lines(rows):
        print(line)

Standard library only, Python 3.9+.
"""

from __future__ import annotations

import datetime
import importlib.util
import math
import os
import re
from typing import Any, Dict, Iterable, List, Optional

__all__ = [
    "APPROVED_DWELL_LIMIT_S",
    "CONTRACT_CONTEXT",
    "LANE_PR_LABEL",
    "NEEDS_CHANGES_LABEL",
    "NEEDS_HUMAN_LABEL",
    "REASON_THIN_CARD",
    "REVIEWED_LABEL",
    "REVIEW_CONTEXT",
    "SOURCE_APPROVED",
    "SOURCE_CHANGES",
    "SOURCE_CI",
    "SOURCE_CREATED",
    "SOURCE_NEEDS_HUMAN",
    "SOURCE_UPDATED",
    "STAGE_APPROVED",
    "STAGE_CHANGES",
    "STAGE_CI_RED",
    "STAGE_DWELL_LIMIT_S",
    "STAGE_FIRST_REVIEW",
    "STAGE_HUMAN",
    "STAGE_RE_REVIEW",
    "STAGE_UNKNOWN",
    "backlog",
    "brain_log_lines",
    "classify",
    "duration",
    "is_overdue",
    "linked_issue",
    "section_data",
]

# ------------------------------------------------------------------- labels

# The label the dispatcher puts on the card of a finished lane
# (``tools/lane-autodispatch/autodispatch.py``).  The pull request itself
# never carries it.
LANE_PR_LABEL = "lane-pr"
# The labels the dispatch engine puts on the pull request
# (``openswarm/lib/dispatch/engine.py``).
REVIEWED_LABEL = "lane-reviewed"
NEEDS_CHANGES_LABEL = "needs-changes"
NEEDS_HUMAN_LABEL = "needs-human"
# The commit status context of a lane review verdict
# (``openswarm/lib/review/status.py``).
REVIEW_CONTEXT = "lane-review"
# The contract gate of an external lane: red for a missing link or a thin diff
# (``openswarm/docs/EXTERNAL-LANES.md``).
CONTRACT_CONTEXT = "external-lane/contract"

# ------------------------------------------------------------------- stages

STAGE_FIRST_REVIEW = "等首审"
STAGE_RE_REVIEW = "等复审"
STAGE_CHANGES = "审查 CHANGES 待修复"
STAGE_CI_RED = "CI 未绿"
STAGE_APPROVED = "已批待合并"
STAGE_HUMAN = "等人工裁决"
STAGE_UNKNOWN = "unknown"

# The reason of a human arbitration: a thin diff needs an exemption.  A plain
# human decision has no reason and says so by leaving the stage alone.
REASON_THIN_CARD = "薄卡豁免"
REASON_HUMAN = None

# An approved pull request waits two hours for the merge loop; every other
# stage waits a day before it is called a backlog.
APPROVED_DWELL_LIMIT_S = 2 * 3600
STAGE_DWELL_LIMIT_S = 24 * 3600

# The CI states of one pull request.
CI_GREEN = "green"
CI_RED = "red"
CI_PENDING = "pending"
CI_NONE = "none"
CI_UNKNOWN = "unknown"

# The link of a pull request to its card: a closing reference wins over the
# branch convention (``openswarm/lib/dispatch/engine.py``,
# ``tools/check-pr-issue-link.py``).
CLOSES_RE = re.compile(r"\b(?:Closes|Fixes|Resolves)\s+#([1-9]\d*)\b", re.I)
BRANCH_RE = re.compile(r"(?:fleet|brain)/[^/]+-issue([1-9]\d*)-.+")

# The sources of a dwell time, named in every row so the reader knows how
# exact the number is.
SOURCE_CHANGES = "CHANGES 判决时间"
SOURCE_CI = "CI 检查时间"
SOURCE_CREATED = "PR 创建时间"
SOURCE_UPDATED = "PR 最后变动时间"
# The moment the ``needs-human`` label was put on the pull request; the
# dispatcher writes it into the review ledger (``needs_human_at``).
SOURCE_NEEDS_HUMAN = "人工裁决标记时间"
# The stage "已批待合并" starts at the later of the two moments.
SOURCE_APPROVED = "批准时间与 CI 转绿时间"

_CI_CHECKS = None


# ------------------------------------------------------------------- helpers

def _ci_checks():
    """The CI classifier of the repository (``openswarm/lib/ci/checks.py``).

    That module is the single place that decides what counts as red, so the
    backlog and the merge gate cannot disagree about it.
    """
    global _CI_CHECKS
    if _CI_CHECKS is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ci", "checks.py")
        spec = importlib.util.spec_from_file_location("openswarm_ci_checks", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CI_CHECKS = module
    return _CI_CHECKS


def label_names(obj: Any) -> set:
    """The label names of a pull request or a card (strings or ``{"name": ...}``)."""
    if not isinstance(obj, dict):
        return set()
    labels = obj.get("labels")
    if isinstance(labels, str):
        labels = [labels]
    if not isinstance(labels, (list, tuple, set, frozenset)):
        return set()
    names = set()
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def linked_issue(pr: Any) -> Optional[int]:
    """The card a pull request belongs to, or None when it cannot be read.

    The closing reference of the body wins over the branch convention, the
    same way the external lane pass and the dispatcher read it.
    """
    if not isinstance(pr, dict):
        return None
    match = CLOSES_RE.search(pr.get("body") or "")
    if match:
        return int(match.group(1))
    head = pr.get("headRefName") or (pr.get("head") or {}).get("ref") or ""
    match = BRANCH_RE.fullmatch(str(head))
    return int(match.group(1)) if match else None


def head_sha(pr: Any) -> Optional[str]:
    """The sha of the pull request head, if the input carries one."""
    if not isinstance(pr, dict):
        return None
    sha = pr.get("headRefOid") or (pr.get("head") or {}).get("sha")
    return str(sha).strip().lower() if sha else None


def epoch(value: Any, offset: Optional[float] = None) -> Optional[float]:
    """Return a POSIX timestamp for a number or an ISO-8601 string, else None.

    A naive stamp (no timezone) is read in the fleet timezone: ``offset`` is
    the seconds east of UTC.  The review ledger of the dispatcher writes local
    wall time without a zone, so the timezone has to be named by the caller.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            pass
        else:
            return number if math.isfinite(number) else None
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            if offset is None:
                parsed = parsed.astimezone()
            else:
                zone = datetime.timezone(datetime.timedelta(seconds=float(offset)))
                parsed = parsed.replace(tzinfo=zone)
        return parsed.timestamp()
    return None


def iso(value: Any) -> Optional[str]:
    """Return the ISO-8601 UTC stamp of a timestamp, or None when there is none."""
    moment = epoch(value)
    if moment is None:
        return None
    try:
        stamp = datetime.datetime.fromtimestamp(moment, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.isoformat(timespec="seconds")


def duration(seconds: Any) -> str:
    """A short human duration (``42m0s``, ``3h07m``, ``-`` when unknown)."""
    if seconds is None:
        return "-"
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    if total < 0:
        total = 0
    if total < 60:
        return "%ds" % total
    if total < 3600:
        return "%dm%02ds" % (total // 60, total % 60)
    return "%dh%02dm" % (total // 3600, (total % 3600) // 60)


def _check_name(item: Dict[str, Any]) -> str:
    """The name of a check, for a commit status and for a check run alike."""
    if "context" in item and "state" in item:
        return str(item.get("context") or "")
    return str(item.get("name") or item.get("workflowName") or "")


# ------------------------------------------------------------------ evidence

def verdict_records(pr: Any, ledger: Any) -> List[Dict[str, Any]]:
    """The review verdicts of one pull request, newest last.

    The ledger of the dispatcher (``reviews.json``) is the record with a
    clock: it keeps the verdict, the sha that was reviewed and the time of the
    verdict.  When the ledger knows nothing about the pull request, the commit
    status of the lane review says *that* a verdict exists (without a time).
    """
    records: List[Dict[str, Any]] = []
    if not isinstance(pr, dict):
        return records
    number = pr.get("number")
    entry = ledger.get(str(number)) if isinstance(ledger, dict) else None
    if isinstance(entry, dict):
        verdict = str(entry.get("verdict") or "").strip().upper()
        if verdict:
            records.append({"verdict": verdict,
                            "sha": str(entry.get("sha") or "").strip().lower() or None,
                            "at": entry.get("at"),
                            "needs_human_at": entry.get("needs_human_at"),
                            "source": "dispatcher ledger (reviews.json)"})
    if records:
        return records
    for check in pr.get("statusCheckRollup") or []:
        if not isinstance(check, dict) or _check_name(check) != REVIEW_CONTEXT:
            continue
        state = str(check.get("state") or check.get("conclusion") or "").strip().upper()
        if state in ("SUCCESS", "NEUTRAL"):
            verdict = "APPROVE"
        elif state in ("FAILURE", "ERROR"):
            verdict = "CHANGES"
        else:
            continue
        records.append({"verdict": verdict, "sha": None, "at": None,
                        "source": "commit status " + REVIEW_CONTEXT})
    return records


def _ci_items(items: List[Any], policy: Any) -> Dict[str, List[Any]]:
    """Split the checks into green, red and pending, one check at a time.

    The classifier of the merge gate (``openswarm/lib/ci/checks.py``) is the
    single place that decides what counts as red, so the backlog asks it about
    every check on its own: that way every check keeps its own timestamps.
    """
    checks = _ci_checks()
    green: List[Any] = []
    red: List[Any] = []
    pending: List[Any] = []
    for item in items:
        result = checks.classify([item], policy)
        if result.get("advisory") or result.get("ignored"):
            continue          # advisory checks never gate, ignored ones do not either
        state = result.get("state")
        if state == checks.GREEN:
            green.append(item)
        elif state == checks.RED:
            red.append(item)
        elif state == checks.PENDING:
            if policy.ignores(_check_name(item)):
                continue      # an ignored pending check does not gate
            pending.append(item)
    return {"green": green, "red": red, "pending": pending}


def _green_moment(items: List[Any], offset: Optional[float]) -> Optional[float]:
    """The moment the checks turned green: the last effective check that finished.

    ``None`` when one of the checks carries no time: then nobody can say when
    the set became green, and the stage that waits for the merge loop has no
    dwell time either.
    """
    moments = []
    for item in items:
        moment = epoch(item.get("completedAt"), offset)
        if moment is None:
            return None
        moments.append(moment)
    return max(moments) if moments else None


def _red_moment(items: List[Any], offset: Optional[float]) -> Optional[float]:
    """The moment the checks turned red: the first failing check that finished."""
    moments = []
    for item in items:
        moment = epoch(item.get("completedAt") or item.get("startedAt"), offset)
        if moment is not None:
            moments.append(moment)
    return min(moments) if moments else None


def _pending_moment(items: List[Any], offset: Optional[float]) -> Optional[float]:
    """The moment the running checks started."""
    moments = []
    for item in items:
        moment = epoch(item.get("startedAt") or item.get("completedAt"), offset)
        if moment is not None:
            moments.append(moment)
    return min(moments) if moments else None


def ci_reading(pr: Any, policy: Any = None,
               offset: Optional[float] = None) -> Dict[str, Any]:
    """The CI state of one pull request, with the moments the stages need.

    The lane review status is not a CI check (it is the verdict), so it is
    left out; the contract gate of an external lane is one: a thin diff keeps
    the pull request out of the merge gate.

    A missing rollup is ``unknown``, not green: without the rollup nobody can
    say whether the checks are running.  A rollup without a single effective
    check is ``none``: there is no evidence of a green run either.
    """
    reading: Dict[str, Any] = {"state": CI_UNKNOWN, "red": [], "pending": [],
                                "green_at": None, "red_at": None, "pending_at": None,
                                "reason": "CI 状态缺失"}
    if not isinstance(pr, dict):
        return reading
    rollup = pr.get("statusCheckRollup")
    if rollup is None:
        return reading
    if not isinstance(rollup, (list, tuple)):
        reading["reason"] = "CI 数据格式未知"
        return reading
    checks = _ci_checks()
    items = [item for item in rollup
             if isinstance(item, dict) and _check_name(item) != REVIEW_CONTEXT]
    if isinstance(policy, checks.IgnorePolicy):
        effective = policy
    else:
        effective = checks.IgnorePolicy.from_cfg(policy or {})
    result = checks.gate_classify(items, effective)
    buckets = _ci_items(items, effective)
    green_at = _green_moment(buckets["green"], offset)
    red_at = _red_moment(buckets["red"], offset)
    pending_at = _pending_moment(buckets["pending"], offset)
    reading.update({
        "state": result["state"],
        "red": list(result["red"]),
        "pending": list(result["pending"]),
        "ignored": list(result.get("ignored") or []),
        "green_at": green_at,
        "red_at": red_at,
        "pending_at": pending_at,
        "reason": "",
    })
    return reading


# ------------------------------------------------------------------- stages

def _updated_start(pr: Any, offset: Optional[float]) -> Optional[float]:
    """The moment the pull request last changed (its own clock)."""
    return epoch(pr.get("updatedAt") or pr.get("updated_at"), offset)


def _record_for_head(record: Optional[Dict[str, Any]], pr: Any) -> bool:
    """True when a verdict is about the head that is open now.

    A verdict without a sha cannot be checked, so it counts for the head that
    is open; a verdict about another commit says the head moved.
    """
    if not record:
        return False
    reviewed = record.get("sha")
    current = head_sha(pr)
    if not reviewed or not current:
        return True
    return reviewed == current


def _changes_start(record: Optional[Dict[str, Any]],
                   offset: Optional[float]) -> Dict[str, Any]:
    """The moment the verdict asked for changes.

    The ledger is the only record that carries the time of a verdict, so
    without it the moment is unknown: the last change of the pull request is
    not a substitute for it.
    """
    if record and record.get("at") is not None:
        moment = epoch(record.get("at"), offset)
        if moment is not None:
            return {"at": moment, "source": SOURCE_CHANGES}
    return {"at": None, "source": None}


def _needs_human_start(record: Optional[Dict[str, Any]],
                       offset: Optional[float]) -> Dict[str, Any]:
    """The moment the ``needs-human`` label was put on the pull request.

    The dispatcher writes that moment into the review ledger
    (``needs_human_at``).  The CHANGES verdict that led to the escalation is
    not the same moment -- the label can be put on a later tick -- so it must
    not be used as a substitute: without the label time the dwell is unknown.
    """
    if record and record.get("needs_human_at") is not None:
        moment = epoch(record.get("needs_human_at"), offset)
        if moment is not None:
            return {"at": moment, "source": SOURCE_NEEDS_HUMAN}
    return {"at": None, "source": None}


def _approved_start(pr: Any, record: Optional[Dict[str, Any]], ci: Dict[str, Any],
                    offset: Optional[float]) -> Dict[str, Any]:
    """The moment the pull request entered the stage "已批待合并".

    That stage needs two things at the same time: the approval of the head
    that is open now and green checks.  It starts at the **later** of the two
    moments, so an approval that came three hours before the checks turned
    green is not counted as three hours of waiting.  When one of the two
    moments cannot be read, the moment of the stage is unknown and the row
    says which reading is missing.
    """
    approval = None
    if record and record.get("verdict") == "APPROVE" and _record_for_head(record, pr):
        approval = epoch(record.get("at"), offset)
    green = ci.get("green_at")
    missing = []
    if approval is None:
        missing.append("批准时间")
    if green is None:
        missing.append("CI 转绿时间")
    if missing:
        return {"at": None, "source": None, "missing": missing}
    return {"at": max(approval, green), "source": SOURCE_APPROVED, "missing": []}


def _re_review_start(record: Optional[Dict[str, Any]], pr: Any,
                     offset: Optional[float]) -> Dict[str, Any]:
    """The moment the head moved and the review became due again.

    The head move is not in the reading of ``gh pr list``: the closest clock
    is the last change of the pull request, and it only counts when it is not
    older than the verdict (a pull request that did not change since the
    verdict did not move its head).
    """
    updated = _updated_start(pr, offset)
    if updated is None:
        return {"at": None, "source": None}
    verdict = epoch((record or {}).get("at"), offset)
    if verdict is not None and updated < verdict:
        return {"at": None, "source": None}
    return {"at": updated, "source": SOURCE_UPDATED}


def _approved_now(pr: Any, labels: set, record: Optional[Dict[str, Any]]) -> bool:
    """True when the pull request is approved for its current head.

    The ``lane-reviewed`` label is the authoritative signal: the dispatcher
    removes it as soon as the head moves.  A verdict of the ledger counts when
    it was written for the head that is open now.
    """
    if REVIEWED_LABEL in labels:
        # A label that survived a head move would be wrong: when the ledger
        # says the approval is about another commit, the review is due again.
        if record and record.get("verdict") == "APPROVE" \
                and not _record_for_head(record, pr):
            return False
        return True
    if not record or record.get("verdict") != "APPROVE":
        return False
    return _record_for_head(record, pr)


def _stage_text(stage: str, reason: Optional[str]) -> str:
    """The stage with its reason, e.g. ``等人工裁决（薄卡豁免）``."""
    if reason:
        return "%s（%s）" % (stage, reason)
    return stage


def classify(pr: Any, card: Any, ledger: Any, now: Any,
             policy: Any = None, offset: Optional[float] = None,
             review_source: bool = True) -> Optional[Dict[str, Any]]:
    """One row of the merge queue, or None when the pull request is not in it.

    ``card`` is the card the pull request belongs to (None when it cannot be
    found), ``ledger`` the review ledger of the dispatcher (None when it was
    not collected) and ``review_source`` says whether the ledger was there at
    all: without it the absence of a verdict is not evidence of "no review".
    """
    if not isinstance(pr, dict):
        return None
    state = str(pr.get("state") or "OPEN").strip().upper()
    if state != "OPEN" or pr.get("mergedAt") or pr.get("merged_at"):
        return None
    if pr.get("isDraft") or pr.get("draft"):
        return None
    issue = linked_issue(pr)
    if issue is None:
        return None
    if not isinstance(card, dict) or LANE_PR_LABEL not in label_names(card):
        return None

    labels = label_names(pr)
    records = verdict_records(pr, ledger)
    record = records[-1] if records else None
    ci = ci_reading(pr, policy, offset)
    signals: List[str] = []
    if ci["state"] == CI_UNKNOWN:
        signals.append(ci["reason"])
    elif ci["state"] == CI_NONE:
        signals.append("CI 无检查项")
    if ci["red"]:
        signals.append("CI 失败: " + ",".join(ci["red"]))
    if ci["pending"]:
        signals.append("CI 进行中: " + ",".join(ci["pending"]))
    contract_failed = CONTRACT_CONTEXT in ci["red"]
    if contract_failed:
        signals.append("%s 失败（薄卡/契约门）" % CONTRACT_CONTEXT)
    if pr.get("reviewDecision"):
        signals.append("reviewDecision=" + str(pr["reviewDecision"]))

    reason: Optional[str] = None
    stage: str
    start: Optional[float] = None
    source: Optional[str] = None

    if NEEDS_HUMAN_LABEL in labels:
        # A human decision: the stage started when the needs-human label was
        # put on the pull request, not when the verdict before it was written.
        stage = STAGE_HUMAN
        reason = REASON_THIN_CARD if contract_failed else REASON_HUMAN
        moment = _needs_human_start(record, offset)
        start, source = moment["at"], moment["source"]
        if start is None:
            signals.append("人工裁决标记时间缺失")
    elif NEEDS_CHANGES_LABEL in labels or (
            record and record.get("verdict") == "CHANGES"
            and _record_for_head(record, pr)):
        stage = STAGE_CHANGES
        moment = _changes_start(record, offset)
        start, source = moment["at"], moment["source"]
        if start is None:
            signals.append("CHANGES 判决时间缺失")
    elif ci["state"] in (CI_RED, CI_PENDING):
        # The checks are not green: the stage started when the failing check
        # finished (or when the running one started).
        stage = STAGE_CI_RED
        if ci["state"] == CI_RED:
            start, source = ci["red_at"], SOURCE_CI
        else:
            start, source = ci["pending_at"], SOURCE_CI
        if start is None:
            signals.append("CI 检查时间缺失")
    elif _approved_now(pr, labels, record):
        if ci["state"] == CI_GREEN:
            # Approved and green: the stage started at the later of the two
            # moments, so an approval that waited for the checks is counted
            # from the moment the checks turned green.
            stage = STAGE_APPROVED
            moment = _approved_start(pr, record, ci, offset)
            start, source = moment["at"], moment["source"]
            for missing in moment["missing"]:
                signals.append(missing + "缺失")
        else:
            # Approved, but nobody can say whether the checks are green: an
            # empty rollup is no evidence of a green run.
            stage = STAGE_UNKNOWN
    elif record:
        # A verdict exists, but not for the head that is open now: the head
        # moved and the review is due again.  The stage starts when the pull
        # request last changed, which is the closest clock to the move.
        stage = STAGE_RE_REVIEW
        moment = _re_review_start(record, pr, offset)
        start, source = moment["at"], moment["source"]
        if start is None:
            signals.append("头部移动时间缺失")
    elif ci["state"] == CI_UNKNOWN:
        stage = STAGE_UNKNOWN
    elif not review_source:
        stage = STAGE_UNKNOWN
        signals.append("review 记录缺失")
    else:
        stage = STAGE_FIRST_REVIEW
        created = epoch(pr.get("createdAt") or pr.get("created_at"), offset)
        start, source = created, SOURCE_CREATED
        if created is None:
            signals.append("创建时间缺失")

    moment_now = epoch(now)
    dwell = None
    if start is not None and moment_now is not None:
        dwell = max(0.0, float(moment_now) - float(start))
    age = None
    created = epoch(pr.get("createdAt") or pr.get("created_at"), offset)
    if created is not None and moment_now is not None:
        age = max(0.0, float(moment_now) - float(created))
    if stage == STAGE_UNKNOWN:
        # An unknown stage has no dwell time: the moment it started is unknown.
        dwell = None
        source = None

    stage_text = _stage_text(stage, reason)
    row = {
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "url": pr.get("url"),
        "issue": issue,
        "stage": stage,
        "reason": reason,
        "stage_text": stage_text,
        "stage_started_at": iso(start),
        "dwell_s": dwell,
        "dwell_source": source,
        "age_s": age,
        "ci_state": ci["state"],
        "signals": signals,
        "summary": "#%s %s 停留%s" % (pr.get("number"), stage_text, duration(dwell)),
    }
    row["overdue"] = is_overdue(row)
    return row


def is_overdue(row: Any) -> bool:
    """True when a row is over the threshold of its stage.

    An approved pull request waits two hours for the merge loop, every other
    stage waits a day.  A row without a dwell time cannot be over anything.
    """
    if not isinstance(row, dict):
        return False
    dwell = row.get("dwell_s")
    if not isinstance(dwell, (int, float)) or isinstance(dwell, bool):
        return False
    if row.get("stage") == STAGE_APPROVED:
        return dwell > APPROVED_DWELL_LIMIT_S
    return dwell > STAGE_DWELL_LIMIT_S


def overdue(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    """The rows that are over their threshold."""
    return [row for row in rows or [] if is_overdue(row)]


def brain_log_lines(rows: Iterable[Any]) -> List[str]:
    """The naming lines of the rows that are over their threshold.

    These are the lines the brain log gets: a backlog that is not named is a
    backlog nobody reads.
    """
    lines = []
    for row in rows or []:
        if not is_overdue(row):
            continue
        limit = (APPROVED_DWELL_LIMIT_S if row.get("stage") == STAGE_APPROVED
                 else STAGE_DWELL_LIMIT_S)
        lines.append("合并积压点名 %s（阈值 %s）" % (row.get("summary") or "", duration(limit)))
    return lines


# ------------------------------------------------------------------- backlog

def backlog(prs: Any, cards: Any, ledger: Any, now: Any,
            policy: Any = None, offset: Optional[float] = None,
            review_source: bool = True) -> List[Dict[str, Any]]:
    """The rows of the merge queue, in the order of the pull request numbers.

    ``cards`` are the cards that carry the ``lane-pr`` label, ``ledger`` the
    review ledger of the dispatcher (None when it was not collected).
    """
    by_number: Dict[int, Any] = {}
    for card in cards or []:
        if isinstance(card, dict) and isinstance(card.get("number"), int):
            by_number[card["number"]] = card
    rows = []
    for pr in prs or []:
        if not isinstance(pr, dict):
            continue
        number = pr.get("number")
        row = classify(pr, by_number.get(linked_issue(pr)), ledger, now,
                       policy=policy, offset=offset, review_source=review_source)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: row.get("number") or 0)
    return rows


def section_data(prs: Any, cards: Any, ledger: Any, now: Any,
                 policy: Any = None, offset: Optional[float] = None,
                 review_source: bool = True) -> Dict[str, Any]:
    """The data of the health check section "合并积压".

    Besides the rows it keeps the counters the reader needs to trust the
    section: how many pull requests were left out and why.
    """
    rows = backlog(prs, cards, ledger, now, policy=policy, offset=offset,
                   review_source=review_source)
    by_stage: Dict[str, int] = {}
    for row in rows:
        by_stage[row["stage"]] = by_stage.get(row["stage"], 0) + 1
    dwells = [row["dwell_s"] for row in rows if row.get("dwell_s") is not None]
    skipped = {"merged": 0, "no_link": 0, "no_lane_pr": 0, "draft": 0}
    card_numbers = set()
    for card in cards or []:
        if not isinstance(card, dict) or not isinstance(card.get("number"), int):
            continue
        if LANE_PR_LABEL in label_names(card):
            card_numbers.add(card["number"])
    for pr in prs or []:
        if not isinstance(pr, dict):
            continue
        state = str(pr.get("state") or "OPEN").strip().upper()
        if state != "OPEN" or pr.get("mergedAt") or pr.get("merged_at"):
            skipped["merged"] += 1
            continue
        if pr.get("isDraft") or pr.get("draft"):
            skipped["draft"] += 1
            continue
        issue = linked_issue(pr)
        if issue is None:
            skipped["no_link"] += 1
            continue
        if issue not in card_numbers:
            skipped["no_lane_pr"] += 1
    return {
        "count": len(rows),
        "by_stage": by_stage,
        "rows": rows,
        "overdue": [row["number"] for row in rows if row.get("overdue")],
        "unknown": by_stage.get(STAGE_UNKNOWN, 0),
        "oldest_dwell_s": max(dwells) if dwells else None,
        "skipped": skipped,
        "approved_limit_s": APPROVED_DWELL_LIMIT_S,
        "stage_limit_s": STAGE_DWELL_LIMIT_S,
    }
