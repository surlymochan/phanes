#!/usr/bin/env python3
"""Classify tester evidence depth for a pull request."""
import argparse
import json
import subprocess
import sys


FILES_FULL_READ = 20
CHURN_FULL_READ = 1000
ROUND_FULL_READ = 4


def classify(pr, issue=None):
    reasons = []
    files = pr.get("files")
    additions, deletions = pr.get("additions"), pr.get("deletions")
    if files is None or additions is None or deletions is None:
        reasons.append("diff 数据缺失")
    elif files >= FILES_FULL_READ or additions + deletions >= CHURN_FULL_READ:
        reasons.append(f"大 diff: {files} 文件, +{additions}/-{deletions} 行")

    reviews = pr.get("reviews")
    labels = [x.get("name", "") for x in pr.get("labels", [])]
    if reviews is None:
        reasons.append("review 轮次数据缺失")
    elif len(reviews) >= ROUND_FULL_READ:
        reasons.append(f"review 轮次 {len(reviews)} >= {ROUND_FULL_READ}")
    if "needs-human" in labels:
        reasons.append("PR 有 needs-human 前科标签")

    if issue is None:
        reasons.append("关联 issue 失败类型数据缺失")
    else:
        issue_labels = [x.get("name", "") for x in issue.get("labels", [])]
        failures = [x for x in issue_labels if x.startswith("failure:") or x == "lane-failed"]
        if failures:
            reasons.append("关联 issue 失败记录: " + ", ".join(failures))

    full = any(x.startswith(("大 diff", "PR 有 needs-human", "关联 issue 失败记录")) or
               x.startswith("review 轮次 ") and x[len("review 轮次 "):].split(" ", 1)[0].isdigit()
               for x in reasons)
    return {"pr": pr.get("number"), "level": "全读" if full else "抽检",
            "sampling": "100%" if full else "20%", "reasons": reasons or ["未命中全读条件"]}


def live_data(number):
    raw = subprocess.check_output(["gh", "pr", "view", str(number), "--json",
                                   "number,files,additions,deletions,reviews,labels,closingIssuesReferences"], text=True)
    pr = json.loads(raw)
    issue_ref = (pr.get("closingIssuesReferences") or [None])[0]
    issue = None
    if issue_ref:
        issue = json.loads(subprocess.check_output(["gh", "issue", "view", str(issue_ref["number"]),
                                                    "--json", "number,labels"], text=True))
    return pr, issue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", type=int, help="PR number")
    parser.add_argument("--input", help="offline JSON object containing pr and optional issue")
    args = parser.parse_args()
    try:
        if args.input:
            data = json.load(open(args.input, encoding="utf-8"))
            pr, issue = data.get("pr", {}), data.get("issue")
        else:
            pr, issue = live_data(args.pr)
        result = classify(pr, issue)
        result["pr"] = args.pr
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"tester-risk: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
