#!/usr/bin/env python3
"""Offline contract check for the seed-mint burst cooldown (Refs #1140).

The small model below mirrors the control-plane guard: a successful seed mint
records its timestamp, and another mint is skipped until ``MINT_COOLDOWN``
seconds have elapsed.  The check uses a fake clock and never touches live AD
state.
"""

from __future__ import annotations


MINT_COOLDOWN = 60.0
_last_mint: float | None = None


def seed_mint(now: float) -> bool:
    """Return whether a seed mint is allowed at *now* and record successes."""
    global _last_mint
    if _last_mint is not None and now - _last_mint < MINT_COOLDOWN:
        return False
    _last_mint = now
    return True


def main() -> None:
    global _last_mint
    _last_mint = None
    try:
        fake_now = 1_000_000.0
        assert seed_mint(fake_now), "the first seed mint must be allowed"
        fake_now += MINT_COOLDOWN - 1
        interval = fake_now - 1_000_000.0
        assert interval < MINT_COOLDOWN, "the second mint must be inside the cooldown"
        assert not seed_mint(fake_now), "a burst mint must be skipped"
        assert _last_mint == 1_000_000.0, "a skipped mint must not refresh the timestamp"

        fake_now += 1
        assert seed_mint(fake_now), "minting must resume at the cooldown boundary"
    finally:
        _last_mint = None

    print("seed-mint cooldown: second mint skipped inside MINT_COOLDOWN: OK")


if __name__ == "__main__":
    main()
