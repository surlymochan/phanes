#!/usr/bin/env python3
"""Offline contract for ib-refresh cooldown status accounting (Refs #1262)."""

import datetime as dt
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ib_refresh", ROOT / "tools" / "ib-refresh.py")
IB_REFRESH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IB_REFRESH)


def main():
    hosts = [{"name": f"node-{i}.example.invalid", "cert": f"certs/node-{i}.example.invalid.crt"} for i in range(1, 6)]
    rows = [
        # A cooldown host may still have a valid certificate; it is not reachable
        # for this refresh and must remain outside hosts_ok.
        {"host": "node-1.example.invalid", "status": "cooldown", "final_status": "valid"},
        {"host": "node-2.example.invalid", "status": "skipped_cooldown"},
        {"host": "node-3.example.invalid", "status": "valid", "final_status": "valid"},
        {"host": "node-4.example.invalid", "status": "valid", "final_status": "valid"},
        {"host": "node-5.example.invalid", "status": "valid", "final_status": "valid"},
    ]
    result = IB_REFRESH._status_for(
        {"ok": True, "start_lane_autodispatch": True, "hosts": rows},
        hosts,
        ROOT / "fleet.json",
        dt.datetime.now(dt.timezone.utc),
        need_ok=3,
    )
    assert result["ok"] is True
    assert result["start_lane_autodispatch"] is True
    assert [row["host"] for row in IB_REFRESH._usable_hosts(rows, {host["name"] for host in hosts})] == [
        "node-3.example.invalid", "node-4.example.invalid", "node-5.example.invalid"
    ]
    fields = dict(field.split("=", 1) for field in result["status_line"].split())
    assert fields["ok"] == "1"
    assert fields["hosts_ok"] == "3"
    assert fields["skipped_cooldown"] == "2"
    print("ib-refresh cooldown status: OK")


if __name__ == "__main__":
    main()
