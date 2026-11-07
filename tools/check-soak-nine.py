#!/usr/bin/env python3
"""Offline #1642 contract for busy lanes during node-1.example.invalid/node-2.example.invalid cooldown."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any


PAUSED_LANES = frozenset({"M1", "M2"})
PAUSED_HOSTS = frozenset({"node-1.example.invalid", "node-2.example.invalid"})
ALLOWED_PAUSES = frozenset(
    {"PAUSE-HOST-node-1.example.invalid", "PAUSE-HOST-node-2.example.invalid", "PAUSE-M1", "PAUSE-M2"}
)
BUSY_LIMIT = 9


def check(state_dir: Path, status: dict[str, Any]) -> None:
    """Check fake cooldown pauses and their matching lane status snapshot."""
    pauses = {
        entry.name
        for entry in state_dir.iterdir()
        if entry.is_file() and entry.name.startswith("PAUSE")
    }
    assert pauses == ALLOWED_PAUSES, (
        f"cooldown pause set mismatch: expected {sorted(ALLOWED_PAUSES)}, "
        f"got {sorted(pauses)}"
    )

    for host, lane in zip(sorted(PAUSED_HOSTS), sorted(PAUSED_LANES)):
        assert f"PAUSE-HOST-{host}" in pauses and f"PAUSE-{lane}" in pauses
        assert status.get(lane) == "paused", (
            f"{lane} maps to cooling host {host} and must be paused, "
            f"got {status.get(lane)!r}"
        )

    busy_lanes = [
        key
        for key, value in status.items()
        if not key.startswith("_") and isinstance(value, str) and value.startswith("busy")
    ]
    assert status.get("_busy") == len(busy_lanes), (
        f"_busy={status.get('_busy')} but {len(busy_lanes)} lane(s) are busy"
    )
    assert len(busy_lanes) <= BUSY_LIMIT, (
        f"cooldown busy limit is {BUSY_LIMIT}, got {len(busy_lanes)}"
    )


def main() -> None:
    # Use only fake state and a fake lanes-status snapshot; never read live state.
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as fixture:
        state_dir = Path(fixture)
        for name in ALLOWED_PAUSES:
            (state_dir / name).touch()

        status = {
            **{f"L{index}": "busy #1642" for index in range(1, 10)},
            "M1": "paused",
            "M2": "paused",
            "_busy": 9,
        }
        check(state_dir, status)
        print("OK: node-1.example.invalid/node-2.example.invalid cooldown pauses M1/M2; nine other lanes may be busy")

        invalid = {**status, "L10": "busy #1642", "_busy": 10}
        try:
            check(state_dir, invalid)
        except AssertionError:
            print("OK: busy count above nine is rejected")
        else:
            raise AssertionError("busy count above nine was accepted")

        invalid = {**status, "M1": "busy #1642", "_busy": 10}
        try:
            check(state_dir, invalid)
        except AssertionError:
            print("OK: busy status on cooldown lane is rejected")
        else:
            raise AssertionError("busy status on cooldown lane was accepted")


if __name__ == "__main__":
    main()
