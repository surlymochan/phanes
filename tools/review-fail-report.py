#!/usr/bin/env python3
"""Summarize review failures from reviews.json, autodispatch.log and run results."""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


FAIL_RE = re.compile(
    r"review PR #(?P<pr>\d+) on (?P<lane>\S+) .*?\brun (?P<run>[^)\s]+)\)"
    r" failed \((?P<status>[^/)]+?)/(?P<class>[^)]+)\).*?\(fails=(?P<fails>\d+)\)", re.I
)
LEGACY_RE = re.compile(
    r"review PR #(?P<pr>\d+) by (?P<lane>\S+):.*?\((?P<status>[^/)]+?)/(?P<class>[^)]+)\).*?"
    r"(?:fail #(?P<fails>\d+)|not counted)", re.I
)
PR_RE = re.compile(r"review PR #(?P<pr>\d+)", re.I)


def load_reviews(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("reviews.json must contain an object")
    return data


def parse_log(path):
    if isinstance(path, (list, tuple)):
        return parse_lines(path)
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    return parse_lines(lines)


def parse_lines(lines):
    failures, uncounted = {}, Counter()
    for line in lines:
        m = FAIL_RE.search(line) or LEGACY_RE.search(line)
        if m:
            pr = m.group("pr")
            is_uncounted = "not counted" in line.lower()
            if is_uncounted:
                uncounted[pr] += 1
                continue
            failures.setdefault(pr, []).append({
                "lane": m.group("lane"), "run": m.groupdict().get("run") or "",
                "error_class": m.group("class").strip(), "status": m.group("status").strip(),
                "at": line[:19] if re.match(r"\d{4}-\d\d-\d\d", line) else "",
                "fails": int(m.group("fails")) if m.groupdict().get("fails") else None,
            })
        elif "not counted" in line.lower() and (m := PR_RE.search(line)):
            uncounted[m.group("pr")] += 1
    return failures, uncounted


def summarize(reviews, log_path, runs=None, max_fails=4):
    history, uncounted = parse_log(log_path)
    prs = set(map(str, reviews)) | set(history) | set(uncounted)
    rows, totals = [], Counter()
    for pr in prs:
        current = reviews.get(pr, {})
        current = current if isinstance(current, dict) else {}
        fails = int(current.get("fails") or 0)
        events = history.get(pr, [])
        # Reviews state is authoritative; log-only data still offers a useful count.
        if pr not in reviews and events:
            fails = max((e["fails"] or 0 for e in events), default=0)
        lane = current.get("lane") or (events[-1]["lane"] if events else "")
        run = current.get("run") or (events[-1]["run"] if events else "")
        at = current.get("at") or (events[-1]["at"] if events else "")
        for event in events:
            totals[event["error_class"] or "unknown"] += 1
            if runs and event["run"]:
                result_path = Path(runs) / event["run"] / "result.json"
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    err_lines = str(result.get("error") or result.get("summary") or "").splitlines()
                    event["error"] = err_lines[0] if err_lines else ""
                except (OSError, ValueError, AttributeError):
                    event["error"] = ""
        if not events and lane and run:
            events = [{"lane": lane, "run": run, "error_class": "", "at": at}]
        rows.append({"pr": pr, "fails": fails, "remaining": max_fails - fails,
                     "risk": fails >= max_fails - 1, "lane": lane, "run": run,
                     "at": at, "uncounted": uncounted[pr], "failures": events})
    rows.sort(key=lambda row: (row["remaining"], int(row["pr"]) if row["pr"].isdigit() else row["pr"]))
    return rows, totals


def self_test():
    reviews = {"1572": {"fails": 2, "lane": "reviewer-a", "run": "r2", "at": "2026-01-02"},
               "1600": {"fails": 3}}
    lines = [
        "2026-01-01T00:00:00 review PR #1572 on reviewer-a (review run r1) failed (failed/vm_paused): paused (fails=1)",
        "2026-01-02T00:00:00 review PR #1572 on reviewer-a (review run r2) failed (failed/exec_error): bad (fails=2)",
        "2026-01-03T00:00:00 review PR #1600 on reviewer-b (review run r3) failed (failed/timeout): timeout (fails=3)",
        "review PR #1572 on reviewer-a (review run r4) failed (failed/vm_paused): paused → not counted (fails=2)",
    ]
    rows, totals = summarize(reviews, lines, max_fails=4)
    by_pr = {row["pr"]: row for row in rows}
    assert len(by_pr["1572"]["failures"]) == 2
    assert by_pr["1572"]["uncounted"] == 1
    assert by_pr["1572"]["risk"] is False
    assert by_pr["1600"]["risk"] is True
    assert by_pr["1600"]["remaining"] == 1
    assert totals == Counter({"vm_paused": 1, "exec_error": 1, "timeout": 1})
    print("OK: review fail report self-test")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviews", default="reviews.json")
    parser.add_argument("--log", default="autodispatch.log")
    parser.add_argument("--runs")
    parser.add_argument("--max-fails", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    try:
        rows, totals = summarize(load_reviews(args.reviews), args.log, args.runs, args.max_fails)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"max_fails": args.max_fails, "prs": rows, "error_classes": totals},
                         ensure_ascii=False, indent=2))
    else:
        for row in rows:
            flag = " AT_RISK" if row["risk"] else ""
            print(f"PR #{row['pr']}: fails={row['fails']} remaining={row['remaining']}{flag} "
                  f"uncounted={row['uncounted']} lane={row['lane']} run={row['run']} at={row['at']}")
            for event in row["failures"]:
                print(f"  {event.get('at', '')} {event['lane']} run={event['run']} "
                      f"{event['error_class']} {event.get('error', '')}".rstrip())
        print("error_class totals: " + ", ".join(f"{key}={value}" for key, value in sorted(totals.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
