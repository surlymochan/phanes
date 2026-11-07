#!/usr/bin/env python3
"""Offline contract for ib-refresh's partial-fleet readiness gate."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ib_refresh", ROOT / "tools" / "ib-refresh.py")
IB_REFRESH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IB_REFRESH)


def main():
    # node-1.example.invalid/node-2.example.invalid are in cooldown; only the reachable node-3.example.invalid-node-5.example.invalid rows are written.
    rows = [
        {"host": "node-1.example.invalid", "status": "cooldown"},
        {"host": "node-2.example.invalid", "status": "skipped_cooldown"},
        {"host": "node-3.example.invalid", "status": "valid", "final_status": "valid"},
        {"host": "node-4.example.invalid", "status": "valid", "final_status": "valid"},
        {"host": "node-5.example.invalid", "status": "valid", "final_status": "valid"},
    ]
    usable = IB_REFRESH._usable_hosts(rows)
    assert [row["host"] for row in usable] == ["node-3.example.invalid", "node-4.example.invalid", "node-5.example.invalid"]
    assert IB_REFRESH._need_ok(len(usable), 2, 3)
    assert IB_REFRESH._status_line(True, len(usable), 2, 1800, 1735689600).startswith(
        "ok=1 hosts_ok=3 skipped_cooldown=2"
    )
    print("ib-refresh need_ok partial fleet: OK")


if __name__ == "__main__":
    main()
