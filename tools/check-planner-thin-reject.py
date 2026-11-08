#!/usr/bin/env python3
"""Regression check for thin planner cards in docs/backlog/next (Refs #1643)."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREDICATE = ROOT / "tools/check-ad-ready-target.py"


def load_contract():
    spec = importlib.util.spec_from_file_location("planner_card_contract", PREDICATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    contract = load_contract()
    thin_cards = {
        "missing-body": "# Planner card\nlabels: p1\n验收：python3 check.py\n",
        "missing-acceptance": "# Planner card\nlabels: p1\nImplement the bounded behavior.\n",
        "missing-labels": "# Planner card\nImplement the bounded behavior.\n验收：python3 check.py\n",
    }
    thick_cards = {
        "bus-result": (
            "# Bus result delivery is idempotent\nlabels: p1,backend:pi\n"
            "Update the worker result path and tests. Cover successful delivery, "
            "a repeated identical result, and a conflicting result.\n"
            "验收：`python3 -m unittest swrm.tests.test_bus`\n"
        ),
        "base-unavailable": (
            "# Reject unavailable repository bases before claim\nlabels: p1,backend:codex\n"
            "Update planner and worker checkout paths plus protocol tests. "
            "An unavailable base must fail before creating a branch or running verify; "
            "a reachable base must still complete normally.\n"
            "验收：`python3 -m unittest swrm.tests.test_github_issue_bus`\n"
        ),
        "invalid-result": (
            "# Ignore malformed result comments\nlabels: p1,backend:codex\n"
            "Validate result fields before accepting a terminal status. Test malformed "
            "comments followed by a valid winning result and confirm only the valid "
            "result is accepted.\n"
            "验收：`python3 -m unittest swrm.tests.test_github_issue_bus`\n"
        ),
    }

    scratch_root = ROOT / ".swrm-tmp"
    scratch_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch_root) as raw:
        cards_dir = Path(raw)
        for name, text in {**thin_cards, **thick_cards}.items():
            (cards_dir / f"{name}.md").write_text(text, encoding="utf-8")

        accepted = contract.local_candidates(cards_dir)
        expected = [thick_cards[name] for name in sorted(thick_cards)]
        assert accepted == expected, "thin planner cards must be skipped and thick cards passed"
        for name, text in thin_cards.items():
            assert contract.is_thin_card(text), f"thin sample {name} was accepted"
        for name, text in thick_cards.items():
            assert not contract.is_thin_card(text), f"thick sample {name} was rejected"

    print("planner thin-card rejection: OK")


if __name__ == "__main__":
    main()
