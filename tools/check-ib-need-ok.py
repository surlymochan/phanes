#!/usr/bin/env python3
"""Offline contract for ib-refresh's partial-fleet readiness gate.

Deprecated (Refs #1480): the Instabox nodes are offline since 2026-11-02, so the
host names below are historical fixtures only.  The live fleet topology lives in
docs/fleet-topology-expected.md.
"""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ib_refresh", ROOT / "tools" / "ib-refresh.py")
IB_REFRESH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IB_REFRESH)


def main():
    # ib1/ib2 are in cooldown; only the reachable ib3-ib5 rows are written.
    rows = [
        {"host": "ib1", "status": "cooldown"},
        {"host": "ib2", "status": "skipped_cooldown"},
        {"host": "ib3", "status": "valid", "final_status": "valid"},
        {"host": "ib4", "status": "valid", "final_status": "valid"},
        {"host": "ib5", "status": "valid", "final_status": "valid"},
    ]
    usable = IB_REFRESH._usable_hosts(rows)
    assert [row["host"] for row in usable] == ["ib3", "ib4", "ib5"]
    assert IB_REFRESH._need_ok(len(usable), 2, 3)
    assert IB_REFRESH._status_line(True, len(usable), 2, 1800, 1735689600).startswith(
        "ok=1 hosts_ok=3 skipped_cooldown=2"
    )
    print("ib-refresh need_ok partial fleet: OK")
    print("deprecated: instabox nodes are offline; this contract is history only (Refs #1480)")


if __name__ == "__main__":
    main()
