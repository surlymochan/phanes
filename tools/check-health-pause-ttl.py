#!/usr/bin/env python3
"""Offline contract for expiring automatic health-check PAUSE files (Refs #1261)."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


PAUSE_TTL = 3600
AUTO_PREFIX = "auto:"


def is_auto_body(body: str) -> bool:
    """Return whether a PAUSE body is automatic: the exact ``auto:`` prefix."""
    return body.startswith(AUTO_PREFIX)


def read_body(pause: Path) -> str:
    """Read a PAUSE body; an unreadable body counts as manual, never automatic."""
    try:
        return pause.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def clear_expired_auto_pauses(state_dir: Path, now: float, ttl: int = PAUSE_TTL) -> list[str]:
    """Remove only expired PAUSE files explicitly marked with ``auto:``."""
    cleared: list[str] = []
    for pause in state_dir.glob("PAUSE-*"):
        if not pause.is_file() or not is_auto_body(read_body(pause)):
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

        second_auto_pause = state_dir / "PAUSE-worker-auto"
        second_auto_pause.write_text("auto: quota 429\n")
        os.utime(second_auto_pause, (expired, expired))

        indented_auto_pause = state_dir / "PAUSE-backend-indented"
        indented_auto_pause.write_text(" auto: quota 429\n")
        os.utime(indented_auto_pause, (expired, expired))

        inline_auto_pause = state_dir / "PAUSE-backend-inline"
        inline_auto_pause.write_text("operator hold until auto: cleared\n")
        os.utime(inline_auto_pause, (expired, expired))

        binary_pause = state_dir / "PAUSE-backend-binary"
        binary_pause.write_bytes(b"\xff\xfe auto: quota 429\n")
        os.utime(binary_pause, (expired, expired))

        almost_expired_pause = state_dir / "PAUSE-backend-almost"
        almost_expired_pause.write_text("auto: quota 429\n")
        almost = now - PAUSE_TTL + 60
        os.utime(almost_expired_pause, (almost, almost))

        assert sorted(clear_expired_auto_pauses(state_dir, now)) == [
            "PAUSE-backend-auto",
            "PAUSE-worker-auto",
        ]
        assert not auto_pause.exists(), "expired auto PAUSE must be cleared"
        assert not second_auto_pause.exists(), "every expired auto PAUSE must be cleared"
        assert manual_pause.exists(), "manual PAUSE must not be cleared"
        assert manual_autoish_pause.exists(), "only the exact auto: prefix is automatic"
        assert indented_auto_pause.exists(), "auto: must start the body"
        assert inline_auto_pause.exists(), "an inline auto: is still a manual PAUSE"
        assert binary_pause.exists(), "an unreadable PAUSE body is never auto-cleared"
        assert fresh_auto_pause.exists(), "fresh auto PAUSE must not be cleared"
        assert almost_expired_pause.exists(), "auto PAUSE before its TTL must not be cleared"
        assert clear_expired_auto_pauses(state_dir, now) == [], "a second sweep clears nothing"

        print("OK: expired auto: PAUSE clears after PAUSE_TTL")
        print("OK: expired manual PAUSE remains present")
        print("OK: fresh auto: PAUSE remains present")
        print("OK: auto: must be the exact body prefix, not an inline word")
        print("OK: unreadable PAUSE body is never auto-cleared")
        print("OK: a repeated sweep clears nothing new")


if __name__ == "__main__":
    main()
