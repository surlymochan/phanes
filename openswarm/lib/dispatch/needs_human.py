"""The needs-human backlog: what waits for a person, and what to do about it (Refs #1695).

A PR whose review rounds are exhausted, or an issue the lanes could not finish, carries the
``needs-human`` label and waits for a human.  Nothing told the human about it: the backlog
stayed invisible until somebody opened the Project board and looked at every single card.

This module turns the labelled issues and PRs into one human-facing summary:

* every entry names the number, the title, the review round, the last verdict summary and
  how long the item has been waiting; the entries are sorted by the waiting time, the
  longest wait first;
* every entry carries a *recommended action* - what the human is expected to do next:
  ``等用户裁决`` (a decision is needed), ``等维护者信息`` (information is missing) or
  ``可拆小重试`` (the work should be split and retried).  When the verdict text does not
  say, the entry is marked ``需人工判断`` instead of guessing.

The module only reports: it never changes a label, a round counter or a dispatch decision.
``collect`` takes a ``gh`` runner, so the whole summary can be replayed from a fixture.

Standard library only, Python 3.9+.
"""
import datetime
import json
import itertools
import re
import time
import urllib.parse

# The label that parks a card for a human.
LABEL = "needs-human"
# The two kinds of cards that can carry it.
KIND_ISSUE = "issue"
KIND_PR = "pr"

# The recommended actions, one per kind of human answer that is expected.
ACTION_USER = "等用户裁决"          # a decision (scope, design, priority) is needed
ACTION_MAINTAINER = "等维护者信息"  # the reviewer waits for information that only a maintainer has
ACTION_SPLIT = "可拆小重试"          # the change is too big: split it and retry
ACTION_MANUAL = "需人工判断"        # the verdict text does not say: a human has to look

# A backlog entry is repeated in the log when it is new, and again once it waited this long.
DEFAULT_INTERVAL_H = 24

# The backlog is read page by page: the REST list endpoint answers with the issues *and*
# the pull requests of the repository, so one paginated read covers both kinds.
PAGE_SIZE = 100
# No default cap: all pages of the backlog must be read.
MAX_PAGES = None

# The state file (in the dispatcher state dir) remembers what was already reported.
STATE_FILE = "needs-human-report.json"

# The verdict markers of a lane review (the same shape as lib/review/status.py).
VERDICT_LINE_RE = re.compile(r"^VERDICT:[ \t]*(APPROVE|CHANGES)(?![A-Za-z0-9_])", re.M)
SUMMARY_LINE_RE = re.compile(r"^SUMMARY:[ \t]*(.*)$", re.M)
# The marker the dispatcher leaves in its review comment.
REVIEW_MARKER = "<!-- lane-review "

# The verdict wording that says what the human has to do.  The first group that matches
# wins, so the order is the priority: missing information first (nothing else can move),
# then a decision, then a split.
INFO_RE = re.compile(r"缺少|缺失|请提供|需要提供|补充|信息不足|无法确认|未说明|没有说明|澄清|复现|"
                     r"missing|lack of|repro", re.I)
DECISION_RE = re.compile(r"裁决|决策|选型|取舍|需要确认|待确认|请确认|是否应该|需求不明确|由你决定|偏好|"
                         r"decision|confirm whether|which approach", re.I)
SPLIT_RE = re.compile(r"太大|过大|范围过大|超出范围|拆分|拆小|拆成|分批|"
                      r"split|too large|out of scope", re.I)


def _flat(value):
    """One line of text: a title or a verdict summary never breaks the log line."""
    return " ".join(str(value or "").split())


def parse_time(value):
    """The epoch seconds of a timestamp: an epoch number, an ISO 8601 string or ``%F %T``.

    A timestamp without a timezone is read as local time (the dispatcher writes its review
    stamps in the local time of the box); a timestamp with one keeps it.  An unreadable
    value yields ``None`` instead of raising: a broken stamp must not break the summary.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = None
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    return parsed.timestamp()


def parse_verdict(text):
    """The ``(verdict, summary)`` pair of a review conclusion, or ``(None, None)``.

    Mirrors ``lib/review/status.py``: the verdict is the value of the first ``VERDICT:``
    line, the summary the rest of the first ``SUMMARY:`` line.
    """
    body = str(text or "")
    verdict = VERDICT_LINE_RE.search(body)
    summary = SUMMARY_LINE_RE.search(body)
    return (verdict.group(1) if verdict else None,
            summary.group(1).strip() if summary else None)


def classify_action(verdict_text, rounds=0):
    """The recommended action of one backlog entry, with the reason behind it.

    The verdict text decides: missing information (``等维护者信息``), a decision that only
    the user can take (``等用户裁决``) or a change that should be split (``可拆小重试``).
    The round count decides when the text does not say: two or more CHANGES rounds that
    never converged point at a work item that is too big, a single round without any text
    is a case a human has to judge.
    """
    text = str(verdict_text or "")
    try:
        count = int(rounds or 0)
    except (TypeError, ValueError):
        count = 0
    for action, pattern in ((ACTION_MAINTAINER, INFO_RE), (ACTION_USER, DECISION_RE), (ACTION_SPLIT, SPLIT_RE)):
        match = pattern.search(text)
        if match:
            return action, "verdict 命中「%s」" % match.group(0)
    if not text.strip():
        if count >= 2:
            return ACTION_SPLIT, "连续 %d 轮 CHANGES 且无 verdict 文本：建议拆小重试" % count
        if count == 1:
            return ACTION_USER, "首轮 CHANGES 且无 verdict 文本：需人工裁决"
        return ACTION_MANUAL, "无 verdict 文本"
    if count >= 2:
        return ACTION_SPLIT, "连续 %d 轮 CHANGES 未收敛且 verdict 未指明方向：建议拆小重试" % count
    return ACTION_MANUAL, "verdict 未指明可执行方向"


def last_review_verdict(runner, number):
    """The verdict of the last lane review of one PR, read from its review comments.

    ``runner(argv)`` runs one gh command and returns ``(rc, stdout, stderr)``.  A PR whose
    review record holds no summary (an escalation from an earlier release) is completed
    from its last lane-review comment; a PR without such a comment yields no verdict.
    """
    rc, out, _ = runner(["pr", "view", str(int(number)), "--json", "comments"])
    if rc:
        return None, None, ""
    try:
        data = json.loads(out or "{}")
    except ValueError:
        return None, None, ""
    body = ""
    for comment in (data or {}).get("comments") or []:
        text = str((comment or {}).get("body") or "")
        if REVIEW_MARKER in text:
            body = text
    if not body:
        return None, None, ""
    verdict, summary = parse_verdict(body)
    return verdict, summary, body


def _item(kind, row, reviews):
    """One raw backlog item out of a gh row and the review record of its number."""
    number = int(row.get("number") or 0)
    record = (reviews or {}).get(str(number)) or {}
    item = {
        "kind": kind,
        "number": number,
        "title": str(row.get("title") or ""),
        "created_at": parse_time(row.get("createdAt") or row.get("created_at")),
        "updated_at": parse_time(row.get("updatedAt") or row.get("updated_at")),
        "rounds": int(record.get("rounds") or 0),
        "verdict": record.get("verdict") or "",
        "summary": str(record.get("summary") or ""),
        "text": str(record.get("text") or ""),
        "since": parse_time(record.get("at")),
    }
    if item["since"] is None:
        item["since"] = item["updated_at"] or item["created_at"]
    return item


def _rows(out):
    """The JSON rows of one gh answer; a broken answer yields no rows."""
    try:
        data = json.loads(out or "[]")
    except ValueError:
        return []
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def labelled_pages(runner, repo, label=LABEL, page_size=PAGE_SIZE, max_pages=MAX_PAGES):
    """Every open issue and PR that carries the label, read page by page.

    The REST list endpoint of the repository answers with the issues *and* the pull
    requests, so one paginated read covers both kinds of card: a backlog that is longer
    than one page is read completely instead of being cut off at a fixed limit.  The
    pages are read one after the other until a page comes back empty.
    """
    endpoint = "repos/%s/issues?state=open&labels=%s" % (str(repo or "").strip("/"),
                                                          urllib.parse.quote(str(label), safe=""))
    rows, size = [], int(page_size)
    for page in (itertools.count(1) if max_pages is None else range(1, int(max_pages) + 1)):
        rc, out, _ = runner(["api", "%s&per_page=%d&page=%d" % (endpoint, size, page)])
        if rc:
            break
        batch = _rows(out)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < size:
            break
    return rows


def collect(runner, repo, label=LABEL, reviews=None, now=None, read_comments=True,
            page_size=PAGE_SIZE, max_pages=MAX_PAGES):
    """Every open issue and PR that carries the label, as raw items.

    ``runner(argv)`` runs one gh command and returns ``(rc, stdout, stderr)``.  The
    backlog is read page by page (``labelled_pages``), so every labelled card is listed
    and not only the first page.  A command that fails contributes no items instead of
    raising: an unreadable backlog must not break the health report.  ``reviews`` maps a
    PR number (as a string) to its review record (rounds, verdict, summary); a record
    without a verdict text is completed from the last lane-review comment of the PR.
    """
    reviews = reviews or {}
    items = []
    for row in labelled_pages(runner, repo, label, page_size=page_size, max_pages=max_pages):
        kind = KIND_PR if row.get("pull_request") else KIND_ISSUE
        item = _item(kind, row, reviews)
        if read_comments and kind == KIND_PR and not item["text"]:
            verdict, summary, body = last_review_verdict(runner, item["number"])
            item["verdict"] = item["verdict"] or (verdict or "")
            item["summary"] = item["summary"] or (summary or "")
            item["text"] = body
        items.append(item)
    return items


def summarize(items, now=None):
    """One entry per backlog item, sorted by the waiting time (the longest wait first).

    Every entry names the number, the kind, the title, the review round, the last verdict
    summary, how long the item has been waiting and the recommended action.
    """
    moment = time.time() if now is None else now
    entries = []
    for item in items or []:
        since = item.get("since")
        waiting = max(0.0, (moment - since) / 3600.0) if since else 0.0
        rounds = int(item.get("rounds") or 0)
        text = item.get("text") or item.get("summary") or ""
        action, reason = classify_action(text, rounds)
        entries.append({
            "kind": item.get("kind") or KIND_ISSUE,
            "number": int(item.get("number") or 0),
            "title": str(item.get("title") or ""),
            "rounds": rounds,
            "verdict": str(item.get("verdict") or ""),
            "summary": str(item.get("summary") or ""),
            "waiting_hours": round(waiting, 1),
            "action": action,
            "reason": reason,
        })
    entries.sort(key=lambda entry: (-entry["waiting_hours"], entry["number"]))
    return entries


def render(entries):
    """The summary as markdown: one line per backlog item, longest wait first."""
    items = entries or []
    if not items:
        return "needs-human：无积压"
    lines = ["needs-human 积压 %d 项（按等待时长排序）：" % len(items)]
    for entry in items:
        lines.append("- %s #%d %s · 轮次 %d · 等待 %.1fh · %s · 建议：%s（%s）" % (
            "PR" if entry.get("kind") == KIND_PR else "issue",
            entry.get("number", 0),
            _flat(entry.get("title")) or "-",
            entry.get("rounds", 0),
            entry.get("waiting_hours", 0.0),
            _flat(entry.get("summary") or entry.get("verdict")) or "无 verdict 摘要",
            entry.get("action", ACTION_MANUAL),
            entry.get("reason", ""),
        ))
    return "\n".join(lines)


def render_line(entries):
    """The summary on one line: the shape the brain log takes.

    Every entry carries the number, the kind, the round count, the waiting time, the
    title, the last verdict summary and the recommended action: the line a human reads
    has to name the card, not only its number.
    """
    items = entries or []
    if not items:
        return "needs-human：无积压"
    parts = []
    for entry in items:
        parts.append("#%d(%s,%d轮,%.1fh,%s,%s,%s)" % (
            entry.get("number", 0),
            "PR" if entry.get("kind") == KIND_PR else "issue",
            entry.get("rounds", 0),
            entry.get("waiting_hours", 0.0),
            _flat(entry.get("title")) or "-",
            _flat(entry.get("summary") or entry.get("verdict")) or "无 verdict 摘要",
            entry.get("action", ACTION_MANUAL),
        ))
    return "needs-human 积压 %d 项：%s" % (len(items), " ".join(parts))


def item_key(entry):
    """The key of one entry in the report state: the kind and the number."""
    return "%s:%d" % (entry.get("kind") or KIND_ISSUE, int(entry.get("number") or 0))


def due(entries, state, now=None, interval_h=DEFAULT_INTERVAL_H):
    """The entries that have to be reported now, and the new report state.

    An entry is due when it is new, or when it waited another full interval since it
    was written the last time.  The state of every entry that is still in the backlog
    is kept, so an entry that is not due in this round is not written again in the
    next one; only an entry that left the backlog is dropped from the state.
    """
    moment = time.time() if now is None else now
    seen = dict((state or {}).get("items") or {})
    items, out = {}, []
    for entry in entries or []:
        key = item_key(entry)
        known = seen.get(key) or {}
        waiting = float(entry.get("waiting_hours") or 0.0)
        last_wait = float(known.get("waiting_hours") or 0.0)
        last_at = float(known.get("reported_at") or 0.0)
        overdue = waiting >= interval_h and (
            waiting - last_wait >= interval_h or moment - last_at >= interval_h * 3600)
        if not known or overdue:
            out.append(entry)
            items[key] = {"reported_at": moment, "waiting_hours": waiting}
        else:
            items[key] = known
    return out, items


def load_state(path):
    """The report state of one state dir; a damaged file yields an empty state."""
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path, state):
    """Write the report state; a broken state dir must not break the report."""
    try:
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(state or {}, stream, indent=1, ensure_ascii=False)
    except OSError:
        return False
    return True


__all__ = [
    "ACTION_MANUAL",
    "ACTION_MAINTAINER",
    "ACTION_SPLIT",
    "ACTION_USER",
    "DEFAULT_INTERVAL_H",
    "KIND_ISSUE",
    "KIND_PR",
    "LABEL",
    "MAX_PAGES",
    "PAGE_SIZE",
    "STATE_FILE",
    "classify_action",
    "collect",
    "due",
    "item_key",
    "labelled_pages",
    "last_review_verdict",
    "load_state",
    "parse_time",
    "parse_verdict",
    "render",
    "render_line",
    "save_state",
    "summarize",
]
