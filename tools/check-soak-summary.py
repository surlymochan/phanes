#!/usr/bin/env python3
"""Offline dual-mode soak summary for supervisory polling.

The fixture mirrors the lane status monitored by the control plane without
reading the live autodispatch directory or requiring SSH/GH credentials.
"""

from __future__ import annotations

import json


def parse_lanes_status(raw: str) -> tuple[int, int]:
    """Extract the aggregate busy/ready counts from a lanes-status snapshot."""
    status = json.loads(raw)
    busy = status.get("_busy")
    ready = status.get("_ready")
    assert isinstance(busy, int) and busy >= 0, f"invalid _busy: {busy!r}"
    assert isinstance(ready, int) and ready >= 0, f"invalid _ready: {ready!r}"
    return busy, ready


def main() -> None:
    # Deliberately fake data: this command must remain safe for laptop/CI use.
    lanes_status = '{"CX1":"busy #992","CX2":"idle","_busy":1,"_ready":2}'

    busy, ready = parse_lanes_status(lanes_status)
    print(f"busy={busy}")
    print(f"ready={ready}")


if __name__ == "__main__":
    main()
