#!/usr/bin/env python3
"""Offline contract check for the ib-refresh machine-readable status row.

Deprecated (Refs #1480): the Instabox nodes are offline since 2026-11-02, so this
check only keeps the historical instabox refresh contract alive.  It no longer
describes the live fleet topology (see docs/fleet-topology-expected.md).
"""

from __future__ import annotations


EXPECTED_KEYS = ("ok", "hosts_ok", "skipped_cooldown", "min_ttl", "min_exp")


def parse_status_line(line: str) -> dict[str, int]:
    fields = line.strip().split()
    keys = tuple(field.partition("=")[0] for field in fields)
    assert keys == EXPECTED_KEYS, f"status keys/order changed: {keys!r}"
    parsed = {}
    for field in fields:
        key, separator, value = field.partition("=")
        assert separator and value, f"malformed status field: {field!r}"
        parsed[key] = int(value)
    return parsed


def main() -> None:
    sample = "ok=1 hosts_ok=2 skipped_cooldown=1 min_ttl=1800 min_exp=1735689600"
    status = parse_status_line(sample)
    assert tuple(status) == EXPECTED_KEYS
    assert status["ok"] == 1
    assert status["hosts_ok"] == 2
    assert status["skipped_cooldown"] >= 1
    assert status["min_ttl"] >= 0
    assert status["min_exp"] > 0
    print("ib-refresh status line: OK")
    print("deprecated: instabox nodes are offline; this contract is history only (Refs #1480)")


if __name__ == "__main__":
    main()
