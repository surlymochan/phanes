#!/usr/bin/env python3
"""Offline contract for expiring automatic health-check PAUSE files (Refs #1224)."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


PAUSE_TTL = 3600
AUTO_PREFIX = "auto:"


def clear_expired_auto_pauses(state_dir: Path, now: float, ttl: int = PAUSE_TTL) -> list[str]:
    """Remove only expired PAUSE files explicitly marked with ``auto:``."""
    cleared: list[str] = []
    for pause in state_dir.glob("PAUSE-*"):
        if not pause.is_file() or not pause.read_text().startswith(AUTO_PREFIX):
            continue
        if now - pause.stat().st_mtime < ttl:
            continue
        pause.unlink()
        cleared.append(pause.name)
    return cleared


def main() -> None:
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as fixture:
        state_dir = Path(fixture)
        now = time.time()
        expired = now - PAUSE_TTL - 1

        auto_pause = state_dir / "PAUSE-backend-auto"
        auto_pause.write_text("auto: quota 429\n")
        os.utime(auto_pause, (expired, expired))

        manual_pause = state_dir / "PAUSE-backend-manual"
        manual_pause.write_text("operator hold\n")
        os.utime(manual_pause, (expired, expired))

        fresh_auto_pause = state_dir / "PAUSE-backend-fresh"
        fresh_auto_pause.write_text("auto: quota 429\n")

        manual_autoish_pause = state_dir / "PAUSE-backend-autoish"
        manual_autoish_pause.write_text("automatic: operator hold\n")
        os.utime(manual_autoish_pause, (expired, expired))

        assert clear_expired_auto_pauses(state_dir, now) == ["PAUSE-backend-auto"]
        assert not auto_pause.exists(), "expired auto PAUSE must be cleared"
        assert manual_pause.exists(), "manual PAUSE must not be cleared"
        assert manual_autoish_pause.exists(), "only the exact auto: prefix is automatic"
        assert fresh_auto_pause.exists(), "fresh auto PAUSE must not be cleared"

        print("OK: expired auto: PAUSE clears after PAUSE_TTL")
        print("OK: expired manual PAUSE remains present")
        print("OK: fresh auto: PAUSE remains present")


if __name__ == "__main__":
    main()
