#!/usr/bin/env python3
"""Offline contract for refilling AD's ready queue to TARGET (Refs #1040).

The fixture is entirely local: this check must not invoke gh, git, SSH, or
any other network-backed command.
"""
from __future__ import annotations

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


def main() -> None:
    assert refill_need(0) == 20
    assert refill_need(9) == 11
    assert refill_need(3) == 17
    assert refill_need(10) == 0
    assert refill_need(10, starved=3) == 3
    assert refill_need(12, starved=3) == 3
    assert refill_need(5, starved=18) == 18
    assert refill_need(5, starved=12) == 15

    thin_short = "# Too thin\nlabels: p1\n"
    thin_empty = ""
    thin_no_title = "No title\nlabels: p1\nImplement the path.\n验收：python3 check.py\n"
    thin_no_labels = "# Title\nNo labels line\nImplement the path.\n验收：python3 check.py\n"
    thin_no_acceptance = "# Useful card\nlabels: p1\nImplement the bounded refill path.\nNo acceptance.\n"
    thin_one_body = "# Useful card\nlabels: p1\n验收：python3 check.py\n"
    good = "# Useful card\nlabels: p1\nImplement the bounded refill path.\n验收：python3 check.py\n"

    assert is_thin_card(thin_short)
    assert is_thin_card(thin_empty)
    assert is_thin_card(thin_no_title)
    assert is_thin_card(thin_no_labels)
    assert is_thin_card(thin_no_acceptance)
    assert is_thin_card(thin_one_body)
    assert not is_thin_card(good)

    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        (directory / "thin.md").write_text(thin_short, encoding="utf-8")
        (directory / "good.md").write_text(good, encoding="utf-8")
        assert local_candidates(directory) == [good]

    print("ad ready/target contract: OK")


if __name__ == "__main__":
    main()
