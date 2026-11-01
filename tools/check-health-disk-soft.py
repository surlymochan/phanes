#!/usr/bin/env python3
"""Offline contract: disk_soft is an alert, never a host pause (Refs #969)."""

from __future__ import annotations

import tempfile
from pathlib import Path


DISK_SOFT = 80
DISK_HARD = 90


def evaluate_disk(used_percent: int) -> dict[str, str]:
    """Return the health level for a disk metric without changing state."""
    if used_percent >= DISK_HARD:
        return {"level": "hard", "alert": "disk_hard"}
    if used_percent >= DISK_SOFT:
        return {"level": "soft", "alert": "disk_soft"}
    return {"level": "ok", "alert": "disk_ok"}


def apply_disk_result(state_dir: Path, result: dict[str, str]) -> None:
    """Materialize only hard disk results as an automatic host pause."""
    if result["level"] == "hard":
        (state_dir / "PAUSE-HOST-test").write_text("auto: disk_hard\n")


def main() -> None:
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as fixture:
        state_dir = Path(fixture)
        result = evaluate_disk(DISK_SOFT)
        apply_disk_result(state_dir, result)

        assert result["alert"] == "disk_soft"
        assert result["level"] == "soft"
        assert not list(state_dir.glob("PAUSE-HOST-*")), (
            "disk_soft must not create a PAUSE-HOST file"
        )

        hard_result = evaluate_disk(DISK_HARD)
        apply_disk_result(state_dir, hard_result)
        assert hard_result["level"] == "hard"
        assert (state_dir / "PAUSE-HOST-test").exists()

        print("OK: disk_soft is level=soft and creates no PAUSE-HOST file")
        print("OK: disk_hard creates the expected automatic host pause")


if __name__ == "__main__":
    main()
