"""Pure task-source helpers for dispatch (Refs #1399).

The functions consume decoded GitHub JSON values; fetching them is the caller's job.
"""
import re


_CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+#(\d+)\b", re.I)
_HOT_LINE = re.compile(r"^\s*(?:[-*+]\s+)?热文件\s*[:：]\s*(.*)$")


def _issue_number(issue):
    if isinstance(issue, dict):
        issue = issue.get("number")
    try:
        number = int(issue)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _task(issue, kind, pr=None, head_sha=None, hot_files=None):
    return {"issue": issue, "kind": kind, "pr": pr, "head_sha": head_sha,
            "hot_files": list(hot_files or [])}


def _inflight_keys(inflight):
    """Accept existing task dicts or explicit (kind, pr, sha) tuples."""
    keys = set()
    for item in inflight or ():
        if isinstance(item, dict):
            keys.add((item.get("kind"), item.get("pr"), item.get("head_sha")))
        else:
            try:
                keys.add(tuple(item))
            except TypeError:
                continue
    return keys


def _pr_tasks(prs, kind, inflight, eligible, issue_for_pr=None):
    existing = _inflight_keys(inflight)
    tasks, seen = [], set(existing)
    for pr in prs or ():
        if not isinstance(pr, dict) or not eligible(pr):
            continue
        match = _CLOSES_RE.search(pr.get("body") or "")
        issue = (_issue_number(issue_for_pr(pr)) if issue_for_pr else
                 (_issue_number(match.group(1)) if match else None))
        number = _issue_number(pr.get("number"))
        sha = (pr.get("headRefOid") or pr.get("head_sha") or pr.get("headSha")
               or pr.get("headRefName"))
        if issue is None or number is None or not isinstance(sha, str) or not sha:
            continue
        key = (kind, number, sha)
        if key in seen:
            continue
        seen.add(key)
        tasks.append(_task(issue, kind, number, sha))
    return tasks


def labels_source(issues):
    """Create label-dispatch tasks from issue JSON objects."""
    tasks, seen = [], set()
    for issue in issues or ():
        if not isinstance(issue, dict):
            continue
        number = _issue_number(issue.get("number"))
        if number is None or number in seen:
            continue
        seen.add(number)
        paths = []
        for line in str(issue.get("body") or "").splitlines():
            match = _HOT_LINE.match(line)
            if match:
                for path in re.split(r"[\s,，、;；]+", match.group(1)):
                    path = path.strip(" `\"'").rstrip("。．")
                    if path and path not in paths:
                        paths.append(path)
        tasks.append(_task(number, "labels", hot_files=paths))
    return tasks


def _failed_ci(pr):
    rollup = pr.get("statusCheckRollup") or []
    for check in rollup:
        if not isinstance(check, dict):
            continue
        state = str(check.get("conclusion") or check.get("state") or "").upper()
        if state in ("FAILURE", "FAILED", "ERROR", "TIMED_OUT", "ACTION_REQUIRED"):
            return True
    return False


def ci_fix_source(prs, inflight=None, eligible=None, issue_for_pr=None):
    """Create one CI-fix task for each failed PR head that is not already in flight."""
    return _pr_tasks(prs, "ci_fix", inflight, eligible or _failed_ci, issue_for_pr)


def rebase_source(prs, inflight=None, issue_for_pr=None):
    """Create one rebase task for each conflicting PR head that is not already in flight."""
    return _pr_tasks(prs, "rebase", inflight,
                     lambda pr: str(pr.get("mergeable") or "").upper() == "CONFLICTING", issue_for_pr)
