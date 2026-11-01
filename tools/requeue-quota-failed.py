#!/usr/bin/env python3
"""Requeue issues incorrectly failed after backend quota/rate-limit errors (Refs #1541)."""
import argparse
import json
import re
import subprocess
from pathlib import Path


ENDED = re.compile(r"ended #(?P<n>\d+) without a commit \(run (?P<run>[^,]+), (?P<status>[^/]+)/(?P<class>[^)]+)\) → labelled lane-failed")
DISPATCHED = re.compile(r"dispatched #(?P<n>\d+)")


def plan(log_text, since=None):
    """Return issues whose latest end was a quota failure, unless later dispatched."""
    latest = {}
    for index, line in enumerate(log_text.splitlines()):
        clock = re.search(r"\b(\d{2}:\d{2})\b", line)
        if since and clock and clock.group(1) < since:
            continue
        ended = ENDED.search(line)
        if ended:
            latest[int(ended.group("n"))] = (index, ended.groupdict())
    rows = []
    for number, (index, event) in latest.items():
        if event["class"] not in {"rate_limit", "quota"}:
            continue
        later_dispatch = any(
            (match := DISPATCHED.search(line)) and int(match.group("n")) == number
            for line in log_text.splitlines()[index + 1:]
        )
        if not later_dispatch:
            rows.append({"number": number, **event})
    return sorted(rows, key=lambda row: row["number"])


def gh_runner(args):
    return subprocess.run(["gh", *args], text=True, capture_output=True, check=False)


def execute(repo, rows, apply=False, runner=gh_runner, output=print):
    changed = []
    for row in rows:
        n = row["number"]
        output(f"#{n}: quota/rate-limit ({row['status']}/{row['class']}) → lane-ready")
        result = runner(["issue", "view", str(n), "--repo", repo, "--json", "state,labels"])
        if result.returncode:
            output(f"  skip: cannot read issue #{n}")
            continue
        issue = json.loads(result.stdout)
        labels = {label["name"] if isinstance(label, dict) else label for label in issue["labels"]}
        if issue["state"].lower() != "open" or "lane-failed" not in labels:
            output(f"  skip: state/labels changed ({issue['state']}, {sorted(labels)})")
            continue
        if apply:
            edit = runner(["issue", "edit", str(n), "--repo", repo, "--remove-label", "lane-failed", "--add-label", "lane-ready"])
            if edit.returncode:
                output(f"  error: failed to relabel #{n}")
                continue
            comment = f"Requeued after backend {row['class']} error (run {row['run']}); the lane failure was caused by quota/rate limiting."
            runner(["issue", "comment", str(n), "--repo", repo, "--body", comment])
            changed.append(n)
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--since", help="include log events at or after HH:MM")
    parser.add_argument("--apply", action="store_true", help="apply label changes and comment")
    args = parser.parse_args(argv)
    rows = plan(Path(args.log).read_text(encoding="utf-8"), args.since)
    print(f"{'apply' if args.apply else 'dry-run'}: {len(rows)} candidate(s)")
    execute(args.repo, rows, args.apply)


if __name__ == "__main__":
    main()
