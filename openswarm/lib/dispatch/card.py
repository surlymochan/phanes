"""Card files: the format the planner writes and the mint path reads (Refs #1721).

A card file is the markdown a planner run writes into ``docs/backlog/next/``::

    # 标题
    labels: p1,backend:codex

    正文；热文件：openswarm/lib/dispatch/engine.py

``parse_card`` returns ``{"title", "labels", "body"}``.  The body keeps the ``热文件：``
line, so the mint preflight sees the hot files the card declares, and the labels are
the ones the card asks for (the ready label is added by the mint path itself).
"""
import re

TITLE_RE = re.compile(r"^#\s*(.+)$")
LABELS_RE = re.compile(r"^labels\s*[:：]\s*(.*)$", re.I)


def parse_card(text):
    """Return the title, the labels and the body of a card file.

    A card without a title line falls back to the first non-empty line, and a card
    without a ``labels:`` line simply has no labels: the mint path adds the ready
    label, so an unlabelled card still enters the dispatch queue.
    """
    lines = (text or "").splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    title = ""
    if index < len(lines):
        head = lines[index].strip()
        match = TITLE_RE.match(head)
        title = (match.group(1).strip() if match else head.lstrip("# ").strip())
        index += 1
    labels = []
    if index < len(lines):
        match = LABELS_RE.match(lines[index].strip())
        if match:
            labels = [label.strip() for label in match.group(1).split(",") if label.strip()]
            index += 1
    return {"title": title, "labels": labels, "body": "\n".join(lines[index:]).strip()}
