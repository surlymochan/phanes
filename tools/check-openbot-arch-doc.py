#!/usr/bin/env python3
"""Check the OpenBot architecture notes against the tree they describe (Refs #18).

The architecture notes of the OpenBot tree (`volto/`, renamed from `openbot/`) name the
files they describe. A note that names a path the tree does not hold is documentation
drift: a reader follows the path and finds nothing. This tool lists every such path and
exits non-zero, so the drift is caught before it ships.

What counts as a repository-relative path
-----------------------------------------
* The first segment of the path is a top-level directory of the tree (`src/`, `apps/`,
  `packages/`, `remote/`, ...), so `src/backend/volto-database.ts` is one and
  `agent/tool-catalog.ts` is not.
* The path has at least two segments.
* It is not a URL, an absolute path, a home path or a templated path such as
  `src/main/<name>-service.ts`; those name a shape, not a file of the tree.

Where a path is resolved
------------------------
* A path in an inline code span is resolved against the root of the tree.
* A path in a Markdown link is resolved against the directory of its document and then
  against the root of the tree.

A path that a document marks with `待核实` on the same line is a known open question: it
is listed with the findings, but it does not fail the check, because a document may name
a path the tree does not hold on purpose.

Usage:
    python3 tools/check-openbot-arch-doc.py            # report the drift
    python3 tools/check-openbot-arch-doc.py --json     # the same report as JSON
    python3 tools/check-openbot-arch-doc.py --self-test # offline fixtures
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[1]
TREE_NAMES = ("volto", "openbot")
INDEX_DOC = "docs/ARCHITECTURE.md"
TOPIC_DOC_DIR = "docs/architecture"
TODO_MARKER = "待核实"
PREFIXES = ("openbot-", "volto-", "swrm-")

CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
PLACEHOLDER_CHARS = set("<>*?{}|")


def tree_root(explicit: Path | None = None) -> Path:
    """Return the root of the OpenBot tree the notes are checked against."""
    if explicit is not None:
        return explicit
    for name in TREE_NAMES:
        candidate = REPO_ROOT / name
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"no OpenBot tree found: {', '.join(TREE_NAMES)}")


def documents(tree: Path) -> list[Path]:
    """Return the architecture documents the check reads."""
    found = []
    index = tree / INDEX_DOC
    if index.is_file():
        found.append(index)
    topic_dir = tree / TOPIC_DOC_DIR
    if topic_dir.is_dir():
        found.extend(sorted(topic_dir.glob("*.md")))
    return found


def top_level_roots(tree: Path) -> set[str]:
    """Return the first segments that start a repository-relative path."""
    if not tree.is_dir():
        return set()
    roots = {entry.name for entry in tree.iterdir() if entry.is_dir()}
    # A path may also be written from the repository root, as `volto/src/main/...`.
    roots.add(tree.name)
    return roots


def is_placeholder(token: str) -> bool:
    """Report whether the token holds a placeholder instead of a real path."""
    return any(char in PLACEHOLDER_CHARS for char in token)


def is_repo_relative(token: str, roots: set[str]) -> bool:
    """Report whether the token is a repository-relative path with two segments."""
    if not token or " " in token or is_placeholder(token):
        return False
    if "://" in token or token.startswith(("/", "~", "$", "@", "#")):
        return False
    if SCHEME_RE.match(token):
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
    if SCHEME_RE.match(target):
        return ""
    if is_placeholder(target):
        return ""
    return target


def line_spans(line: str, roots: set[str]) -> list[tuple[int, int, str, str]]:
    """Return the (start, end, text, kind) spans of the line that can hold a path."""
    spans = []
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
    before = line[:start] if index == 0 else line[spans[index - 1][1]:start]
    after_end = spans[index + 1][0] if index + 1 < len(spans) else len(line)
    after = line[end:after_end]
    return TODO_MARKER in before or TODO_MARKER in after


def resolve_code_path(tree: Path, token: str) -> Path:
    """Return the path a repository-relative code span points at."""
    path = token[:-1] if token.endswith("/") else token
    if path.split("/", 1)[0] == tree.name:
        return tree.parent / path
    return tree / path


def tree_index(tree: Path) -> dict[str, list[str]]:
    """Return the files of the tree by name, relative to the tree root."""
    index: dict[str, list[str]] = {}
    for path in tree.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(tree).parts
        if any(part in {"node_modules", ".git"} for part in parts):
            continue
        index.setdefault(path.name, []).append(path.relative_to(tree).as_posix())
    return index


def candidates_for(token: str, index: dict[str, list[str]]) -> list[str]:
    """Return the files of the tree that could be the path a document meant."""
    name = token.rsplit("/", 1)[-1]
    names = [name]
    for prefix in PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            stem = name[len(prefix):]
            names.extend(other + stem for other in PREFIXES if other != prefix)
            break
    found = []
    for candidate in names:
        matches = index.get(candidate, [])
        if len(matches) == 1 and matches[0] not in found:
            found.append(matches[0])
    return found


def check(tree: Path) -> dict:
    """Return the findings of the architecture documents of the tree."""
    roots = top_level_roots(tree)
    index = tree_index(tree)
    missing: list[str] = []
    todo: list[str] = []
    referenced = 0
    for document in documents(tree):
        relative = document.relative_to(tree).as_posix()
        lines = document.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            spans = line_spans(line, roots)
            for position, (_, _, text, kind) in enumerate(spans):
                referenced += 1
                if kind == "path":
                    if resolve_code_path(tree, text).exists():
                        continue
                    finding = f"{relative}:{number}: missing path `{text}`"
                else:
                    if (document.parent / text).exists() or (tree / text).exists():
                        continue
                    finding = f"{relative}:{number}: missing link target `{text}`"
                hints = candidates_for(text, index)
                if hints:
                    finding += " (the tree holds " + ", ".join(hints) + ")"
                if is_marked_todo(line, spans, position):
                    todo.append(finding)
                else:
                    missing.append(finding)
    return {"missing": missing, "todo": todo, "referenced": referenced}


def self_test() -> None:
    """Run the built-in offline fixtures."""
    roots = {"src", "apps", "packages"}
    assert is_repo_relative("src/backend/volto-database.ts", roots)
    assert not is_repo_relative("agent/tool-catalog.ts", roots)
    assert not is_repo_relative("src/main/<name>-service.ts", roots)
    assert not is_repo_relative("https://example.com/src/main/index.ts", roots)
    assert not is_repo_relative("~/.config/opencode/config.json", roots)
    assert link_target("../../PRIVACY.md") == "../../PRIVACY.md"
    assert link_target("https://example.com/x") == ""
    line = "The path `apps/desktop` (待核实: no such directory) is a counterexample."
    spans = line_spans(line, roots)
    assert [span[2] for span in spans] == ["apps/desktop"]
    assert is_marked_todo(line, spans, 0)
    assert not is_marked_todo(line.replace(TODO_MARKER, ""), spans, 0)
    print("ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, help="root of the OpenBot tree (default: volto/ or openbot/)")
    parser.add_argument("--self-test", action="store_true", help="run built-in offline fixtures")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    try:
        tree = tree_root(args.tree)
        report = check(tree)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for finding in report["missing"]:
            print(finding)
        for finding in report["todo"]:
            print(f"{finding} (marked {TODO_MARKER})")
        print(
            f"{len(report['missing'])} missing path(s), {len(report['todo'])} open question(s), "
            f"{report['referenced']} path(s) checked in {tree.name}/"
        )
    return int(bool(report["missing"]))


if __name__ == "__main__":
    raise SystemExit(main())
