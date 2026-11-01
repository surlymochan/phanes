#!/usr/bin/env python3
"""Check that the OpenBot architecture docs only reference paths that exist.

Usage:
  python3 tools/check-openbot-arch-doc.py               # fail on any missing path
  python3 tools/check-openbot-arch-doc.py --allow-todo  # skip the paths marked 待核实

The script reads `openbot/docs/ARCHITECTURE.md` and `openbot/docs/architecture/*.md`,
extracts repository-relative paths from inline code spans and Markdown links, and checks
that every one of them exists under `openbot/`.

Only repository-relative paths are checked, that is a path whose first segment is a
top-level entry of the `openbot/` tree (`src/main/...`, `apps/...`, `packages/...`).
Module-relative paths (`agent/tool-catalog.ts`), URLs, absolute paths, home paths and
templated paths (`src/main/<name>-service.ts`) are not repository-relative and are skipped.

A path that cannot be found is printed as `missing:` and the script exits non-zero.
A path that is annotated with `待核实` next to it in its document is a known open
question: it is only reported when `--allow-todo` is not given.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TODO_MARKER = "待核实"
INDEX_DOC = "docs/ARCHITECTURE.md"
TOPIC_DOC_DIR = "docs/architecture"

CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")
PLACEHOLDER_CHARS = set("<>*?{}|")


def documents(openbot_dir: Path) -> list[Path]:
    """Return the architecture documents that the check reads."""
    index = openbot_dir / INDEX_DOC
    docs = [index] if index.is_file() else []
    topic_dir = openbot_dir / TOPIC_DOC_DIR
    if topic_dir.is_dir():
        docs.extend(sorted(topic_dir.glob("*.md")))
    return docs


def top_level_roots(openbot_dir: Path) -> set[str]:
    """Return the first segments that start a repository-relative path."""
    roots = {entry.name for entry in openbot_dir.iterdir() if entry.is_dir()}
    # A path may also be written from the repository root, as `openbot/src/main/...`.
    roots.add(openbot_dir.name)
    return roots


def is_placeholder(token: str) -> bool:
    """Report whether the token holds a placeholder instead of a real path."""
    return any(char in PLACEHOLDER_CHARS for char in token)


def is_repo_relative(token: str, roots: set[str]) -> bool:
    """Report whether the token is a repository-relative path with at least two segments."""
    if not token or " " in token or is_placeholder(token):
        return False
    if "://" in token or token.startswith(("/", "~", "$", "@", "#")):
        return False
    if token.count("/") < 1:
        return False
    return token.split("/", 1)[0] in roots


def link_target(raw: str) -> str:
    """Return the path part of a Markdown link target."""
    parts = raw.split()
    if not parts:
        return ""
    target = parts[0].strip("<>").split("#", 1)[0]
    if not target or "://" in target or target.startswith(("/", "~", "#")):
        return ""
    if is_placeholder(target):
        return ""
    return target


def line_spans(line: str, roots: set[str]) -> list[tuple[int, int, str, str]]:
    """Return the (start, end, text, kind) spans of the line that can hold a path."""
    spans: list[tuple[int, int, str, str]] = []
    for match in CODE_SPAN_RE.finditer(line):
        token = match.group(1).strip()
        if is_repo_relative(token, roots):
            spans.append((match.start(1), match.end(1), token, "path"))
    for match in LINK_RE.finditer(line):
        target = link_target(match.group(1))
        if target:
            spans.append((match.start(1), match.end(1), target, "link"))
    spans.sort()
    return spans


def is_marked_todo(line: str, spans: list[tuple[int, int, str, str]], index: int) -> bool:
    """Report whether the 待核实 marker sits right next to the span on the line."""
    start, end = spans[index][0], spans[index][1]
    before = line[:start] if index == 0 else line[spans[index - 1][1] : start]
    after_end = spans[index + 1][0] if index + 1 < len(spans) else len(line)
    after = line[end:after_end]
    return TODO_MARKER in before or TODO_MARKER in after


def resolve_code_path(openbot_dir: Path, repo_root: Path, token: str) -> Path:
    """Return the path a repository-relative code span points at."""
    path = token[:-1] if token.endswith("/") else token
    if path.split("/", 1)[0] == openbot_dir.name:
        return repo_root / path
    return openbot_dir / path


def check(openbot_dir: Path, repo_root: Path) -> tuple[list[str], list[str]]:
    """Return the (missing, todo) findings of the architecture documents."""
    roots = top_level_roots(openbot_dir)
    missing: list[str] = []
    todo: list[str] = []
    if not (openbot_dir / INDEX_DOC).is_file():
        missing.append(f"{INDEX_DOC}: missing document")
    for doc in documents(openbot_dir):
        relative = doc.relative_to(openbot_dir).as_posix()
        for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            spans = line_spans(line, roots)
            for index, (_, _, text, kind) in enumerate(spans):
                if kind == "path":
                    if resolve_code_path(openbot_dir, repo_root, text).exists():
                        continue
                    finding = f"{relative}:{number}: missing path `{text}`"
                else:
                    if (doc.parent / text).exists() or (openbot_dir / text).exists():
                        continue
                    finding = f"{relative}:{number}: missing link target `{text}`"
                if is_marked_todo(line, spans, index):
                    todo.append(finding)
                else:
                    missing.append(finding)
    return missing, todo


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-todo",
        action="store_true",
        help="skip the paths that the documents mark with 待核实",
    )
    parser.add_argument(
        "--openbot-dir",
        type=Path,
        default=None,
        help="the OpenBot tree to check (defaults to `openbot/` next to this script)",
    )
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    openbot_dir = args.openbot_dir or repo_root / "openbot"
    if not openbot_dir.is_dir():
        print(f"openbot tree not found: {openbot_dir}", file=sys.stderr)
        return 2
    missing, todo = check(openbot_dir, repo_root)
    for finding in missing:
        print(f"missing: {finding}", file=sys.stderr)
    for finding in todo:
        if not args.allow_todo:
            print(f"todo: {finding}", file=sys.stderr)
    if missing or (todo and not args.allow_todo):
        print(
            f"openbot architecture doc paths: {len(missing)} missing, {len(todo)} 待核实",
            file=sys.stderr,
        )
        return 1
    if todo:
        print(f"openbot architecture doc paths: OK ({len(todo)} 待核实 skipped)")
    else:
        print("openbot architecture doc paths: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
