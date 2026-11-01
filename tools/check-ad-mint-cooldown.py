#!/usr/bin/env python3
"""Offline contract check for seed mint burst cooldown (Refs #1024)."""

from __future__ import annotations


MINT_COOLDOWN = 60.0
_last_mint: float | None = None


def mint_seed(seed: str, now: float, minted: list[str]) -> bool:
    """Mint once per cooldown window, returning whether this call minted."""
    global _last_mint

    if _last_mint is not None and now - _last_mint < MINT_COOLDOWN:
        return False
    minted.append(seed)
    _last_mint = now
    return True


def main() -> None:
    global _last_mint

    _last_mint = None
    minted: list[str] = []
    now = 1_000_000.0
    try:
        assert mint_seed("seed-1", now, minted)

        now += MINT_COOLDOWN - 1
        assert not mint_seed("seed-2", now, minted)
        assert minted == ["seed-1"]
        assert _last_mint == 1_000_000.0

        now += 1
        assert mint_seed("seed-2", now, minted)
        assert minted == ["seed-1", "seed-2"]
    finally:
        _last_mint = None

    print("seed mint cooldown: second mint skipped before 60s, accepted at 60s: OK")


if __name__ == "__main__":
    main()
