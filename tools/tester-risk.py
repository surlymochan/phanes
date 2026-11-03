#!/usr/bin/env python3
"""Classify tester evidence depth for a pull request."""
import argparse
import json
import subprocess
import sys


FILES_FULL_READ = 20
CHURN_FULL_READ = 1000
ROUND_FULL_READ = 4
# lane 评审以 PR 评论落盘（<!-- lane-review pr=... sha=... verdict=... -->），不是 gh review。
LANE_REVIEW_MARKER = "<!-- lane-review"
# 本仓库真实的失败标签只有 lane-failed；failure:* 前缀在本仓库不存在（见 gh label list），
# 保留前缀匹配仅为兼容其他 fork / 未来约定。
FAILURE_LABELS = ("lane-failed",)
FAILURE_LABEL_PREFIX = "failure:"


def file_count(files):
    """`gh pr view --json files` 返回对象数组，文件数取数组长度（不是拿它跟整数比）。"""
    if isinstance(files, list):
        return len(files)
    if isinstance(files, int) and not isinstance(files, bool):
        return files
    return None


def label_names(labels):
    """gh 的 labels 形如 [{"name": ...}]；容忍字符串项与缺失/None。"""
    if not isinstance(labels, list):
        return []
    names = []
    for item in labels:
        if isinstance(item, dict):
            name = item.get("name")
        elif isinstance(item, str):
            name = item
        else:
            name = None
        if name:
            names.append(name)
    return names


def lane_review_rounds(pr):
    """lane 评审轮次 = 带 lane-review 标记的 PR 评论数；评论数据不可用时返回 (None, None)。"""
    comments = pr.get("comments")
    if isinstance(comments, list):
        rounds = sum(1 for comment in comments
                     if isinstance(comment, dict) and LANE_REVIEW_MARKER in (comment.get("body") or ""))
        return rounds, "lane-review 评论"
    return None, None


def classify(pr, issue=None):
    reasons = []
    full = False
    files = file_count(pr.get("files"))
    additions, deletions = pr.get("additions"), pr.get("deletions")
    if files is None or not isinstance(additions, int) or not isinstance(deletions, int):
        reasons.append("diff 数据缺失")
    elif files >= FILES_FULL_READ or additions + deletions >= CHURN_FULL_READ:
        reasons.append(f"大 diff: {files} 文件, +{additions}/-{deletions} 行")
        full = True

    rounds, source = lane_review_rounds(pr)
    if rounds is None:
        reviews = pr.get("reviews")
        fallback = len(reviews) if isinstance(reviews, list) else None
        if fallback is None:
            reasons.append("review 轮次数据缺失（lane-review 评论与 gh reviews 均不可用）")
        else:
            reasons.append(f"review 轮次数据缺失（lane-review 评论不可用，gh reviews 兜底 {fallback}）")
        if fallback is not None and fallback >= ROUND_FULL_READ:
            reasons.append(f"review 轮次 {fallback} >= {ROUND_FULL_READ}（gh reviews 兜底）")
            full = True
    elif rounds >= ROUND_FULL_READ:
        reasons.append(f"review 轮次 {rounds} >= {ROUND_FULL_READ}（{source}）")
        full = True

    if "needs-human" in label_names(pr.get("labels")):
        reasons.append("PR 有 needs-human 前科标签")
        full = True

    if issue is None:
        reasons.append("关联 issue 失败类型数据缺失")
    else:
        issue_labels = label_names(issue.get("labels"))
        failures = [x for x in issue_labels
                    if x in FAILURE_LABELS or x.startswith(FAILURE_LABEL_PREFIX)]
        if failures:
            reasons.append("关联 issue 失败记录: " + ", ".join(failures))
            full = True

    return {"pr": pr.get("number"), "level": "全读" if full else "抽检",
            "sampling": "100%" if full else "20%", "reasons": reasons or ["未命中全读条件"]}


def live_data(number):
    raw = subprocess.check_output(["gh", "pr", "view", str(number), "--json",
                                   "number,files,additions,deletions,reviews,comments,labels,"
                                   "closingIssuesReferences"], text=True)
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
            with open(args.input, encoding="utf-8") as handle:
                data = json.load(handle)
            pr, issue = data.get("pr", {}), data.get("issue")
        else:
            pr, issue = live_data(args.pr)
        result = classify(pr, issue)
        result["pr"] = pr.get("number") or args.pr
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"tester-risk: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
