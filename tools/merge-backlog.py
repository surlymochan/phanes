#!/usr/bin/env python3
"""The merge queue of the fleet: which open pull request waits for what (Refs #1696).

The 2026-11-02 night shift saw several pull requests that were approved or
green and still open.  Nothing said *where* they waited, so a queue that was
not moving looked exactly like a queue that was moving.

This tool prints the merge queue: every open pull request whose **card**
carries the ``lane-pr`` label, the stage it waits in (等首审 / 等复审 /
审查 CHANGES 待修复 / CI 未绿 / 已批待合并 / 等人工裁决) and how long it waits
there.  The rows that are over their threshold (two hours for an approved pull
request, a day for every other stage) are named on their own lines -- those
lines are what the brain log gets, because a backlog that is not named is a
backlog nobody reads.

It is the health check section "合并积压": the exit code is 1 when a row is
over its threshold, so the tool can be used as a check.

Usage:
  merge-backlog.py --prs FILE --issues FILE [--reviews FILE] [--ci-policy FILE]
                   [--brain-log FILE] [--now ISO] [--tz +08:00] [--json] [--self-test]
  merge-backlog.py --gh-live [--reviews FILE] [--brain-log FILE] [--tz +08:00]

``--prs`` is the JSON array of the open pull requests (``gh pr list --state
open --json number,title,url,labels,body,headRefName,headRefOid,isDraft,
mergeable,reviewDecision,statusCheckRollup,createdAt,updatedAt``), ``--issues``
the JSON array of the cards that carry the ``lane-pr`` label (``gh issue list
--label lane-pr --state open --json number,title,state,labels,body,updatedAt``)
and ``--reviews`` the review ledger of the dispatcher (``reviews.json``).
``--gh-live`` reads both lists with the ``gh`` CLI instead of a snapshot.

The pure judgement lives in ``tools/merge_backlog.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "merge_backlog.py"

spec = importlib.util.spec_from_file_location("merge_backlog", MODULE_PATH)
merge_backlog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge_backlog)

GH_TIMEOUT = 60

PR_FIELDS = ("number,title,url,labels,body,headRefName,headRefOid,isDraft,"
              "mergeable,reviewDecision,statusCheckRollup,createdAt,updatedAt")
CARD_FIELDS = "number,title,state,labels,body,updatedAt"


def read_json(path, expected):
    """Read a JSON file and check its shape."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, expected):
        raise ValueError("%s: expected %s" % (path, expected.__name__))
    return data


def parse_offset(text):
    """Return the seconds east of UTC of a timezone flag (``+08:00``, ``Z``, ``UTC``)."""
    if text is None:
        return None
    value = str(text).strip()
    if not value:
        return None
    if value.upper() in ("Z", "UTC"):
        return 0
    if value[0] in "+-":
        sign = -1 if value[0] == "-" else 1
        value = value[1:]
    parts = value.split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except (IndexError, ValueError):
        raise ValueError("invalid timezone: %r" % text)
    return sign * (hours * 3600 + minutes * 60)


def run_gh(arguments):
    """Run one ``gh`` command and return its JSON output."""
    result = subprocess.run(["gh"] + list(arguments), capture_output=True, text=True,
                            check=False, timeout=GH_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError("gh %s failed: %s" % (" ".join(arguments),
                                                 (result.stderr or "").strip()))
    return json.loads(result.stdout or "null")


def print_table(rows):
    """Print the queue: one line per waiting pull request."""
    for row in rows:
        print("#%s  %s  停留%s  卡#%s  %s" % (
            row.get("number"), row.get("stage_text"),
            merge_backlog.duration(row.get("dwell_s")), row.get("issue"),
            "; ".join(row.get("signals") or [])))


def brain_log_entry(rows, now):
    """The lines the brain log gets: the header of the reading and the names.

    A backlog that is not named in the brain log is a backlog nobody reads, so
    every row that is over its threshold gets a line of its own.
    """
    lines = ["合并积压 @ %s count=%d 超时=%d" % (
        merge_backlog.iso(now) or "-", len(rows),
        len(merge_backlog.overdue(rows)))]
    lines.extend(merge_backlog.brain_log_lines(rows))
    return lines


def append_brain_log(rows, path, now):
    """Append the naming lines to a brain log file."""
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as stream:
        for line in brain_log_entry(rows, now):
            stream.write(line + "\n")
    return target


def self_test():
    """Offline self test: one row per stage and the naming of the overdue ones."""
    prs = [
        {"number": 1, "state": "OPEN", "body": "Closes #11", "createdAt": "2026-11-01T00:00:00Z",
         "updatedAt": "2026-11-01T00:00:00Z", "statusCheckRollup": []},
        {"number": 2, "state": "OPEN", "body": "Closes #12", "labels": [{"name": "needs-human"}],
         "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-01T00:00:00Z",
         "statusCheckRollup": [{"context": "external-lane/contract", "state": "FAILURE"}]},
        {"number": 3, "state": "MERGED", "body": "Closes #13", "mergedAt": "2026-11-01T00:00:00Z",
         "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-01T00:00:00Z"},
    ]
    cards = [{"number": 11, "state": "OPEN", "labels": [{"name": "lane-pr"}]},
             {"number": 12, "state": "OPEN", "labels": [{"name": "lane-pr"}]},
             {"number": 13, "state": "CLOSED", "labels": [{"name": "lane-pr"}]}]
    # The ledger of the dispatcher: the moment the needs-human label went on.
    # The naive stamps of the ledger are read as UTC here (offset=0): the self
    # test has to give the same result on every host, and the fleet host runs
    # on UTC+8.
    ledger = {"2": {"sha": "b" * 40, "verdict": "CHANGES", "at": "2026-11-01 00:00:00",
                    "needs_human_at": "2026-11-01 00:00:00"}}
    now = merge_backlog.epoch("2026-11-03T00:00:00Z")
    rows = merge_backlog.backlog(prs, cards, ledger, now, offset=0)
    stages = {row["number"]: row["stage"] for row in rows}
    assert stages == {1: merge_backlog.STAGE_FIRST_REVIEW,
                      2: merge_backlog.STAGE_HUMAN}, stages
    assert rows[1]["stage_text"] == "等人工裁决（薄卡豁免）", rows[1]
    assert merge_backlog.brain_log_lines(rows) == [
        "合并积压点名 #1 等首审 停留48h00m（阈值 24h00m）",
        "合并积压点名 #2 等人工裁决（薄卡豁免） 停留48h00m（阈值 24h00m）"], rows
    print("merge backlog self-test: the queue, the stages and the naming all passed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prs", help="JSON file with the open pull requests")
    parser.add_argument("--issues", help="JSON file with the cards that carry lane-pr")
    parser.add_argument("--reviews", help="the review ledger of the dispatcher (reviews.json)")
    parser.add_argument("--ci-policy", help="JSON file with the CI ignore/advisory policy")
    parser.add_argument("--brain-log", help="brain log file the naming lines are appended to")
    parser.add_argument("--gh-live", action="store_true",
                        help="read the open pull requests and the lane-pr cards with gh")
    parser.add_argument("--now", help="the time the reading belongs to (ISO-8601), default: now")
    parser.add_argument("--tz", help="the timezone of the naive source stamps (+08:00), default: local")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if not args.gh_live and not (args.prs and args.issues):
        parser.error("--prs and --issues are required (or --gh-live)")
    try:
        offset = parse_offset(args.tz)
        if args.gh_live:
            prs = run_gh(["pr", "list", "--state", "open", "--limit", "200",
                          "--json", PR_FIELDS])
            cards = run_gh(["issue", "list", "--label", "lane-pr", "--state", "open",
                            "--limit", "500", "--json", CARD_FIELDS])
            if not isinstance(prs, list) or not isinstance(cards, list):
                raise ValueError("the gh reading must hold JSON arrays")
        else:
            prs = read_json(args.prs, list)
            cards = read_json(args.issues, list)
        ledger = read_json(args.reviews, dict) if args.reviews else None
        policy = read_json(args.ci_policy, dict) if args.ci_policy else {}
        now = merge_backlog.epoch(args.now) if args.now else merge_backlog.epoch(time.time())
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    rows = merge_backlog.backlog(prs, cards, ledger, now, policy=policy, offset=offset,
                                  review_source=ledger is not None)
    if args.as_json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        print_table(rows)
        for line in merge_backlog.brain_log_lines(rows):
            print(line)
    if args.brain_log:
        append_brain_log(rows, args.brain_log, now)
    return int(bool(merge_backlog.overdue(rows)))


if __name__ == "__main__":
    raise SystemExit(main())
