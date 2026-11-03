#!/usr/bin/env python3
"""Lane verdict versus tester shadow verdict: one append-only divergence log (Refs #1789).

The merge gate of Epic #1785 may only switch from the lane review to the tester
verdict when the two sides have been measured side by side for a while.  This
tool is the bookkeeping of that shadow period: every reviewed pull request gets
one round per review, and that round holds the lane verdict next to the tester
shadow verdict.  The final outcome of the round is appended later as a second
event, so the file is never rewritten and stays a faithful history of what was
judged, when, and who turned out to be right.

Two event kinds share one JSON Lines file:

``verdict``
    ``{"schema": "phanes.review-divergence/1", "kind": "verdict", "pr": 1751,
    "head": "a141b7b371bb3e3d4a158d82bdf067fc4c926aff", "round": 1,
    "lane_verdict": "CHANGES", "tester_verdict": "FAIL",
    "recorded_at": "2026-11-02T17:33:59Z"}``

``outcome``
    ``{"schema": "phanes.review-divergence/1", "kind": "outcome", "pr": 1751,
    "head": "a141b7b371bb3e3d4a158d82bdf067fc4c926aff", "round": 1,
    "outcome": "re_review", "outcome_at": "2026-11-02T18:19:28Z",
    "correct": "both", "backfilled_at": "2026-11-02T18:25:00Z"}``

A *round* is the pair of events with the same key ``(pr, head, round)``.  The
last event of a kind wins, so a corrected backfill is a new event and never an
edit of an old line.  Several rounds of one pull request are several keys and
never overwrite each other.

Usage:
  review-divergence-log.py append   --pr N --head SHA --lane APPROVE --tester PASS
  review-divergence-log.py backfill --pr N --head SHA --outcome merged
  review-divergence-log.py pending
  review-divergence-log.py stats
  review-divergence-log.py list

The log path comes from ``--log``, then ``$REVIEW_DIVERGENCE_LOG``, then
``$OPENSWARM_HOME/review-divergence.jsonl``, then
``~/.claude/openswarm/review-divergence.jsonl``.  Exit code 2 means the input
was rejected or the log is malformed; a malformed line is never skipped
silently.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys


SCHEMA = "phanes.review-divergence/1"
LOG_NAME = "review-divergence.jsonl"

LANE_VERDICTS = ("APPROVE", "CHANGES")
TESTER_VERDICTS = ("PASS", "FAIL", "INSUFFICIENT")
OUTCOMES = ("merged", "closed", "re_review")
CORRECT_VALUES = ("both", "lane", "tester", "neither", "undecided")

# The lane verdict and the tester verdict answer the same question on two
# scales: APPROVE means the round may pass, CHANGES means it may not.
LANE_SIGNS = {"APPROVE": "positive", "CHANGES": "negative"}
TESTER_SIGNS = {"PASS": "positive", "FAIL": "negative", "INSUFFICIENT": "abstain"}
# A merged pull request was good enough; a closed one and a round that sent the
# pull request back for another review were not.
POSITIVE_OUTCOMES = ("merged",)

LANE_ALIASES = {
    "approve": "APPROVE", "approved": "APPROVE", "通过": "APPROVE", "放行": "APPROVE",
    "changes": "CHANGES", "changes_requested": "CHANGES", "request_changes": "CHANGES",
    "不通过": "CHANGES", "要求修改": "CHANGES",
}
TESTER_ALIASES = {
    "pass": "PASS", "passed": "PASS", "通过": "PASS",
    "fail": "FAIL", "failed": "FAIL", "不通过": "FAIL",
    "insufficient": "INSUFFICIENT", "insufficient_evidence": "INSUFFICIENT",
    "证据不足": "INSUFFICIENT",
}
OUTCOME_ALIASES = {
    "merged": "merged", "merge": "merged", "合并": "merged",
    "closed": "closed", "close": "closed", "关闭": "closed",
    "re_review": "re_review", "re-review": "re_review", "rereview": "re_review",
    "再审": "re_review", "再审查": "re_review",
}

HEAD_RE = re.compile(r"^[0-9a-f]{7,40}$")


# ------------------------------------------------------------------ helpers

def log_path(explicit=None):
    """Return the log file: --log, then the environment, then the fleet home."""
    if explicit:
        return Path(explicit).expanduser()
    named = os.environ.get("REVIEW_DIVERGENCE_LOG")
    if named:
        return Path(named).expanduser()
    home = os.environ.get("OPENSWARM_HOME")
    if home:
        return Path(home).expanduser() / LOG_NAME
    return Path.home() / ".claude" / "openswarm" / LOG_NAME


def parse_timestamp(value, field="timestamp"):
    """Return an aware UTC datetime; a naive or unreadable stamp is an error."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("%s is required" % field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("%s must be an ISO-8601 timestamp, got %r" % (field, value)) from exc
    if parsed.tzinfo is None:
        raise ValueError("%s must carry an explicit timezone, got %r" % (field, value))
    return parsed.astimezone(timezone.utc)


def iso(moment):
    """Return the ISO-8601 UTC spelling of one instant."""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def now_iso():
    """Return the current instant as an ISO-8601 UTC stamp."""
    return iso(datetime.now(timezone.utc))


def require_pr(value):
    """Return the pull request number; it must be a positive integer."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("pr must be a positive integer, got %r" % (value,)) from exc
    if number <= 0:
        raise ValueError("pr must be a positive integer, got %r" % (value,))
    return number


def require_head(value):
    """Return the reviewed head sha in lower case; 7 to 40 hex characters."""
    text = str(value or "").strip().lower()
    if not HEAD_RE.match(text):
        raise ValueError("head must be 7 to 40 hex characters, got %r" % (value,))
    return text


def require_round(value):
    """Return the review round number; it must be a positive integer."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("round must be a positive integer, got %r" % (value,)) from exc
    if number < 1:
        raise ValueError("round must be a positive integer, got %r" % (value,))
    return number


def normalize(value, table, allowed, label):
    """Return the canonical spelling of a verdict or an outcome."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("%s is required" % label)
    word = table.get(text.lower().replace(" ", "_")) or table.get(text)
    if word not in allowed:
        raise ValueError("%s must be one of %s, got %r" % (label, "/".join(allowed), value))
    return word


def lane_verdict(value):
    """Return the lane verdict (APPROVE or CHANGES)."""
    return normalize(value, LANE_ALIASES, LANE_VERDICTS, "lane verdict")


def tester_verdict(value):
    """Return the tester shadow verdict (PASS, FAIL or INSUFFICIENT)."""
    return normalize(value, TESTER_ALIASES, TESTER_VERDICTS, "tester verdict")


def outcome_name(value):
    """Return the final outcome of a round (merged, closed or re_review)."""
    return normalize(value, OUTCOME_ALIASES, OUTCOMES, "outcome")


# ------------------------------------------------------------------- events

def validate_event(event, where="event"):
    """Return one validated event; anything unexpected raises ValueError."""
    if not isinstance(event, dict):
        raise ValueError("%s is not a JSON object" % where)
    if event.get("schema") != SCHEMA:
        raise ValueError("%s carries an unknown schema %r" % (where, event.get("schema")))
    kind = event.get("kind")
    if kind not in ("verdict", "outcome"):
        raise ValueError("%s carries an unknown kind %r" % (where, kind))
    event = dict(event)
    event["pr"] = require_pr(event.get("pr"))
    event["head"] = require_head(event.get("head"))
    event["round"] = require_round(event.get("round"))
    if kind == "verdict":
        event["lane_verdict"] = lane_verdict(event.get("lane_verdict"))
        event["tester_verdict"] = tester_verdict(event.get("tester_verdict"))
        event["recorded_at"] = iso(parse_timestamp(event.get("recorded_at"), "recorded_at"))
    else:
        event["outcome"] = outcome_name(event.get("outcome"))
        event["outcome_at"] = iso(parse_timestamp(event.get("outcome_at"), "outcome_at"))
        event["correct"] = str(event.get("correct") or "")
        if event["correct"] not in CORRECT_VALUES:
            raise ValueError("%s carries an unknown correct value %r" % (where, event.get("correct")))
        event["backfilled_at"] = iso(parse_timestamp(event.get("backfilled_at"), "backfilled_at"))
    return event


def read_events(path):
    """Return every event of the log; a malformed line is an error, not a skip."""
    location = Path(path)
    if not location.exists():
        return []
    events = []
    for number, line in enumerate(location.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ValueError("%s line %d is not JSON: %s" % (location, number, exc)) from exc
        events.append(validate_event(raw, "%s line %d" % (location, number)))
    return events


def append_event(path, event):
    """Append one event to the log with a single write, so parallel writers cannot interleave."""
    location = Path(path)
    if location.parent and not location.parent.exists():
        location.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"
    handle = os.open(str(location), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(handle, line.encode("utf-8"))
    finally:
        os.close(handle)
    return event


# ------------------------------------------------------------------- rounds

def key_of(event):
    """Return the key of a round: pull request, reviewed head and round number."""
    return (event["pr"], event["head"], event["round"])


def fold(events):
    """Return the effective record per round; the last event of a kind wins."""
    records = {}
    for event in events:
        key = key_of(event)
        record = records.setdefault(key, {"pr": key[0], "head": key[1], "round": key[2],
                                         "lane_verdict": None, "tester_verdict": None,
                                         "recorded_at": None, "outcome": None})
        if event["kind"] == "verdict":
            record.update({"lane_verdict": event["lane_verdict"],
                           "tester_verdict": event["tester_verdict"],
                           "recorded_at": event["recorded_at"],
                           "evidence": event.get("evidence"), "note": event.get("note")})
        else:
            record.update({"outcome": event["outcome"], "outcome_at": event["outcome_at"],
                           "correct": event["correct"], "backfilled_at": event["backfilled_at"],
                           "outcome_note": event.get("note")})
    return records


def sorted_records(records):
    """Return the rounds in a stable order: pull request, round, head."""
    return [records[key] for key in sorted(records, key=lambda item: (item[0], item[2], item[1]))]


def outcome_sign(outcome):
    """Return the sign of an outcome: positive when the pull request was merged."""
    return "positive" if outcome in POSITIVE_OUTCOMES else "negative"


def side_correct(sign, outcome):
    """Return whether one side called the outcome right; None means it abstained."""
    if sign == "abstain":
        return None
    return sign == outcome_sign(outcome)


def who_was_right(lane, tester, outcome):
    """Return the attribution of one round: (who, lane_right, tester_right).

    ``undecided`` means the tester abstained (insufficient evidence), so no
    side by side attribution is possible; the lane is still scored on its own.
    """
    lane_ok = side_correct(LANE_SIGNS[lane], outcome)
    tester_ok = side_correct(TESTER_SIGNS[tester], outcome)
    if tester_ok is None:
        return "undecided", lane_ok, tester_ok
    if lane_ok and tester_ok:
        return "both", lane_ok, tester_ok
    if lane_ok:
        return "lane", lane_ok, tester_ok
    if tester_ok:
        return "tester", lane_ok, tester_ok
    return "neither", lane_ok, tester_ok


def pending_records(records):
    """Return the rounds that hold a verdict but no outcome."""
    return [record for record in sorted_records(records) if record.get("outcome") is None]


# ------------------------------------------------------------------ commands

def cmd_append(args):
    """Append one round with the lane verdict next to the tester shadow verdict."""
    path = log_path(args.log)
    pr = require_pr(args.pr)
    head = require_head(args.head)
    lane = lane_verdict(args.lane)
    tester = tester_verdict(args.tester)
    recorded_at = iso(parse_timestamp(args.recorded_at, "recorded_at")) if args.recorded_at else now_iso()
    records = fold(read_events(path))
    same_head = [record for record in records.values() if record["pr"] == pr and record["head"] == head]
    if args.round is None:
        if same_head:
            highest = max(record["round"] for record in same_head)
            raise ValueError("PR %d head %s already has round %d; pass --round %d to record "
                             "another round of this head" % (pr, head, highest, highest + 1))
        rounds = [record["round"] for record in records.values() if record["pr"] == pr]
        number = max(rounds) + 1 if rounds else 1
    else:
        number = require_round(args.round)
    key = (pr, head, number)
    if key in records and not args.force:
        raise ValueError("PR %d head %s round %d is already recorded; use --force to append "
                         "a corrected record" % (pr, head, number))
    event = {"schema": SCHEMA, "kind": "verdict", "pr": pr, "head": head, "round": number,
             "lane_verdict": lane, "tester_verdict": tester, "recorded_at": recorded_at}
    if args.evidence:
        event["evidence"] = str(args.evidence).strip()
    if args.note:
        event["note"] = str(args.note).strip()
    append_event(path, event)
    print(json.dumps(event, sort_keys=True, ensure_ascii=False))
    return 0


def cmd_backfill(args):
    """Append the final outcome of one round and the attribution that follows from it."""
    path = log_path(args.log)
    pr = require_pr(args.pr)
    head = require_head(args.head)
    outcome = outcome_name(args.outcome)
    outcome_at = iso(parse_timestamp(args.outcome_at, "outcome_at")) if args.outcome_at else now_iso()
    backfilled_at = iso(parse_timestamp(args.backfilled_at, "backfilled_at")) \
        if getattr(args, "backfilled_at", None) else now_iso()
    records = fold(read_events(path))
    candidates = [record for record in records.values()
                  if record["pr"] == pr and record["head"] == head and record.get("lane_verdict")]
    if args.round is None:
        if not candidates:
            raise ValueError("PR %d head %s has no recorded verdict to backfill" % (pr, head))
        waiting = [record for record in candidates if record.get("outcome") is None]
        if len(waiting) == 1:
            number = waiting[0]["round"]
        elif len(candidates) == 1:
            number = candidates[0]["round"]
        elif not waiting:
            raise ValueError("PR %d head %s has %d rounds and all of them are backfilled; "
                             "pass --round --force to correct one"
                             % (pr, head, len(candidates)))
        else:
            raise ValueError("PR %d head %s has %d rounds to backfill; pass --round"
                             % (pr, head, len(waiting)))
    else:
        number = require_round(args.round)
    key = (pr, head, number)
    record = records.get(key)
    if not record or not record.get("lane_verdict"):
        raise ValueError("PR %d head %s round %d has no recorded verdict to backfill" % (pr, head, number))
    if record.get("outcome") is not None and not args.force:
        raise ValueError("PR %d head %s round %d is already backfilled as %s; use --force to correct it"
                         % (pr, head, number, record["outcome"]))
    who, _, _ = who_was_right(record["lane_verdict"], record["tester_verdict"], outcome)
    event = {"schema": SCHEMA, "kind": "outcome", "pr": pr, "head": head, "round": number,
             "outcome": outcome, "outcome_at": outcome_at, "correct": who, "backfilled_at": backfilled_at}
    if args.note:
        event["note"] = str(args.note).strip()
    append_event(path, event)
    print(json.dumps(event, sort_keys=True, ensure_ascii=False))
    return 0


def cmd_pending(args):
    """List the rounds that still wait for their outcome."""
    records = fold(read_events(log_path(args.log)))
    waiting = pending_records(records)
    if args.pr is not None:
        number = require_pr(args.pr)
        waiting = [record for record in waiting if record["pr"] == number]
    if args.json:
        print(json.dumps({"pending": waiting, "count": len(waiting)}, sort_keys=True,
                         ensure_ascii=False, indent=2))
    else:
        for record in waiting:
            print("PR #%d round %d head %s lane=%s tester=%s recorded=%s"
                  % (record["pr"], record["round"], record["head"], record["lane_verdict"],
                     record["tester_verdict"], record["recorded_at"]))
        print("%d round(s) without an outcome" % len(waiting))
    return 0


def cmd_list(args):
    """Print the folded rounds of the log."""
    records = fold(read_events(log_path(args.log)))
    rows = sorted_records(records)
    if args.pr is not None:
        number = require_pr(args.pr)
        rows = [row for row in rows if row["pr"] == number]
    if args.json:
        print(json.dumps({"rounds": rows, "count": len(rows)}, sort_keys=True,
                         ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print("PR #%d round %d head %s lane=%s tester=%s outcome=%s correct=%s"
                  % (row["pr"], row["round"], row["head"], row["lane_verdict"], row["tester_verdict"],
                     row.get("outcome") or "-", row.get("correct") or "-"))
        print("%d round(s)" % len(rows))
    return 0


def build_stats(records):
    """Return the report of one log: samples, accuracy per side and the agreement rate.

    Every rate uses the backfilled rounds as its denominator.  A round without an
    outcome is listed as pending and never counts as a sample, and a tester
    verdict of INSUFFICIENT abstains: it is counted as abstained and never as a
    wrong answer.  A round that holds an outcome but no pair of verdicts cannot
    be scored at all: it is listed as incomplete and never counts as a sample.
    """
    sides = {"lane": {"samples": 0, "correct": 0, "abstained": 0},
             "tester": {"samples": 0, "correct": 0, "abstained": 0}}
    outcomes = {name: 0 for name in OUTCOMES}
    agreement = {"comparable": 0, "agreed": 0, "diverged": 0, "not_comparable": 0}
    divergence = Counter()
    attribution = Counter()
    pending = []
    incomplete = []
    inconsistent = []
    backfilled = 0
    for record in sorted_records(records):
        if not record.get("lane_verdict") or not record.get("tester_verdict"):
            incomplete.append({"pr": record["pr"], "head": record["head"], "round": record["round"],
                               "lane_verdict": record.get("lane_verdict"),
                               "tester_verdict": record.get("tester_verdict"),
                               "outcome": record.get("outcome")})
            continue
        if record.get("outcome") is None:
            pending.append({"pr": record["pr"], "head": record["head"], "round": record["round"],
                            "lane_verdict": record["lane_verdict"],
                            "tester_verdict": record["tester_verdict"],
                            "recorded_at": record["recorded_at"]})
            continue
        backfilled += 1
        outcomes[record["outcome"]] += 1
        who, lane_ok, tester_ok = who_was_right(record["lane_verdict"], record["tester_verdict"],
                                                record["outcome"])
        attribution[who] += 1
        if record.get("correct") != who:
            inconsistent.append({"pr": record["pr"], "head": record["head"], "round": record["round"],
                                 "recorded": record.get("correct"), "computed": who})
        for name, ok in (("lane", lane_ok), ("tester", tester_ok)):
            if ok is None:
                sides[name]["abstained"] += 1
            else:
                sides[name]["samples"] += 1
                sides[name]["correct"] += 1 if ok else 0
        if tester_ok is None:
            agreement["not_comparable"] += 1
        else:
            agreement["comparable"] += 1
            if (record["lane_verdict"] == "APPROVE") == (record["tester_verdict"] == "PASS"):
                agreement["agreed"] += 1
            else:
                agreement["diverged"] += 1
        divergence["%s|%s" % (record["lane_verdict"], record["tester_verdict"])] += 1
    for name, side in sides.items():
        side["accuracy"] = (side["correct"] / side["samples"]) if side["samples"] else None
    agreement["agreement_rate"] = (agreement["agreed"] / agreement["comparable"]) \
        if agreement["comparable"] else None
    return {
        "rounds": {"total": len(records), "backfilled": backfilled, "pending": len(pending),
                   "incomplete": len(incomplete)},
        "outcomes": outcomes,
        "sides": sides,
        "agreement": agreement,
        "attribution": {name: attribution.get(name, 0) for name in CORRECT_VALUES},
        "divergence": [{"lane_verdict": key.split("|")[0], "tester_verdict": key.split("|")[1],
                        "count": count} for key, count in sorted(divergence.items())],
        "pending": pending,
        "incomplete": incomplete,
        "inconsistent_backfills": inconsistent,
    }


def cmd_stats(args):
    """Report the accuracy of both sides and the agreement rate of the log."""
    path = log_path(args.log)
    records = fold(read_events(path))
    if args.pr is not None:
        number = require_pr(args.pr)
        records = {key: record for key, record in records.items() if record[0] == number}
    report = build_stats(records)
    report = {"log": str(path), "generated_at": now_iso(), **report}
    if args.json:
        print(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2))
    else:
        rounds = report["rounds"]
        print("rounds: %d total, %d backfilled, %d pending, %d incomplete"
              % (rounds["total"], rounds["backfilled"], rounds["pending"], rounds["incomplete"]))
        for name in ("lane", "tester"):
            side = report["sides"][name]
            accuracy = "n/a" if side["accuracy"] is None else "%.3f" % side["accuracy"]
            print("%s: %d sample(s), %d correct, accuracy %s, %d abstained"
                  % (name, side["samples"], side["correct"], accuracy, side["abstained"]))
        agree = report["agreement"]
        rate = "n/a" if agree["agreement_rate"] is None else "%.3f" % agree["agreement_rate"]
        print("agreement: %d comparable, %d agreed, %d diverged, rate %s, %d not comparable"
              % (agree["comparable"], agree["agreed"], agree["diverged"], rate,
                 agree["not_comparable"]))
        for row in report["divergence"]:
            print("divergence: lane=%s tester=%s count=%d"
                  % (row["lane_verdict"], row["tester_verdict"], row["count"]))
        print("attribution: %s"
              % ", ".join("%s %d" % (name, count) for name, count in report["attribution"].items()))
        for row in report["pending"]:
            print("pending: PR #%d round %d head %s" % (row["pr"], row["round"], row["head"]))
        for row in report["incomplete"]:
            print("incomplete: PR #%d round %d head %s has no pair of verdicts"
                  % (row["pr"], row["round"], row["head"]))
        for row in report["inconsistent_backfills"]:
            print("inconsistent backfill: PR #%d round %d recorded %s, computed %s"
                  % (row["pr"], row["round"], row["recorded"], row["computed"]))
    return 0


# --------------------------------------------------------------------- main

def self_test():
    """Run a tiny built-in check of the rules the log depends on."""
    assert who_was_right("CHANGES", "FAIL", "re_review") == ("both", True, True)
    assert who_was_right("APPROVE", "FAIL", "merged") == ("lane", True, False)
    assert who_was_right("CHANGES", "PASS", "merged") == ("tester", False, True)
    assert who_was_right("APPROVE", "PASS", "closed") == ("neither", False, False)
    assert who_was_right("CHANGES", "INSUFFICIENT", "closed") == ("undecided", True, None)
    records = fold([
        validate_event({"schema": SCHEMA, "kind": "verdict", "pr": 1, "head": "a" * 40, "round": 1,
                        "lane_verdict": "APPROVE", "tester_verdict": "PASS",
                        "recorded_at": "2026-11-02T00:00:00Z"}),
        validate_event({"schema": SCHEMA, "kind": "verdict", "pr": 1, "head": "b" * 40, "round": 2,
                        "lane_verdict": "CHANGES", "tester_verdict": "INSUFFICIENT",
                        "recorded_at": "2026-11-02T01:00:00Z"}),
    ])
    report = build_stats(records)
    assert report["rounds"] == {"total": 2, "backfilled": 0, "pending": 2, "incomplete": 0}
    assert report["sides"]["lane"]["samples"] == 0
    orphan = fold([validate_event({"schema": SCHEMA, "kind": "outcome", "pr": 2, "head": "c" * 40,
                                   "round": 1, "outcome": "merged",
                                   "outcome_at": "2026-11-02T02:00:00Z", "correct": "both",
                                   "backfilled_at": "2026-11-02T02:00:00Z"})])
    report = build_stats(orphan)
    assert report["rounds"] == {"total": 1, "backfilled": 0, "pending": 0, "incomplete": 1}
    assert report["sides"]["lane"]["samples"] == 0
    print("OK: review divergence log self-test")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lane verdict versus tester shadow verdict log.")
    parser.add_argument("--log", help="path of the log (default: $REVIEW_DIVERGENCE_LOG, "
                                     "$OPENSWARM_HOME/review-divergence.jsonl or "
                                     "~/.claude/openswarm/review-divergence.jsonl)")
    parser.add_argument("--self-test", action="store_true", help="run the built-in check and exit")
    sub = parser.add_subparsers(dest="command")

    append = sub.add_parser("append", help="append one round with both verdicts")
    append.add_argument("--log", dest="log_sub", help=argparse.SUPPRESS)
    append.add_argument("--pr", required=True)
    append.add_argument("--head", required=True)
    append.add_argument("--lane", required=True, help="lane verdict: APPROVE or CHANGES")
    append.add_argument("--tester", required=True, help="tester shadow verdict: PASS, FAIL or INSUFFICIENT")
    append.add_argument("--round", type=int, help="review round of this PR (default: the next round)")
    append.add_argument("--recorded-at", help="ISO-8601 time of the record (default: now, UTC)")
    append.add_argument("--evidence", help="URL of the published lane review or of the shadow report")
    append.add_argument("--note", help="free text note")
    append.add_argument("--force", action="store_true", help="replace the record of the same key")

    backfill = sub.add_parser("backfill", help="append the final outcome of one round")
    backfill.add_argument("--log", dest="log_sub", help=argparse.SUPPRESS)
    backfill.add_argument("--pr", required=True)
    backfill.add_argument("--head", required=True)
    backfill.add_argument("--outcome", required=True, help="merged, closed or re_review")
    backfill.add_argument("--round", type=int, help="review round (default: the only pending round)")
    backfill.add_argument("--outcome-at", help="ISO-8601 time of the outcome (default: now, UTC)")
    backfill.add_argument("--backfilled-at", help="ISO-8601 time of the backfill (default: now, UTC)")
    backfill.add_argument("--note", help="free text note")
    backfill.add_argument("--force", action="store_true", help="append a second outcome for the same key")

    pending = sub.add_parser("pending", help="list the rounds without an outcome")
    pending.add_argument("--log", dest="log_sub", help=argparse.SUPPRESS)
    pending.add_argument("--pr", type=int)
    pending.add_argument("--json", action="store_true")

    listing = sub.add_parser("list", help="print the folded rounds")
    listing.add_argument("--log", dest="log_sub", help=argparse.SUPPRESS)
    listing.add_argument("--pr", type=int)
    listing.add_argument("--json", action="store_true")

    stats = sub.add_parser("stats", help="accuracy per side and the agreement rate")
    stats.add_argument("--log", dest="log_sub", help=argparse.SUPPRESS)
    stats.add_argument("--pr", type=int)
    stats.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if not args.command:
        parser.error("a command is required")
    handlers = {"append": cmd_append, "backfill": cmd_backfill, "pending": cmd_pending,
                "list": cmd_list, "stats": cmd_stats}
    args.log = getattr(args, "log_sub", None) or args.log
    try:
        return handlers[args.command](args)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print("review-divergence-log: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
