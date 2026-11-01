#!/usr/bin/env python3
"""Offline contract for health-loop pause recovery (Refs #845, #846)."""

from __future__ import annotations

import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


# Keep this deliberately explicit: health-loop only releases a quota pause once
# the hold has elapsed and scan_quota finds no fresh quota signal.
QUOTA_HOLD = 3600
HEALTH_MEM_HARD_MB = 256
HEALTH_MEM_SOFT_MB = 512
COOLDOWN_RE = re.compile(
    r"(?:cooling\s+down\s+until|machine_cooldown[^\n]*?until)\s*[=: ]\s*"
    r"([0-9]+(?:\.[0-9]+)?|[^\s,}\]]+)",
    re.IGNORECASE,
)


def _until_timestamp(value: str) -> float:
    """Parse the numeric or ISO-8601 timestamp emitted by an SSH probe."""
    try:
        return float(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()


def cooldown_until(probe_output: str) -> str | None:
    """Return the exact cooldown deadline from a probe, if it reports one."""
    match = COOLDOWN_RE.search(probe_output)
    if not match:
        return None
    value = match.group(1)
    _until_timestamp(value)  # Validate before putting probe data in a pause file.
    return value


def set_host_cooldown(state_dir: Path, host: str, probe_output: str) -> bool:
    """Write the hard host pause used for an SSH machine cooldown."""
    until = cooldown_until(probe_output)
    if until is None:
        return False
    (state_dir / f"PAUSE-HOST-{host}").write_text(f"auto:cooldown:until={until}\n")
    return True


def scan_quota(quota_hits: list[float], now: float) -> bool:
    """Return whether a quota/429 hit is still fresh within QUOTA_HOLD."""
    return any(now - hit < QUOTA_HOLD for hit in quota_hits)


def clear_auto(state_dir: Path, now: float, quota_hits: list[float]) -> list[str]:
    """Clear old auto pauses, retaining host cooldowns until their deadline."""
    cleared: list[str] = []
    for pause in [*state_dir.glob("PAUSE-BACKEND-*"), *state_dir.glob("PAUSE-HOST-*")]:
        if not pause.is_file() or not pause.read_text().startswith("auto:"):
            continue
        body = pause.read_text()
        if pause.name.startswith("PAUSE-HOST-") and body.startswith("auto:cooldown:"):
            match = re.search(r"^auto:cooldown:until=(\S+)", body, re.MULTILINE)
            if match and now < _until_timestamp(match.group(1)):
                continue
        if now - pause.stat().st_mtime < QUOTA_HOLD:
            continue
        if pause.name.startswith("PAUSE-BACKEND-") and scan_quota(quota_hits, now):
            continue
        pause.unlink()
        prefix = "PAUSE-BACKEND-" if pause.name.startswith("PAUSE-BACKEND-") else "PAUSE-HOST-"
        cleared.append(pause.name.removeprefix(prefix))
    return cleared


def dispatchable(state_dir: Path, backend: str) -> bool:
    """The pause-file gate: once removed, the same backend can be dispatched."""
    return not (state_dir / f"PAUSE-BACKEND-{backend}").exists()


def reconcile_mem_health(
    state_dir: Path,
    previous: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
    host_lanes: dict[str, list[str]],
) -> list[str]:
    """Set hard memory pauses, or self-heal them after a soft-level recovery."""
    changed: list[str] = []
    for host, health in current.items():
        available = health["mem_avail_mb"]
        lanes = host_lanes.get(host, [])
        host_pause = state_dir / f"PAUSE-HOST-{host}"
        lane_pauses = [state_dir / f"PAUSE-{lane}" for lane in lanes]
        if available < HEALTH_MEM_HARD_MB:
            host_pause.write_text(f"auto:host_hard mem_avail_mb={available}\n")
            for pause in lane_pauses:
                pause.write_text(f"auto:mem_hard host={host} mem_avail_mb={available}\n")
            changed.extend([host_pause.name, *(pause.name for pause in lane_pauses)])
            continue

        was_hard = previous.get(host, {}).get("mem_avail_mb", HEALTH_MEM_SOFT_MB) < HEALTH_MEM_HARD_MB
        if was_hard and available >= HEALTH_MEM_SOFT_MB:
            for pause in [host_pause, *lane_pauses]:
                if pause.is_file() and pause.read_text().startswith(("auto:host_hard", "auto:mem_hard")):
                    pause.unlink()
                    changed.append(pause.name)
    return changed


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

        cooldown_pause = state_dir / "PAUSE-HOST-ib1"
        until = now + 600
        assert set_host_cooldown(
            state_dir, "ib1", f"machine_cooldown until={until}"
        )
        assert cooldown_until(f"SSH: cooling down until {until}") == str(until)
        assert cooldown_pause.read_text() == f"auto:cooldown:until={until}\n"
        os.utime(cooldown_pause, (old, old))
        assert clear_auto(state_dir, now, []) == [], "cooldown host must be held until deadline"
        assert cooldown_pause.exists()
        assert clear_auto(state_dir, until + 1, []) == ["ib1"]
        assert not cooldown_pause.exists()

        manual_pause = state_dir / "PAUSE-BACKEND-pi"
        manual_pause.write_text("operator hold\n")
        os.utime(manual_pause, (old, old))
        assert clear_auto(state_dir, now, []) == []
        assert not dispatchable(state_dir, "pi"), "manual pause must remain a gate"

        # Fake health.json snapshots for a govpn2-like host. This contract only
        # touches the temporary fixture, never the live autodispatch directory.
        host_lanes = {"govpn2": ["CX2", "CX4"]}
        low = {"govpn2": {"mem_avail_mb": HEALTH_MEM_HARD_MB - 1}}
        recovered = {"govpn2": {"mem_avail_mb": HEALTH_MEM_SOFT_MB}}
        assert reconcile_mem_health(state_dir, {}, low, host_lanes) == [
            "PAUSE-HOST-govpn2", "PAUSE-CX2", "PAUSE-CX4"
        ]
        assert (state_dir / "PAUSE-HOST-govpn2").read_text().startswith("auto:host_hard")
        assert (state_dir / "PAUSE-CX2").read_text().startswith("auto:mem_hard")
        assert reconcile_mem_health(state_dir, low, recovered, host_lanes) == [
            "PAUSE-HOST-govpn2", "PAUSE-CX2", "PAUSE-CX4"
        ]
        assert not (state_dir / "PAUSE-HOST-govpn2").exists()
        assert not (state_dir / "PAUSE-CX2").exists()
        assert not (state_dir / "PAUSE-CX4").exists()

        manual_lane_pause = state_dir / "PAUSE-CX2"
        manual_lane_pause.write_text("operator hold\n")
        reconcile_mem_health(state_dir, low, recovered, host_lanes)
        assert manual_lane_pause.exists(), "manual same-host lane pause must remain"

        print("OK: old auto pause clears after QUOTA_HOLD with no fresh quota hit")
        print("OK: SSH machine_cooldown writes PAUSE-HOST-* and holds until its deadline")
        print("OK: manual PAUSE-BACKEND pause is never auto-cleared")
        print("OK: cleared backend is dispatchable again")
        print("OK: govpn2 mem_hard sets host and same-host lane pauses")
        print("OK: mem_hard recovery to soft clears only auto pauses")


if __name__ == "__main__":
    main()
