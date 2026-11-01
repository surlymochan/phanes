#!/usr/bin/env python3
"""Offline contract for the dual-mode cooldown pause set (Refs #977)."""

from __future__ import annotations

import tempfile
from pathlib import Path


ALLOWED_COOLDOWN_PAUSES = frozenset(
    {
        "PAUSE-HOST-ib1",
        "PAUSE-HOST-ib2",
        "PAUSE-M1",
        "PAUSE-M2",
    }
)


def pause_set(state_dir: Path) -> set[str]:
    """Return pause files in the supplied state directory."""
    return {entry.name for entry in state_dir.iterdir() if entry.is_file() and entry.name.startswith("PAUSE")}


def assert_cooldown_pause_set(state_dir: Path) -> None:
    """Require the cooldown fixture to contain exactly the production allowlist."""
    actual = pause_set(state_dir)
    if actual != ALLOWED_COOLDOWN_PAUSES:
        raise AssertionError(
            f"cooldown pause set mismatch: expected {sorted(ALLOWED_COOLDOWN_PAUSES)}, "
            f"got {sorted(actual)}"
        )


def main() -> None:
    # Use a fake state directory so this check never mutates the live pause directory.
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as fixture:
        state_dir = Path(fixture)
        for name in ALLOWED_COOLDOWN_PAUSES:
            (state_dir / name).touch()

        assert_cooldown_pause_set(state_dir)
        print("OK: cooldown pause set contains only PAUSE-HOST-ib1/2 and PAUSE-M1/M2")

        (state_dir / "PAUSE-HOST-ib3").touch()
        try:
            assert_cooldown_pause_set(state_dir)
        except AssertionError:
            print("OK: unexpected cooldown pause is rejected")
        else:
            raise AssertionError("unexpected cooldown pause was accepted")


if __name__ == "__main__":
    main()
