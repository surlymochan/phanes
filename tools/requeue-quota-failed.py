#!/usr/bin/env python3
"""Requeue issues incorrectly failed after backend quota/rate-limit errors (Refs #1541).

A card that already has an open pull request is delivered: it is not put back into
`lane-ready` (Refs #1772). The delivery guard of the autodispatcher answers that
question, so both requeue paths behave the same way.
"""
import argparse
import importlib.util
import json
import re
import subprocess
from pathlib import Path


AD_SCRIPT = Path(__file__).resolve().parent / "lane-autodispatch" / "autodispatch.py"
_spec = importlib.util.spec_from_file_location("lane_autodispatch", AD_SCRIPT)
autodispatch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autodispatch)

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


def gh_runner(args, payload=None):
    return subprocess.run(["gh", *args], input=payload, text=True, capture_output=True, check=False)


def guard_runner(runner):
    """Adapt a `gh` runner to the guard contract: (rc, stdout, stderr)."""
    def call(args, payload=None):
        result = runner(args, payload)
        return result.returncode, result.stdout, result.stderr
    return call


def execute(repo, rows, apply=False, runner=gh_runner, output=print):
    changed = []
    call = guard_runner(runner)
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
        # Refs #1772: a card with an open pull request is delivered and must not be
        # requeued. A failed lookup blocks the requeue: an unknown state is no state.
        try:
            delivered = autodispatch.open_pr_issues(call, repo)
        except Exception as e:
            output(f"  skip: the open PR lookup failed ({e}); the card is not requeued")
            continue
        prs = delivered.get(n)
        if autodispatch.has_open_pr(n, delivered=delivered):
            output(f"  skip: open PR {autodispatch.pr_list(prs)} already delivers it; lane-pr kept")
            if apply and not autodispatch.ensure_lane_pr(n, runner=call, repo=repo, note=lambda m: output(f"  {m}")):
                output(f"  error: the lane-pr terminal state of #{n} could not be verified")
            continue
        if apply:
            edit = runner(["issue", "edit", str(n), "--repo", repo, "--remove-label", "lane-failed", "--add-label", "lane-ready"])
            if edit.returncode:
                output(f"  error: failed to relabel #{n}")
                continue
            # the label is written: read the delivery state again, a pull request may
            # have been opened meanwhile and the older decision must not win.
            try:
                prs = autodispatch.open_pr_issues(call, repo).get(n)
            except Exception:
                prs = None
            if prs:
                output(f"  repair: open PR {autodispatch.pr_list(prs)} appeared; the card goes back to lane-pr")
                if not autodispatch.ensure_lane_pr(n, runner=call, repo=repo, note=lambda m: output(f"  {m}")):
                    output(f"  error: the lane-pr terminal state of #{n} could not be verified")
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
