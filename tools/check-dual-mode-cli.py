#!/usr/bin/env python3
"""Check that the dual-mode runbooks still describe the local CLI contract."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path


DOC_RANGES = {
    "docs/dual-mode-checklist.md": (17, 75),
    "docs/dual-mode-ssh.md": (83, 130),
    "docs/dual-mode-issue-bus.md": (49, 81),
}
LOCAL_ENTRIES = {
    "openswarm": "openswarm/bin/openswarm",
    "openswarm-fleet": "openswarm/bin/openswarm-fleet",
}
REQUIRED_OPTIONS = {
    ("openswarm", "bus", "status"): {"--repo"},
    ("openswarm", "bus", "init"): set(),
    ("openswarm", "bus-worker"): {"--bus", "--name", "--backend"},
    ("openswarm", "run"): {"--backend", "--cwd", "--base", "--budget-minutes", "--verify", "--task-file"},
    ("openswarm", "watch"): set(),
    ("openswarm-fleet", "fleet", "status"): set(),
    ("openswarm-fleet", "fleet", "exec"): set(),
    ("openswarm-fleet", "lane", "assign"): {"--task-file", "--title", "--budget-minutes"},
    ("openswarm-fleet", "runs"): set(),
}
BUS_STATES = {"queued", "claimed", "done", "failed", "cancelled"}
BUS_LABEL_RE = re.compile(r"\b([A-Za-z0-9_.-]+):(queued|claimed|done|failed|cancelled)\b")
COMMAND_RE = re.compile(r"(?<![A-Za-z0-9_-])(openswarm(?:-fleet)?)(?=\s|$)")


def _source_lines(path: Path, start: int, end: int):
    lines = path.read_text(encoding="utf-8").splitlines()
    for number in range(start, min(end, len(lines)) + 1):
        yield number, lines[number - 1]


def _commands(path: Path, start: int, end: int):
    """Yield (line, executable, argv) for command snippets in a doc range."""
    pending = ""
    pending_line = None
    for number, raw in _source_lines(path, start, end):
        text = raw.strip()
        if pending:
            pending += " " + text
            if text.endswith("\\"):
                pending = pending[:-1].rstrip()
                continue
            text, number = pending, pending_line
            pending = ""
            pending_line = None
        elif text.endswith("\\"):
            pending = text[:-1].rstrip()
            pending_line = number
            continue

        for match in COMMAND_RE.finditer(text):
            try:
                tokens = shlex.split(text[match.start():], comments=False, posix=True)
            except ValueError:
                continue
            if tokens:
                yield number, tokens[0], tokens[1:]


def _help_text(root: Path, executable: str, argv: list[str]) -> str:
    command = [str(root / LOCAL_ENTRIES[executable]), *argv, "--help"]
    if executable == "openswarm":
        command.insert(0, sys.executable)
    result = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"--help exited {result.returncode}")
    return result.stdout


def check(root: Path, docs_dir: Path) -> list[str]:
    errors = []
    seen = 0
    labels = set()
    for relative, (start, end) in DOC_RANGES.items():
        path = docs_dir / Path(relative).name
        if not path.is_file():
            errors.append(f"{relative}: missing document")
            continue
        for line, executable, argv in _commands(path, start, end):
            seen += 1
            command_words = argv[:2] if argv[:1] == ["bus"] else argv[:1]
            key = (executable, *command_words)
            if executable not in LOCAL_ENTRIES:
                errors.append(f"{relative}:{line}: unknown local CLI entry {executable}")
                continue
            if not (root / LOCAL_ENTRIES[executable]).is_file():
                errors.append(f"{relative}:{line}: missing local CLI entry {LOCAL_ENTRIES[executable]}")
                continue
            if executable == "openswarm-fleet":
                # The wrapper delegates to openswarm; its first two words are the
                # actual command path (fleet exec, lane assign, or runs).
                key = (executable, *argv[:2])
            required = REQUIRED_OPTIONS.get(key)
            if required is None:
                errors.append(f"{relative}:{line}: unknown command {' '.join([executable, *argv[:2]])}")
                continue
            missing = sorted(option for option in required if option not in argv)
            if missing:
                errors.append(f"{relative}:{line}: missing allowlist parameter(s): {', '.join(missing)}")
            try:
                _help_text(root, executable, command_words)
            except RuntimeError as exc:
                errors.append(f"{relative}:{line}: command is not executable: {exc}")
        for line, raw in _source_lines(path, start, end):
            labels.update(match.group(0) for match in BUS_LABEL_RE.finditer(raw))

    if not seen:
        errors.append("the selected dual-mode document ranges contain no local CLI commands")
    unknown_labels = sorted(label for label in labels if label.split(":", 1)[1] not in BUS_STATES)
    if unknown_labels:
        errors.append(f"unknown bus status label(s): {', '.join(unknown_labels)}")
    required_labels = {"openswarm:queued", "openswarm:claimed", "openswarm:done"}
    if not required_labels.issubset(labels):
        errors.append("missing bus status label(s): " + ", ".join(sorted(required_labels - labels)))
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", type=Path, help="directory containing the three selected documents")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    docs_dir = args.docs_dir or root / "docs"
    errors = check(root, docs_dir)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("dual-mode CLI contract: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
