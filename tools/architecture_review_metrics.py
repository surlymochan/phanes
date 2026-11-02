#!/usr/bin/env python3
"""Read-only GitHub review metrics, with complete raw snapshots for offline replay."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import gzip
import json
import math
from pathlib import Path
import re
import statistics
import subprocess


LABELS = ("lane-ready", "lane-failed", "lane-pr", "needs-human", "follow-up")
PAGE = "pageInfo{hasNextPage endCursor}"
COMMENT = "id url createdAt body"
REVIEW = "id url submittedAt state body"
PR_QUERY = """query($owner:String!,$name:String!,$after:String){repository(owner:$owner,name:$name){
pullRequests(first:50,after:$after,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{
number url createdAt updatedAt mergedAt headRefName
comments(first:100){nodes{%s} %s} reviews(first:100){nodes{%s} %s}} %s}}}""" % (
    COMMENT, PAGE, REVIEW, PAGE, PAGE)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO string with a timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp needs an explicit timezone")
    return result.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def graphql(query, variables):
    result = subprocess.run(["gh", "api", "graphql", "--input", "-"],
                            input=json.dumps({"query": query, "variables": variables}),
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError(f"GitHub collection failed: {result.stderr.strip()[:1000]}")
    data = json.loads(result.stdout)
    if data.get("errors"):
        raise ValueError(f"GitHub returned GraphQL errors: {json.dumps(data['errors'])[:1000]}")
    repo = data.get("data", {}).get("repository")
    if not isinstance(repo, dict):
        raise ValueError("GitHub repository data is missing; metrics are not complete")
    return repo


def rest_issues(repo, end):
    """REST issue pages include full labels without the GraphQL nested-label cost."""
    result, numbers, page = [], set(), 1
    while True:
        endpoint = (f"repos/{repo}/issues?state=all&per_page=100&sort=created&direction=asc&page={page}")
        response = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=120)
        if response.returncode:
            raise ValueError(f"GitHub issue collection failed: {response.stderr.strip()[:1000]}")
        rows = json.loads(response.stdout)
        if not isinstance(rows, list):
            raise ValueError("GitHub issue inventory did not return an array")
        for issue in rows:
            if "pull_request" in issue or timestamp(issue["created_at"]) >= end:
                continue
            if issue["number"] in numbers:
                raise ValueError("GitHub issue pagination repeated an issue")
            numbers.add(issue["number"])
            result.append({"number": issue["number"], "url": issue["html_url"],
                           "created_at": issue["created_at"], "closed_at": issue["closed_at"],
                           "state": issue["state"], "labels": sorted({label["name"] for label in issue["labels"]})})
        if len(rows) < 100:
            return result
        page += 1


def next_cursor(connection, previous=None):
    info = connection["pageInfo"]
    if not info["hasNextPage"]:
        return None
    cursor = info.get("endCursor")
    if not cursor or cursor == previous:
        raise ValueError("GitHub pagination did not advance")
    return cursor


def nested_nodes(owner, name, number, parent, field, initial, fields):
    nodes = list(initial["nodes"])
    cursor = next_cursor(initial)
    seen = set()
    while cursor:
        if cursor in seen:
            raise ValueError("GitHub nested pagination repeated a cursor")
        seen.add(cursor)
        query = ("query($owner:String!,$name:String!,$number:Int!,$after:String){"
                 "repository(owner:$owner,name:$name){" + parent + "(number:$number){" + field +
                 "(first:100,after:$after){nodes{" + fields + "} " + PAGE + "}}}}")
        connection = graphql(query, {"owner": owner, "name": name, "number": number,
                                      "after": cursor})[parent][field]
        nodes.extend(connection["nodes"])
        cursor = next_cursor(connection, cursor)
    if any(node is None for node in nodes):
        raise ValueError("GitHub returned an unavailable nested item")
    return nodes


def collect(repo, start, end):
    owner, name = repo.split("/")
    prs, issues = [], []
    cursor, seen = None, set()
    while True:
        connection = graphql(PR_QUERY, {"owner": owner, "name": name, "after": cursor})["pullRequests"]
        stop = False
        for pr in connection["nodes"]:
            if timestamp(pr["updatedAt"]) < start:
                stop = True
                break
            if timestamp(pr["createdAt"]) >= end:
                continue
            comments = nested_nodes(owner, name, pr["number"], "pullRequest", "comments", pr["comments"], COMMENT)
            reviews = nested_nodes(owner, name, pr["number"], "pullRequest", "reviews", pr["reviews"], REVIEW)
            prs.append({"number": pr["number"], "url": pr["url"], "created_at": pr["createdAt"],
                        "updated_at": pr["updatedAt"], "merged_at": pr["mergedAt"], "head_ref": pr["headRefName"],
                        "comments": [{"id": item["id"], "url": item["url"], "created_at": item["createdAt"],
                                      "body": item["body"]} for item in comments],
                        "reviews": [{"id": item["id"], "url": item["url"], "submitted_at": item["submittedAt"],
                                     "state": item["state"], "body": item["body"]} for item in reviews]})
        new = None if stop else next_cursor(connection, cursor)
        if not new:
            break
        if new in seen:
            raise ValueError("GitHub PR pagination repeated a cursor")
        seen.add(new)
        cursor = new
    issues = rest_issues(repo, end)
    return {"repository": repo, "window": {"start": iso(start), "end": iso(end)},
            "captured_at": iso(datetime.now(timezone.utc)), "pull_requests": sorted(prs, key=lambda p: p["number"]),
            "issues": sorted(issues, key=lambda i: i["number"]),
            "completeness": {key: True for key in ("complete", "pull_requests", "issues", "comments", "reviews", "labels")}}


def validate(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be an object")
    complete = snapshot["completeness"]
    if not isinstance(complete, dict) or complete.get("complete") is not True or any(v is not True for v in complete.values()):
        raise ValueError("snapshot completeness is missing or incomplete")
    if not isinstance(snapshot["repository"], str) or not re.fullmatch(r"[^/\s]+/[^/\s]+", snapshot["repository"]):
        raise ValueError("invalid repository")
    start, end = (timestamp(snapshot["window"][key]) for key in ("start", "end"))
    if start >= end:
        raise ValueError("window start must precede end")
    timestamp(snapshot["captured_at"])
    for collection in ("pull_requests", "issues"):
        if not isinstance(snapshot[collection], list):
            raise ValueError(f"{collection} must be an array")
        numbers = set()
        for item in snapshot[collection]:
            if not isinstance(item, dict) or type(item["number"]) is not int or item["number"] <= 0:
                raise ValueError("items need a positive integer number")
            if item["number"] in numbers:
                raise ValueError("duplicate item number in snapshot")
            numbers.add(item["number"])
            if not isinstance(item["url"], str) or not item["url"]:
                raise ValueError("item evidence URL is missing")
            created = timestamp(item["created_at"])
            if collection == "issues":
                if item["state"].upper() not in ("OPEN", "CLOSED"):
                    raise ValueError("unknown issue state")
                if item["closed_at"] is not None:
                    timestamp(item["closed_at"])
                if not isinstance(item["labels"], list) or any(not isinstance(label, str) for label in item["labels"]):
                    raise ValueError("issue labels must be strings")
                continue
            timestamp(item["updated_at"])
            if not isinstance(item["head_ref"], str):
                raise ValueError("PR head_ref must be text")
            if item["merged_at"] is not None:
                timestamp(item["merged_at"])
            for key, date in (("comments", "created_at"), ("reviews", "submitted_at")):
                if not isinstance(item[key], list):
                    raise ValueError(f"{key} must be an array")
                for event in item[key]:
                    if not isinstance(event["id"], (str, int)) or not event["url"] or not isinstance(event["body"], str):
                        raise ValueError("review/comment needs an id, URL and text body")
                    if key == "reviews" and not isinstance(event["state"], str):
                        raise ValueError("review state must be text")
                    if event[date] is None and key == "reviews" and event["state"].upper() == "PENDING":
                        continue
                    timestamp(event[date])
    return start, end


def blocker_count(body):
    lines = re.findall(r"^\s*BLOCKERS\s*:[^\r\n]*$", body, re.M | re.I)
    if len(lines) != 1:
        return None
    match = re.fullmatch(r"\s*BLOCKERS\s*:\s*([0-9]+)\s*", lines[0], re.I)
    return int(match.group(1)) if match else None


def lane_report(comment):
    body = comment["body"]
    marker = re.search(r"<!--\s*lane-review\b(.*?)-->", body, re.I | re.S)
    header = re.search(r"^\s*(?:\*\*)?Lane[ -]review\b[^\r\n]*", body, re.M | re.I)
    verdicts = re.findall(r"^\s*VERDICT\s*:\s*(APPROVE|CHANGES)\s*$", body, re.M | re.I)
    if not (marker or header) or len(verdicts) != 1:
        return None
    identity = (marker.group(1) if marker else "") + " " + (header.group(0) if header else "")
    head = re.search(r"\b(?:sha|head)\s*(?:=|:)??\s*[`\"']?([0-9a-f]{7,40})\b", identity, re.I)
    run = re.search(r"\brun\s*(?:=|:)??\s*[`\"']?([A-Za-z0-9_.:/-]+)", identity, re.I)
    return {"verdict": verdicts[0].upper(), "head_sha": head.group(1).lower() if head else None,
            "run": run.group(1) if run else None, "blockers": blocker_count(body)}


def report(snapshot):
    start, end = validate(snapshot)
    in_window = lambda value: value is not None and start <= timestamp(value) < end
    events, reposts = [], []
    raw_total = 0
    candidates = {}
    for pr in sorted(snapshot["pull_requests"], key=lambda item: item["number"]):
        if timestamp(pr["created_at"]) >= end:
            continue
        grouped = {}
        for comment in pr["comments"]:
            parsed = lane_report(comment)
            if not parsed or timestamp(comment["created_at"]) >= end:
                continue
            raw_total += int(in_window(comment["created_at"]))
            key = ((parsed["head_sha"], parsed["run"], parsed["verdict"])
                   if parsed["head_sha"] and parsed["run"] else ("comment", str(comment["id"])))
            grouped.setdefault(key, []).append((comment, parsed))
        for copies in grouped.values():
            copies.sort(key=lambda pair: (timestamp(pair[0]["created_at"]), str(pair[0]["id"])))
            comment, parsed = copies[0]
            duplicates = [copy["url"] for copy, _ in copies[1:]]
            if in_window(comment["created_at"]):
                events.append({"pr": pr["number"], "pr_url": pr["url"], "source": "lane-comment",
                               "at": comment["created_at"], **parsed, "evidence_urls": [comment["url"]],
                               "duplicate_urls": duplicates})
            elif any(in_window(copy["created_at"]) for copy, _ in copies[1:]):
                reposts.append({"pr": pr["number"], "original_at": comment["created_at"],
                                "evidence_url": comment["url"], "duplicate_urls": [copy["url"] for copy, _ in copies[1:]
                                                                                       if in_window(copy["created_at"])]})
        review_ids = set()
        for review in pr["reviews"]:
            verdict = {"APPROVED": "APPROVE", "CHANGES_REQUESTED": "CHANGES"}.get(review["state"].upper())
            if not verdict or not in_window(review["submitted_at"]):
                continue
            if review["id"] in review_ids:
                raise ValueError("duplicate formal review id")
            review_ids.add(review["id"])
            raw_total += 1
            events.append({"pr": pr["number"], "pr_url": pr["url"], "source": "formal-review",
                           "at": review["submitted_at"], "verdict": verdict, "head_sha": None, "run": None,
                           "blockers": blocker_count(review["body"]), "evidence_urls": [review["url"]],
                           "duplicate_urls": []})
        candidates[pr["number"]] = pr
    events.sort(key=lambda item: (item["pr"], timestamp(item["at"]), item["source"], item["evidence_urls"][0]))
    blockers = {key: 0 for key in ("0", "1", "2", "3+", "unknown")}
    verdicts = {"APPROVE": 0, "CHANGES": 0}
    rounds = Counter()
    for event in events:
        number = event["blockers"]
        bucket = "unknown" if number is None else "3+" if number >= 3 else str(number)
        event["blocker_bucket"] = bucket
        blockers[bucket] += 1
        verdicts[event["verdict"]] += 1
        rounds[event["pr"]] += 1
    round_rows, merge_rows = [], []
    buckets = {key: 0 for key in ("<1h", "1h-6h", "6h-24h", "1d-3d", ">=3d", "unknown")}
    for number, pr in sorted(candidates.items()):
        if rounds[number] or in_window(pr["created_at"]) or in_window(pr["merged_at"]):
            round_rows.append({"number": number, "url": pr["url"], "rounds": rounds[number],
                               "evidence_urls": [url for event in events if event["pr"] == number
                                                 for url in event["evidence_urls"]]})
        if in_window(pr["merged_at"]):
            wait = (timestamp(pr["merged_at"]) - timestamp(pr["created_at"])).total_seconds()
            wait_status = "invalid_order" if wait < 0 else "known"
            if wait < 0:
                wait, bucket = None, "unknown"
            else:
                bucket = next((name for limit, name in ((3600, "<1h"), (21600, "1h-6h"), (86400, "6h-24h"),
                                                       (259200, "1d-3d")) if wait < limit), ">=3d")
            buckets[bucket] += 1
            merge_rows.append({"number": number, "url": pr["url"], "created_at": pr["created_at"],
                               "merged_at": pr["merged_at"], "wait_seconds": wait, "wait_status": wait_status})
    waits = sorted(row["wait_seconds"] for row in merge_rows if row["wait_seconds"] is not None)
    unknown_wait_rows = [row for row in merge_rows if row["wait_seconds"] is None]
    histogram = Counter(str(row["rounds"]) for row in round_rows)
    histogram.setdefault("0", 0)
    label_rows = {label: [] for label in LABELS}
    failed = []
    for issue in sorted(snapshot["issues"], key=lambda item: item["number"]):
        if issue["state"].upper() != "OPEN" or timestamp(issue["created_at"]) >= end:
            continue
        labels = set(issue["labels"])
        for label in LABELS:
            if label in labels:
                label_rows[label].append({"number": issue["number"], "url": issue["url"]})
        if "lane-failed" in labels:
            failed.append({"number": issue["number"], "url": issue["url"], "labels": sorted(labels)})
    failed_labels = Counter(label for issue in failed for label in issue["labels"])
    return {
        "repository": snapshot["repository"], "window": {"start": iso(start), "end": iso(end)},
        "captured_at": snapshot["captured_at"], "completeness": snapshot["completeness"],
        "definitions": {
            "window": "Half-open [start,end); event timestamps, not current checks, determine inclusion.",
            "review_rounds": "Published logical verdict events, not internal review attempts. Denominator: PRs reviewed, created or merged in the window; zero reviews included.",
            "deduplication": "Lane reports use PR/head/run/verdict, earliest publication; otherwise comment id. Formal reviews use their ids; independent runs are distinct.",
            "merge_wait": "PR creation to merge, in seconds; p90 uses nearest rank ceil(0.9*n) over known waits. A merge before creation is retained as an unknown duration, never clamped or reordered.",
            "issue_stock": "Open state and labels at captured_at for issues created before end; not reconstructed historical end state.",
            "failed_labels": "Raw labels on open lane-failed issues; no inferred failure cause.",
        },
        "reviews": {"total": len(events), "raw_total": raw_total, "deduplicated_total": len(events),
                    "duplicate_events": raw_total - len(events), "verdicts": verdicts, "blockers": blockers,
                    "events": events, "prior_window_reposts": reposts},
        "review_rounds": {"pull_requests": len(round_rows), "histogram": dict(sorted(histogram.items(), key=lambda p: int(p[0]))),
                          "rows": round_rows},
        "merges": {"count": len(merge_rows), "known_wait_count": len(waits),
                   "unknown_wait_count": len(unknown_wait_rows), "unknown_wait_rows": unknown_wait_rows,
                   "wait_seconds": {"min": min(waits) if waits else None,
                   "median": statistics.median(waits) if waits else None,
                   "p90": waits[math.ceil(len(waits) * .9) - 1] if waits else None, "max": max(waits) if waits else None},
                   "buckets": buckets, "rows": merge_rows},
        "issue_labels": {"counts": {key: len(value) for key, value in label_rows.items()},
                         "issues_by_label": label_rows, "failed_issues": failed,
                         "failed_label_distribution": dict(sorted(failed_labels.items()))},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="surlymochan/phanes")
    parser.add_argument("--end", help="UTC ISO endpoint; defaults to current UTC time")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--snapshot-in", type=Path, help="offline JSON or .json.gz snapshot; uses its recorded window")
    parser.add_argument("--snapshot-out", type=Path, help="write complete JSON or .json.gz live input for replay")
    args = parser.parse_args(argv)
    try:
        if args.snapshot_in:
            if args.snapshot_out:
                raise ValueError("snapshot-out is for live collection; use snapshot-in alone for replay")
            payload = (gzip.decompress(args.snapshot_in.read_bytes()).decode("utf-8")
                       if args.snapshot_in.suffix == ".gz" else args.snapshot_in.read_text(encoding="utf-8"))
            snapshot = json.loads(payload)
        else:
            if args.days <= 0 or not re.fullmatch(r"[^/\s]+/[^/\s]+", args.repo):
                raise ValueError("days must be positive and repo must be owner/name")
            end = timestamp(args.end) if args.end else datetime.now(timezone.utc)
            snapshot = collect(args.repo, end - timedelta(days=args.days), end)
        result = report(snapshot)
        if args.snapshot_out:
            payload = json.dumps(snapshot, sort_keys=True, indent=2) + "\n"
            if args.snapshot_out.suffix == ".gz":
                args.snapshot_out.write_bytes(gzip.compress(payload.encode("utf-8"), mtime=0))
            else:
                args.snapshot_out.write_text(payload, encoding="utf-8")
        print(json.dumps(result, sort_keys=True, indent=2))
    except (ValueError, KeyError, TypeError, AttributeError, OSError, EOFError, subprocess.SubprocessError) as error:
        parser.exit(2, f"architecture metrics: incomplete or invalid input: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
