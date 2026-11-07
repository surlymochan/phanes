"""Lane review metrics: first-round pass rate, average review rounds, findings spread (Refs #1733).

What this measures
------------------

A lane review either approves a pull request or asks for changes.  Two numbers say
how healthy that loop is:

``first_round_pass_rate``
    the share of the reviewed pull requests that passed their **first** formal
    review round (the target is 60% or more)
``avg_review_rounds``
    the mean number of formal review rounds a reviewed pull request needed (the
    target is 2 or less)

The counters live in the dispatcher's review records (``reviews.json``); this module
turns them into per lane, per backend and per week numbers.  It is pure: it reads no
file, runs no command and never touches the clock, so the whole report can be
replayed from a snapshot of the records.

The measurement rules (口径)
----------------------------

* A **formal round** is one review verdict on a pull request head.  The dispatcher
  writes ``review_rounds`` (the number of verdicts of that pull request) into every
  record it writes.  A historical record only carries ``rounds`` (the number of
  *changes* rounds), from which the verdicts are derived: ``rounds`` plus the
  approving verdict, because the approving verdict is one round more.
* A **first-round pass** is a pull request whose **first** formal verdict was
  ``APPROVE``.  The dispatcher stores that first verdict in ``first_verdict`` and it
  never changes: a later verdict (a new head, a re-review) cannot turn a pull
  request that failed its first round into a first-round pass.  A pull request that
  still sits in ``CHANGES`` counts as a first-round failure: its first round already
  happened.  A historical record without ``first_verdict`` is read from its verdict
  and its ``rounds`` counter (a ``rounds`` counter above zero means the first verdict
  was ``CHANGES``).
* A **rebase review** (the re-review of a pull request whose head the dispatcher
  itself moved with a rebase) is not a formal round: the code did not change, only
  its base did.  The dispatcher marks those reviews and the record keeps their count
  in ``rebase_reviews``; the verdict of such a re-review never overwrites the first
  verdict.
* A record without a verdict (a failed review, a rebase marker) or without a usable
  round count is **skipped**: it is counted in ``skipped`` and never enters a rate.
  A historical record that only carries an ``APPROVE`` is counted as a first-round
  pass, but it is marked ``legacy-inferred`` so the reader can see how many samples
  came from the old format.
* The **blockers** and the **follow-ups** of a review are read from the conclusion
  text: the ``[BLOCKER]`` and ``[FOLLOW-UP]`` entries of the findings list (the
  format the lane reviews really use) and, when the conclusion carries no such
  entry, from the ``BLOCKERS: n`` / ``FOLLOW-UPS: n`` summary lines.  The entries
  win over the summary line: they are what the reviewer really wrote.
* The **follow-up cards** (``followup_tickets``) are the follow-up issues the
  dispatcher really opened for the ``[FOLLOW-UP]`` findings of a review.  The rework
  task of a changes round is **not** a follow-up card: that one is queued on the
  original issue (the issue body is edited), and it is counted in ``rework_tasks``.
* The **week** of a review is the ISO week of the record's ``at`` stamp.
* The **bottom lane** of a week is the lane with the lowest first-round pass rate
  (ties are broken by the highest average round count, then by the lane name).  The
  report lists the lanes that were bottom in two consecutive weeks.

The lanes are the **reviewing** lanes (the lane that wrote the verdict); the report
also carries the **author** lanes (the lane whose pull request was reviewed), because
that is the view the scheduler needs when it wants to dispatch less work to a lane.

Usage::

    report = review_metrics.compute(reviews)
    print(review_metrics.render(report))

Standard library only, Python 3.9+.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "METRICS_FILE",
    "SCHEMA",
    "TARGET_AVG_ROUNDS",
    "TARGET_FIRST_ROUND_RATE",
    "compute",
    "first_round_pass",
    "first_verdict_of",
    "follow_up_entries",
    "formal_rounds",
    "parse_counts",
    "parse_findings",
    "render",
    "verdict_of",
    "verdict_word",
    "week_of",
]

# The status file the dispatcher writes next to ``lanes-status.json``.
METRICS_FILE = "lane-review-metrics.json"
SCHEMA = 1
# The target lines of the lane review loop (Refs #1733).
TARGET_FIRST_ROUND_RATE = 0.6
TARGET_AVG_ROUNDS = 2.0

# The verdict words a review record may carry.  The dispatcher writes APPROVE and
# CHANGES; the merge gate also accepts the lower case spellings.
_APPROVE_WORDS = {"approve", "approved", "approval"}
_CHANGES_WORDS = {"changes", "change", "changes_requested", "change_requested"}

# `BLOCKERS: 3` / `FOLLOW-UPS: 1` in the review conclusion.
_COUNT_RE = re.compile(r"^[ \t]*(BLOCKERS?|FOLLOW[ \t\-_]?UPS?)[ \t]*:[ \t]*([0-9]+)[ \t]*$",
                       re.M | re.I)
# The findings of the review conclusion: `- [BLOCKER] ...` / `- [FOLLOW-UP] ...`.
# The marker has to open the line, so a mention inside a sentence never counts.
_ENTRY_RE = re.compile(r"^[ \t]*(?:[-*+][ \t]*)?\[(BLOCKER|FOLLOW[ \t\-_]?UPS?)\][ \t]*(.*)$",
                       re.M | re.I)
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def verdict_of(record: Any) -> str:
    """The verdict of one review record: ``approve``, ``changes`` or ``""``.

    A record without a verdict is not a review (a failed review, a rebase marker):
    it carries no formal round and is skipped by the metrics.
    """
    if not isinstance(record, dict):
        return ""
    word = str(record.get("verdict") or "").strip().lower().replace(" ", "_")
    if word in _APPROVE_WORDS:
        return "approve"
    if word in _CHANGES_WORDS:
        return "changes"
    return ""


def verdict_word(value: Any) -> str:
    """The verdict word of a verdict value: ``APPROVE``, ``CHANGES`` or ``""``."""
    word = str(value or "").strip().lower().replace(" ", "_")
    if word in _APPROVE_WORDS:
        return "APPROVE"
    if word in _CHANGES_WORDS:
        return "CHANGES"
    return ""


def parse_findings(text: Any) -> Dict[str, List[str]]:
    """The findings of a review conclusion, split by marker.

    The lane reviews write their findings as ``- [BLOCKER] ...`` and
    ``- [FOLLOW-UP] ...`` entries; this is the format the metrics read.  The text of
    every entry is returned, so the dispatcher can open one follow-up card per
    follow-up entry.
    """
    blockers: List[str] = []
    follow_ups: List[str] = []
    for match in _ENTRY_RE.finditer(str(text or "")):
        name = re.sub(r"[ \t\-_]", "", match.group(1)).upper()
        item = match.group(2).strip()
        if name.startswith("BLOCKER"):
            blockers.append(item)
        elif name.startswith("FOLLOW"):
            follow_ups.append(item)
    return {"blockers": blockers, "follow_ups": follow_ups}


def follow_up_entries(text: Any) -> List[str]:
    """The ``[FOLLOW-UP]`` findings of a review conclusion."""
    return parse_findings(text)["follow_ups"]


def parse_counts(text: Any) -> Dict[str, Optional[int]]:
    """The finding counts of a review conclusion.

    The counts are read from the ``[BLOCKER]`` / ``[FOLLOW-UP]`` entries of the
    findings list (the format the lane reviews really use); a conclusion without such
    an entry falls back to the ``BLOCKERS: n`` / ``FOLLOW-UPS: n`` summary lines.  A
    missing or malformed count stays ``None`` (unknown), it is never guessed from the
    prose.
    """
    blockers: Optional[int] = None
    follow_ups: Optional[int] = None
    for match in _COUNT_RE.finditer(str(text or "")):
        name = re.sub(r"[ \t\-_]", "", match.group(1)).upper()
        value = int(match.group(2))
        if name.startswith("BLOCKER") and blockers is None:
            blockers = value
        elif name.startswith("FOLLOW") and follow_ups is None:
            follow_ups = value
    findings = parse_findings(text)
    if findings["blockers"]:
        blockers = len(findings["blockers"])
    if findings["follow_ups"]:
        follow_ups = len(findings["follow_ups"])
    return {"blockers": blockers, "follow_ups": follow_ups}


def _as_int(value: Any) -> Optional[int]:
    """An integer value, or ``None`` when the field is missing or not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def formal_rounds(record: Any) -> Tuple[Optional[int], str]:
    """The formal review rounds of one record and where that number came from.

    Returns ``(rounds, source)``.  ``rounds`` is ``None`` when the record cannot
    be used (no verdict, no usable round count); ``source`` then says why:

    ``record``           the dispatcher wrote ``review_rounds``
    ``legacy-rounds``    the record only carries ``rounds`` (the changes rounds)
    ``legacy-inferred``  a historical approve without any round count
    ``no-verdict``       the record is not a review
    ``unknown-rounds``   the record has no usable round count
    """
    verdict = verdict_of(record)
    if not verdict:
        return None, "no-verdict"
    if not isinstance(record, dict):
        return None, "no-verdict"
    recorded = _as_int(record.get("review_rounds"))
    if recorded is not None and recorded >= 1:
        return recorded, "record"
    legacy = _as_int(record.get("rounds"))
    if legacy is not None and legacy >= 0:
        # the old counter only counted the changes rounds: the approving verdict is
        # one round more, a changes round is already inside the counter
        if verdict == "changes":
            return max(1, legacy), "legacy-rounds"
        return legacy + 1, "legacy-rounds"
    if verdict == "approve":
        # a historical approve without a changes round: the approve was the first
        # (and only) verdict of that pull request
        return 1, "legacy-inferred"
    return None, "unknown-rounds"


def first_verdict_of(record: Any) -> str:
    """The verdict of the **first** formal review round: ``approve``, ``changes`` or ``""``.

    The dispatcher writes ``first_verdict`` into every record it writes and never
    changes it: a re-review (a rebase re-review, a review of a new head) cannot
    rewrite the first verdict.  A historical record without that field is read from
    its verdict and its ``rounds`` counter (a ``rounds`` counter above zero means a
    changes round happened before this record, so the first verdict was ``CHANGES``).
    """
    if not isinstance(record, dict):
        return ""
    word = str(record.get("first_verdict") or "").strip().lower().replace(" ", "_")
    if word in _APPROVE_WORDS:
        return "approve"
    if word in _CHANGES_WORDS:
        return "changes"
    legacy = _as_int(record.get("rounds"))
    if legacy is not None and legacy >= 1:
        return "changes"
    return verdict_of(record)


def first_round_pass(record: Any) -> Optional[bool]:
    """Whether the pull request passed its first formal review round.

    ``None`` when the record cannot be used; a pull request whose first verdict was
    ``CHANGES`` is a first-round failure, whatever the later verdicts are.
    """
    rounds, _source = formal_rounds(record)
    if rounds is None:
        return None
    return first_verdict_of(record) == "approve"


def week_of(stamp: Any) -> Optional[str]:
    """The ISO week (``2026-W44``) of a review stamp, or ``None``."""
    match = _DATE_RE.search(str(stamp or ""))
    if not match:
        return None
    try:
        day = datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    iso = day.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def _bucket(value: Optional[int]) -> str:
    """The histogram bucket of a count: ``0``, ``1``, ``2``, ``3+`` or ``unknown``."""
    if value is None:
        return "unknown"
    if value < 0:
        return "unknown"
    if value >= 3:
        return "3+"
    return str(value)


def _rate(passed: int, total: int) -> Optional[float]:
    if not total:
        return None
    return round(passed / total, 4)


def _mean(values: Sequence[int]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _aggregate(samples: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The metric row of a group of samples."""
    total = len(samples)
    passed = sum(1 for sample in samples if sample["first_round_pass"])
    rounds = [sample["rounds"] for sample in samples]
    blockers: Dict[str, int] = {key: 0 for key in ("0", "1", "2", "3+", "unknown")}
    follow_ups: Dict[str, int] = {key: 0 for key in ("0", "1", "2", "3+", "unknown")}
    for sample in samples:
        for name, value in (("blockers", sample["blockers"]), ("follow_ups", sample["follow_ups"])):
            bucket = _bucket(value)
            target = blockers if name == "blockers" else follow_ups
            target[bucket] = target.get(bucket, 0) + 1
    rate = _rate(passed, total)
    average = _mean(rounds)
    return {
        "reviews": total,
        "first_round_pass": passed,
        "first_round_pass_rate": rate,
        "avg_rounds": average,
        "verdicts": {"approve": sum(1 for s in samples if s["verdict"] == "approve"),
                     "changes": sum(1 for s in samples if s["verdict"] == "changes")},
        "first_verdicts": {"approve": sum(1 for s in samples if s["first_verdict"] == "approve"),
                            "changes": sum(1 for s in samples if s["first_verdict"] == "changes")},
        "blockers": dict(sorted(blockers.items())),
        "follow_ups": dict(sorted(follow_ups.items())),
        "followup_tickets": sum(int(sample["followup_tickets"] or 0) for sample in samples),
        "rework_tasks": sum(int(sample["rework_tasks"] or 0) for sample in samples),
        "rebase_reviews": sum(int(sample["rebase_reviews"] or 0) for sample in samples),
        "legacy_samples": sum(1 for sample in samples if sample["rounds_source"] != "record"),
        "meets_first_round_target": (rate is not None and rate >= TARGET_FIRST_ROUND_RATE),
        "meets_rounds_target": (average is not None and average <= TARGET_AVG_ROUNDS),
    }


def _group(samples: Iterable[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    """The metric rows of a dimension (lane, backend, ...), sorted by name."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for sample in samples:
        groups.setdefault(sample[key], []).append(sample)
    return {name: _aggregate(rows) for name, rows in sorted(groups.items())}


def _bottom(rows: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """The bottom lane of a week: the lowest first-round pass rate wins.

    Ties are broken by the highest average round count (more rounds is worse) and
    then by the lane name, so the answer never depends on the dict order.
    """
    def rank(item: Tuple[str, Dict[str, Any]]):
        name, row = item
        rate = row["first_round_pass_rate"] if row["first_round_pass_rate"] is not None else 0.0
        average = row["avg_rounds"] if row["avg_rounds"] is not None else 0.0
        return (rate, -average, name)

    ranked = [item for item in rows.items()
              if item[1]["reviews"] and item[0] != "unknown"]
    if not ranked:
        return None
    return min(ranked, key=rank)[0]


def _week_index(week: str) -> Tuple[int, int]:
    """The ISO year and week number of an ISO week key."""
    match = re.fullmatch(r"(\d{4})-W(\d{1,2})", str(week or ""))
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def _weeks_are_consecutive(first: str, second: str) -> bool:
    """Whether two ISO weeks follow each other without a gap."""
    year, week = _week_index(first)
    other_year, other_week = _week_index(second)
    if not year or not other_year:
        return False
    monday = datetime.date.fromisocalendar(year, week, 1)
    other = datetime.date.fromisocalendar(other_year, other_week, 1)
    return (other - monday).days == 7


def _author_lane(pr_data: Any) -> Optional[str]:
    """The lane that wrote the pull request, read from the PR head branch."""
    if not isinstance(pr_data, dict):
        return None
    branch = str(pr_data.get("headRefName") or pr_data.get("head_ref") or "")
    match = re.match(r"fleet/([A-Za-z]+\d+)-issue", branch)
    return match.group(1) if match else None


def _pr_backend(pr_data: Any) -> Optional[str]:
    """The backend of the author lane, read from the PR labels (``backend:codex``)."""
    if not isinstance(pr_data, dict):
        return None
    labels = pr_data.get("labels") or []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else str(label)
        if not name:
            continue
        name = str(name).strip().lower()
        if name.startswith("backend:"):
            backend = name.split(":", 1)[1].strip()
            if backend:
                return backend
    return None


def _pr_index(prs: Any) -> Dict[str, Any]:
    """The pull request data, indexed by number (a list or a mapping)."""
    index: Dict[str, Any] = {}
    if isinstance(prs, dict):
        for key, value in prs.items():
            if isinstance(value, dict):
                index[str(key)] = value
    elif isinstance(prs, (list, tuple)):
        for item in prs:
            if isinstance(item, dict) and item.get("number") is not None:
                index[str(item["number"])] = item
    return index


def _followup_count(record: Dict[str, Any]) -> int:
    """The follow-up cards that were really opened for a pull request.

    The dispatcher records the numbers of the follow-up issues it opened
    (``followup_issues``); an older record only carries the counter
    (``followup_tickets``).  The rework task of a changes round is not a follow-up
    card: it lives in ``rework_tasks``.
    """
    issues = record.get("followup_issues")
    if isinstance(issues, (list, tuple)):
        return len(issues)
    return _as_int(record.get("followup_tickets")) or 0


def compute(reviews: Any, prs: Any = None, generated_at: Optional[str] = None) -> Dict[str, Any]:
    """The lane review metrics of a set of review records.

    ``reviews`` is the content of ``reviews.json`` (a mapping of pull request to
    review record); ``prs`` is optional pull request data (a list or a mapping of
    pull requests) that fills in what an old record does not carry.  A record that
    cannot be used is skipped, never guessed.
    """
    records = reviews if isinstance(reviews, dict) else {}
    pr_data = _pr_index(prs)
    samples: List[Dict[str, Any]] = []
    skipped: Dict[str, int] = {}
    for key in sorted(records, key=lambda item: (len(str(item)), str(item))):
        record = records[key]
        if not isinstance(record, dict):
            skipped["not-a-record"] = skipped.get("not-a-record", 0) + 1
            continue
        verdict = verdict_of(record)
        if not verdict:
            skipped["no-verdict"] = skipped.get("no-verdict", 0) + 1
            continue
        rounds, source = formal_rounds(record)
        if rounds is None:
            skipped["unknown-rounds"] = skipped.get("unknown-rounds", 0) + 1
            continue
        number = record.get("pr")
        if number is None and str(key).isdigit():
            number = int(key)
        pr_info = pr_data.get(str(number)) or {}
        lane = str(record.get("lane") or "unknown")
        backend = str(record.get("backend") or pr_info.get("backend") or _pr_backend(pr_info) or "unknown")
        author = str(record.get("author_lane") or _author_lane(pr_info) or "unknown")
        blockers = _as_int(record.get("blockers"))
        follow_ups = _as_int(record.get("follow_ups"))
        samples.append({
            "pr": number,
            "issue": record.get("issue"),
            "lane": lane,
            "backend": backend,
            "author_lane": author,
            "verdict": verdict,
            "first_verdict": first_verdict_of(record),
            "rounds": rounds,
            "rounds_source": source,
            "first_round_pass": first_round_pass(record) is True,
            "blockers": blockers,
            "follow_ups": follow_ups,
            "followup_tickets": _followup_count(record),
            "rework_tasks": _as_int(record.get("rework_tasks")) or 0,
            "rebase_reviews": _as_int(record.get("rebase_reviews")) or 0,
            "at": record.get("at"),
            "week": week_of(record.get("at")),
        })

    weeks: Dict[str, List[Dict[str, Any]]] = {}
    for sample in samples:
        week = sample["week"]
        if not week:
            continue
        weeks.setdefault(week, []).append(sample)
    week_rows = []
    for week in sorted(weeks):
        lane_rows = _group(weeks[week], "lane")
        author_rows = _group(weeks[week], "author_lane")
        week_rows.append({
            "week": week,
            "reviews": len(weeks[week]),
            "lanes": lane_rows,
            "author_lanes": author_rows,
            "bottom_lane": _bottom(lane_rows),
            "bottom_author_lane": _bottom(author_rows),
        })
    bottom_lanes, bottom_author_lanes = _streaks(week_rows)

    totals = _aggregate(samples)
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "definitions": _definitions(),
        "targets": {
            "first_round_pass_rate": TARGET_FIRST_ROUND_RATE,
            "avg_review_rounds": TARGET_AVG_ROUNDS,
            "meets_first_round_target": totals["meets_first_round_target"],
            "meets_rounds_target": totals["meets_rounds_target"],
        },
        "totals": totals,
        "lanes": _group(samples, "lane"),
        "author_lanes": _group(samples, "author_lane"),
        "backends": _group(samples, "backend"),
        "weeks": week_rows,
        "bottom_lanes": bottom_lanes,
        "bottom_author_lanes": bottom_author_lanes,
        "skipped": dict(sorted(skipped.items())),
        "skipped_total": sum(skipped.values()),
    }


def _streaks(week_rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The lanes that were bottom in two consecutive weeks, one list per view.

    The reviewing lanes (the lane that wrote the verdict) and the author lanes (the
    lane whose pull request was reviewed) are two views: they are listed apart, a
    lane that is only bottom in one of them is not in the other list.

    The lists are a hint for the scheduler (it may dispatch less work to those
    lanes); this card does not change the dispatching itself.
    """
    found: Dict[str, Dict[str, List[str]]] = {"lane": {}, "author_lane": {}}
    for index in range(len(week_rows) - 1):
        first, second = week_rows[index], week_rows[index + 1]
        if not _weeks_are_consecutive(first["week"], second["week"]):
            continue
        for key in ("lane", "author_lane"):
            name = first["bottom_" + key]
            if not name or name != second["bottom_" + key]:
                continue
            weeks = found[key].setdefault(name, [])
            if second["week"] not in weeks:
                weeks.append(second["week"])
    return ([{"lane": name, "weeks": sorted(weeks)} for name, weeks in sorted(found["lane"].items())],
            [{"lane": name, "weeks": sorted(weeks)} for name, weeks in sorted(found["author_lane"].items())])


def _definitions() -> Dict[str, str]:
    """The measurement rules, written into the head of every report."""
    return {
        "first_round_pass": "首轮通过率: the pull request passed its first formal review round "
                             "(the first verdict was APPROVE). The dispatcher stores that first "
                             "verdict in first_verdict and never changes it, so a later verdict (a "
                             "new head, a re-review) cannot turn a failed first round into a pass. "
                             "A pull request that still sits in CHANGES counts as a first-round "
                             "failure: its first round already happened.",
        "review_rounds": "平均轮次: the mean number of formal review rounds of the reviewed pull requests. "
                         "One round is one review verdict on a pull request head. The dispatcher writes "
                         "review_rounds into every record; an old record that only carries rounds (the "
                         "changes rounds) is read as rounds + 1 for an approving verdict.",
        "rebase_reviews": "A rebase review (the re-review of a pull request whose head the dispatcher moved "
                          "with a rebase) is not a formal round: the code did not change, only its base did. "
                          "Those reviews are counted in rebase_reviews and left out of the round counts; "
                          "their verdict does not overwrite the first verdict either.",
        "legacy_records": "A historical record without any round count is skipped unless it carries an "
                          "APPROVE: that approve is read as a first-round pass (legacy-inferred). The count "
                          "of such samples is in legacy_samples, the skipped records in skipped.",
        "lanes": "The lanes are the reviewing lanes (the lane that wrote the verdict). author_lanes is the "
                 "view of the lanes whose pull requests were reviewed: that is the view the scheduler needs "
                 "when it wants to dispatch less work to a lane.",
        "backends": "The backend of the reviewing lane, as the dispatcher recorded it.",
        "blockers_follow_ups": "The BLOCKER and FOLLOW-UP counts of the review conclusions: the "
                               "[BLOCKER] and [FOLLOW-UP] entries of the findings list (the format the "
                               "lane reviews really use), or the BLOCKERS: n / FOLLOW-UPS: n summary lines "
                               "when the conclusion carries no such entry. The counts are bucketed into "
                               "0, 1, 2, 3+ and unknown. A missing count stays unknown, it is never guessed "
                               "from the prose.",
        "followup_tickets": "follow-up 开单量: the follow-up cards the dispatcher really opened for the "
                            "[FOLLOW-UP] findings of the review (the numbers of the follow-up issues are "
                            "in followup_issues). The rework task of a changes round is NOT a follow-up "
                            "card: it is queued on the original issue and counted in rework_tasks.",
        "rework_tasks": "The rework tasks the review queued on the original issue (one per changes round "
                        "that was handed back to the author lane).",
        "weeks": "The week of a review is the ISO week of the record's at stamp. The bottom lane of a week "
                 "is the lane with the lowest first-round pass rate (ties: the highest average round count, "
                 "then the lane name). bottom_lanes lists the lanes that were bottom in two consecutive weeks.",
        "targets": "首轮通过率 >= 60% and 平均轮次 <= 2 are the target lines of the lane review loop.",
    }


def render(report: Dict[str, Any]) -> str:
    """The one-screen summary of a metrics report."""
    if not isinstance(report, dict):
        return "no lane review metrics"
    lines: List[str] = []
    totals = report.get("totals") or {}
    rate = totals.get("first_round_pass_rate")
    average = totals.get("avg_rounds")
    lines.append("lane review metrics: %d review(s), first-round pass %s (target >= %.0f%%), "
                 "average rounds %s (target <= %.2f)"
                 % (totals.get("reviews") or 0,
                    "n/a" if rate is None else "%.1f%%" % (rate * 100),
                    TARGET_FIRST_ROUND_RATE * 100,
                    "n/a" if average is None else "%.2f" % average,
                    TARGET_AVG_ROUNDS))
    lanes = report.get("lanes") or {}
    if lanes:
        lines.append("  %-12s %7s %10s %11s %10s %8s %s"
                     % ("lane", "reviews", "1st-pass", "avg-rounds", "follow-ups", "rework", "target"))
        for name, row in lanes.items():
            row_rate = row.get("first_round_pass_rate")
            row_average = row.get("avg_rounds")
            ok = bool(row.get("meets_first_round_target")) and bool(row.get("meets_rounds_target"))
            lines.append("  %-12s %7d %9s %11s %10d %8d %s"
                         % (name, row.get("reviews") or 0,
                            "n/a" if row_rate is None else "%.1f%%" % (row_rate * 100),
                            "n/a" if row_average is None else "%.2f" % row_average,
                            row.get("followup_tickets") or 0,
                            row.get("rework_tasks") or 0,
                            "ok" if ok else "below target"))
    bottom = report.get("bottom_lanes") or []
    if bottom:
        lines.append("  bottom two weeks (reviewing lanes): "
                     + ", ".join("%s (%s)" % (row["lane"], ",".join(row["weeks"])) for row in bottom))
    bottom_author = report.get("bottom_author_lanes") or []
    if bottom_author:
        lines.append("  bottom two weeks (author lanes): "
                     + ", ".join("%s (%s)" % (row["lane"], ",".join(row["weeks"])) for row in bottom_author))
    skipped = report.get("skipped_total") or 0
    if skipped:
        lines.append("  skipped records: %d %s" % (skipped, report.get("skipped") or {}))
    return "\n".join(lines)
