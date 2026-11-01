#!/usr/bin/env python3
"""Offline dual-mode soak summary for supervisory polling.

The fixture mirrors the two files monitored by the control plane without
reading the live autodispatch directory or requiring SSH/GH credentials.
"""

from __future__ import annotations

import json


IB_STATUS_KEYS = ("ok", "hosts_ok", "skipped_cooldown", "min_ttl", "min_exp")


def parse_lanes_status(raw: str) -> tuple[int, int]:
    """Extract the aggregate busy/ready counts from a lanes-status snapshot."""
    status = json.loads(raw)
    busy = status.get("_busy")
    ready = status.get("_ready")
    assert isinstance(busy, int) and busy >= 0, f"invalid _busy: {busy!r}"
    assert isinstance(ready, int) and ready >= 0, f"invalid _ready: {ready!r}"
    return busy, ready


def parse_ib_status(raw: str) -> dict[str, int]:
    """Parse the fixed-order machine-readable ib-refresh status row."""
    fields = raw.strip().split()
    keys = tuple(field.partition("=")[0] for field in fields)
    assert keys == IB_STATUS_KEYS, f"status keys/order changed: {keys!r}"
    parsed: dict[str, int] = {}
    for field in fields:
        key, separator, value = field.partition("=")
        assert separator and value, f"malformed status field: {field!r}"
        parsed[key] = int(value)
    assert parsed["hosts_ok"] >= 0
    assert parsed["min_exp"] > 0
    return parsed


def main() -> None:
    # Deliberately fake data: this command must remain safe for laptop/CI use.
    lanes_status = '{"CX1":"busy #992","CX2":"idle","_busy":1,"_ready":2}'
    ib_status = "ok=1 hosts_ok=2 skipped_cooldown=0 min_ttl=1800 min_exp=1793520000"

    busy, ready = parse_lanes_status(lanes_status)
    ib = parse_ib_status(ib_status)
    assert ib["ok"] == 1
    print(f"busy={busy}")
    print(f"ready={ready}")
    print(f"hosts_ok={ib['hosts_ok']}")
    print(f"min_exp={ib['min_exp']}")


if __name__ == "__main__":
    main()
