#!/usr/bin/env python3
"""Offline contract for refilling AD's ready queue to TARGET (Refs #1242).

The fixture is entirely local: this check must not invoke gh, git, SSH, or
any other network-backed command.

The planner cards also carry the budget tier of #1686: a large card is marked
with the `budget:large` label or a `预算：90` line, every other card keeps the
default budget.  A malformed marker is reported instead of being accepted, and
an unmarked card that looks large is only hinted — never promoted.
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTODISPATCH_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"

LOW = 10
TARGET = 20


def load_autodispatch():
    """Load the dispatcher by path: the budget marker semantics live there (Refs #1686)."""
    spec = importlib.util.spec_from_file_location("ad_budget_contract", AUTODISPATCH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AD = load_autodispatch()


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


def card_budget(text: str) -> dict:
    """The budget decision of a planner card (Refs #1686)."""
    return AD.card_budget(text)


def card_budget_hint(text: str):
    """The hint that an unmarked card looks like a large card (never a promotion)."""
    return AD.card_budget_hint(text)


def local_candidates(directory: Path) -> list[str]:
    """Read only local markdown cards and discard thin or malformed ones."""
    return [
        text
        for path in sorted(directory.glob("*.md"))
        if not is_thin_card(text := path.read_text(encoding="utf-8"))
        and not card_budget(text)["error"]
    ]


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

    # Budget tier of a planner card (Refs #1686).
    large = "# Large card\nlabels: p1,backend:codex,budget:large\nImplement the bounded path.\n验收：python3 check.py\n"
    marked_body = "# Large card\nlabels: p1\nImplement the bounded path.\n预算：90\n验收：python3 check.py\n"
    broken = "# Large card\nlabels: p1\nImplement the bounded path.\n预算：九十\n验收：python3 check.py\n"
    broken_label = "# Large card\nlabels: p1,budget:huge\nImplement the bounded path.\n验收：python3 check.py\n"
    assert card_budget(large)["tier"] == "large"
    assert card_budget(large)["error"] is None
    assert card_budget(marked_body)["tier"] == "large"
    assert card_budget(marked_body)["error"] is None
    assert card_budget(broken)["tier"] == "default"
    assert card_budget(broken)["error"]
    assert card_budget(broken_label)["error"]
    assert card_budget(good)["tier"] == "default"
    assert card_budget(good)["error"] is None

    # A long card without a marker is hinted, never promoted (Refs #1686).
    long_card = "# Long card\nlabels: p1\n" + ("实现正文。" * 300) + "\n验收：python3 check.py\n"
    assert card_budget(long_card)["tier"] == "default"
    assert card_budget_hint(long_card)
    assert card_budget_hint(good) is None
    assert card_budget_hint(large) is None

    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        (directory / "good.md").write_text(good, encoding="utf-8")
        (directory / "large.md").write_text(large, encoding="utf-8")
        (directory / "broken.md").write_text(broken, encoding="utf-8")
        assert local_candidates(directory) == [good, large]

    print("ad ready/target contract: OK")


if __name__ == "__main__":
    main()
