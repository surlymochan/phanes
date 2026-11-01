#!/usr/bin/env python3
"""Offline contract for health-loop quota pause recovery (Refs #845)."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


# Keep this deliberately explicit: health-loop only releases a quota pause once
# the hold has elapsed and scan_quota finds no fresh quota signal.
QUOTA_HOLD = 3600


def scan_quota(quota_hits: list[float], now: float) -> bool:
    """Return whether a quota/429 hit is still fresh within QUOTA_HOLD."""
    return any(now - hit < QUOTA_HOLD for hit in quota_hits)


def clear_auto(state_dir: Path, now: float, quota_hits: list[float]) -> list[str]:
    """Clear only old auto pauses whose backend has no fresh quota hit."""
    cleared: list[str] = []
    for pause in state_dir.glob("PAUSE-BACKEND-*"):
        if not pause.is_file() or not pause.read_text().startswith("auto:"):
            continue
        backend = pause.name.removeprefix("PAUSE-BACKEND-")
        if now - pause.stat().st_mtime >= QUOTA_HOLD and not scan_quota(quota_hits, now):
            pause.unlink()
            cleared.append(backend)
    return cleared


def dispatchable(state_dir: Path, backend: str) -> bool:
    """The pause-file gate: once removed, the same backend can be dispatched."""
    return not (state_dir / f"PAUSE-BACKEND-{backend}").exists()


def main() -> None:
    # The temporary fixture is created beside this script, never in the live
    # ~/phanes-ctl/autodispatch directory (or any other external directory).
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as fixture:
        state_dir = Path(fixture)
        now = time.time()
        old = now - QUOTA_HOLD - 1

        auto_pause = state_dir / "PAUSE-BACKEND-pi"
        auto_pause.write_text("auto: quota 429\n")
        os.utime(auto_pause, (old, old))

        assert clear_auto(state_dir, now, []) == ["pi"]
        assert dispatchable(state_dir, "pi"), "cleared backend must be dispatchable"

        manual_pause = state_dir / "PAUSE-BACKEND-pi"
        manual_pause.write_text("operator hold\n")
        os.utime(manual_pause, (old, old))
        assert clear_auto(state_dir, now, []) == []
        assert not dispatchable(state_dir, "pi"), "manual pause must remain a gate"

        print("OK: old auto pause clears after QUOTA_HOLD with no fresh quota hit")
        print("OK: manual PAUSE-BACKEND pause is never auto-cleared")
        print("OK: cleared backend is dispatchable again")


if __name__ == "__main__":
    main()
