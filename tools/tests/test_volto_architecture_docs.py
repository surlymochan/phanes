"""Regression guard for the Volto architecture notes (Refs #18).

Issue #18 asks for the architecture notes of the OpenBot tree, now `volto/`, to agree
with the implementation they describe. A note that names a file the tree does not hold
is documentation drift: a reader follows the path and finds nothing.

The guard reads the architecture documents, collects the repository-relative paths they
name and fails while one of them is missing.

What counts as a repository-relative path
-----------------------------------------
* The first segment of the path is a top-level directory of the `volto/` tree
  (`src/`, `apps/`, `packages/`, `remote/`, ...), so `src/backend/volto-database.ts` is
  one and `agent/tool-catalog.ts` is not.
* The path has at least two segments.
* It is not a URL, an absolute path, a home path or a templated path such as
  `src/main/<name>-service.ts`; those name a shape, not a file of the tree.

Where a path is resolved
------------------------
* A path in an inline code span is resolved against the root of the `volto/` tree.
* A path in a Markdown link is resolved against the directory of its document and then
  against the root of the tree.

A path that a document marks with `待核实` on the same line is a known open question:
it is listed with the findings, but it does not fail the guard, because a document may
name a path the tree does not hold on purpose.

    python3 -m unittest discover -s tools/tests -p 'test_volto_architecture_docs.py'
"""

from __future__ import annotations

import re
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
VOLTO_DIR = REPO_ROOT / "volto"
INDEX_DOC = "docs/ARCHITECTURE.md"
TOPIC_DOC_DIR = "docs/architecture"
TODO_MARKER = "待核实"
PREFIXES = ("openbot-", "volto-", "swrm-")

CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
PLACEHOLDER_CHARS = set("<>*?{}|")

_TREE_INDEX: dict[str, list[str]] | None = None


def documents() -> list[Path]:
    """Return the architecture documents the guard reads."""
    found = []
    index = VOLTO_DIR / INDEX_DOC
    if index.is_file():
        found.append(index)
    topic_dir = VOLTO_DIR / TOPIC_DOC_DIR
    if topic_dir.is_dir():
        found.extend(sorted(topic_dir.glob("*.md")))
    return found


def top_level_roots() -> set[str]:
    """Return the first segments that start a repository-relative path."""
    if not VOLTO_DIR.is_dir():
        return set()
    roots = {entry.name for entry in VOLTO_DIR.iterdir() if entry.is_dir()}
    # A path may also be written from the repository root, as `volto/src/main/...`.
    roots.add(VOLTO_DIR.name)
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


def resolve_code_path(token: str) -> Path:
    """Return the path a repository-relative code span points at."""
    path = token[:-1] if token.endswith("/") else token
    if path.split("/", 1)[0] == VOLTO_DIR.name:
        return REPO_ROOT / path
    return VOLTO_DIR / path


def tree_index() -> dict[str, list[str]]:
    """Return the files of the `volto/` tree by name, relative to the tree root."""
    global _TREE_INDEX
    if _TREE_INDEX is not None:
        return _TREE_INDEX
    index: dict[str, list[str]] = {}
    for path in VOLTO_DIR.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(VOLTO_DIR).parts
        if any(part in {"node_modules", ".git"} for part in parts):
            continue
        index.setdefault(path.name, []).append(path.relative_to(VOLTO_DIR).as_posix())
    _TREE_INDEX = index
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


def collect_findings() -> tuple[list[str], list[str], int]:
    """Return the (missing, todo, referenced) findings of the architecture documents."""
    roots = top_level_roots()
    index = tree_index()
    missing: list[str] = []
    todo: list[str] = []
    referenced = 0
    for document in documents():
        relative = document.relative_to(VOLTO_DIR).as_posix()
        lines = document.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            spans = line_spans(line, roots)
            for position, (_, _, text, kind) in enumerate(spans):
                referenced += 1
                if kind == "path":
                    if resolve_code_path(text).exists():
                        continue
                    finding = f"{relative}:{number}: missing path `{text}`"
                else:
                    if (document.parent / text).exists() or (VOLTO_DIR / text).exists():
                        continue
                    finding = f"{relative}:{number}: missing link target `{text}`"
                hints = candidates_for(text, index)
                if hints:
                    finding += " (the tree holds " + ", ".join(hints) + ")"
                if is_marked_todo(line, spans, position):
                    todo.append(finding)
                else:
                    missing.append(finding)
    return missing, todo, referenced


class PathRulesTests(unittest.TestCase):
    """The rules of the guard, so that it cannot pass because it reads nothing."""

    def test_repository_relative_paths_are_recognised(self):
        roots = top_level_roots()
        self.assertTrue(is_repo_relative("src/backend/volto-database-schema.ts", roots))
        self.assertFalse(is_repo_relative("agent/tool-catalog.ts", roots))
        self.assertFalse(is_repo_relative("src/main/<name>-service.ts", roots))
        self.assertFalse(is_repo_relative("https://example.com/src/main/index.ts", roots))
        self.assertFalse(is_repo_relative("~/.config/opencode/config.json", roots))

    def test_paths_resolve_against_the_tree(self):
        self.assertTrue(resolve_code_path("src/backend/volto-database-schema.ts").exists())
        self.assertTrue(resolve_code_path("apps/auth-api/src/lib/news.ts").exists())
        self.assertFalse(resolve_code_path("apps/desktop").exists())

    def test_a_marked_open_question_is_reported_but_not_a_finding(self):
        line = "The path `apps/desktop` (待核实: the repository has no such directory) is a counterexample."
        spans = line_spans(line, top_level_roots())
        self.assertEqual([span[2] for span in spans], ["apps/desktop"])
        self.assertTrue(is_marked_todo(line, spans, 0))
        self.assertFalse(is_marked_todo(line.replace(TODO_MARKER, ""), spans, 0))


class ArchitectureDocPathTests(unittest.TestCase):
    def test_architecture_documents_are_in_scope(self):
        # The guard is only meaningful when it reads the notes: the index, every topic
        # document and a healthy number of repository-relative paths have to be in it.
        found = documents()
        names = [document.relative_to(VOLTO_DIR).as_posix() for document in found]
        self.assertIn(INDEX_DOC, names)
        self.assertIn("docs/architecture/team-api.md", names)
        self.assertGreaterEqual(len(names), 10)
        _, _, referenced = collect_findings()
        self.assertGreater(referenced, 50)

    def test_architecture_documents_name_existing_paths(self):
        missing, _, _ = collect_findings()
        report = "\n".join(f"  {finding}" for finding in missing)
        self.assertEqual(
            missing, [],
            "the architecture notes name paths the tree does not hold; point them at the "
            "files of the tree, or mark the ones that are still open with 待核实:\n"
            + report)


if __name__ == "__main__":
    unittest.main()
