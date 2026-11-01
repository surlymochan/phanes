#!/usr/bin/env python3
"""Offline contract for refilling AD's ready queue to TARGET (Refs #1259).

The fixture is entirely local: this check must not invoke gh, git, SSH, or
any other network-backed command.
"""
from __future__ import annotations

import ast
import tempfile
from pathlib import Path


LOW = 10
TARGET = 20


def refill_need(ready: int, low: int = LOW, target: int = TARGET, starved: int = 0) -> int:
    """Return the number of cards to request for a low or backend-starved queue."""
    if ready < low:
        return max(target - ready, starved, 0)
    return max(starved, 0)


def is_thin_card(text: str) -> bool:
    """Reject cards without a title, labels, substantive body, and acceptance."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 4 or not lines[0].startswith("# ") or not lines[1].startswith("labels:"):
        return True
    body = lines[2:]
    return len(body) < 2 or not any(line.startswith("验收：") for line in body)


def local_candidates(directory: Path) -> list[str]:
    """Read only local markdown cards and discard thin cards."""
    return [
        text
        for path in sorted(directory.glob("*.md"))
        if not is_thin_card(text := path.read_text(encoding="utf-8"))
    ]


def assert_offline_only() -> None:
    """The contract must stay offline: no shell-out module and no `gh` helper."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"subprocess", "os"}, f"offline contract imports {sorted(imported)}"
    assert not any(name.startswith("gh") for name in globals()), "offline contract must not call gh"


def main() -> None:
    # Below LOW, refill to TARGET; a larger starvation request still wins.
    for ready, low, target, starved in (
        (6, 8, 15, 0),
        (6, 8, 15, 10),
        (9, 10, 20, 0),
        (0, 10, 20, 0),
        (3, 10, 20, 25),
    ):
        need = refill_need(ready, low=low, target=target, starved=starved)
        expected = max(target - ready, starved, 0) if ready < low else max(starved, 0)
        assert need == expected
    assert refill_need(8, low=8, target=15) == 0
    assert refill_need(0) == 20
    assert refill_need(9) == 11
    assert refill_need(3) == 17
    assert refill_need(10) == 0
    assert refill_need(10, starved=3) == 3
    assert refill_need(12, starved=3) == 3
    assert refill_need(5, starved=18) == 18
    assert refill_need(5, starved=12) == 15

    # The refill lands the queue on TARGET, not one card above LOW.
    for ready, low, target in ((9, 10, 20), (6, 8, 15), (0, 10, 20), (3, 10, 20)):
        need = refill_need(ready, low=low, target=target)
        assert ready + need == target, (ready, low, target, need)
        assert ready + need >= target
    # ready == LOW is not low: nothing is requested; one card below LOW refills to TARGET.
    assert refill_need(LOW, low=LOW, target=TARGET) == 0
    assert refill_need(LOW - 1, low=LOW, target=TARGET) == TARGET - (LOW - 1)
    # A larger starvation request wins and overshoots TARGET.
    assert refill_need(3, low=LOW, target=TARGET, starved=25) == 25
    assert 3 + refill_need(3, low=LOW, target=TARGET, starved=25) > TARGET

    thin_short = "# Too thin\nlabels: p1\n"
    thin_empty = ""
    thin_no_title = "No title\nlabels: p1\nImplement the path.\n验收：python3 check.py\n"
    thin_no_labels = "# Title\nNo labels line\nImplement the path.\n验收：python3 check.py\n"
    thin_no_acceptance = "# Useful card\nlabels: p1\nImplement the bounded refill path.\nNo acceptance.\n"
    thin_one_body = "# Useful card\nlabels: p1\n验收：python3 check.py\n"
    thin_blank_body = "# Useful card\nlabels: p1\n\n\n"
    thin_title_no_space = "#Useful card\nlabels: p1\nImplement the bounded refill path.\n验收：python3 check.py\n"
    thin_labels_not_second = "# Useful card\nAn intro line\nlabels: p1\n验收：python3 check.py\n"
    good = "# Useful card\nlabels: p1\nImplement the bounded refill path.\n验收：python3 check.py\n"
    good_pinned = "# Pinned card\nlabels: p1,backend:codex\nImplement the bounded refill path.\n验收：python3 check.py\n"

    assert is_thin_card(thin_short)
    assert is_thin_card(thin_empty)
    assert is_thin_card(thin_no_title)
    assert is_thin_card(thin_no_labels)
    assert is_thin_card(thin_no_acceptance)
    assert is_thin_card(thin_one_body)
    assert is_thin_card(thin_blank_body)
    assert is_thin_card(thin_title_no_space)
    assert is_thin_card(thin_labels_not_second)
    assert not is_thin_card(good)
    assert not is_thin_card(good_pinned)

    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        (directory / "thin.md").write_text(thin_short, encoding="utf-8")
        (directory / "good.md").write_text(good, encoding="utf-8")
        (directory / "pinned.md").write_text(good_pinned, encoding="utf-8")
        assert local_candidates(directory) == [good, good_pinned]

    assert_offline_only()

    print("ad ready/target contract: OK")


if __name__ == "__main__":
    main()
