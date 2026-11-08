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
    names = ["gores1", "gores2", "gocc", "gorelay", "govpn", "govpn2"]
    hosts = [{"name": name, "cert": f"certs/{name}.crt"} for name in names]
    rows = [
        # A cooldown host may still have a valid certificate; it is not reachable
        # for this refresh and must remain outside hosts_ok.
        {"host": "gores1", "status": "cooldown", "final_status": "valid"},
        {"host": "gores2", "status": "skipped_cooldown"},
        {"host": "gocc", "status": "valid", "final_status": "valid"},
        {"host": "gorelay", "status": "valid", "final_status": "valid"},
        {"host": "govpn", "status": "valid", "final_status": "valid"},
        {"host": "govpn2", "status": "valid", "final_status": "valid"},
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
        "gocc", "gorelay", "govpn", "govpn2"
    ]
    fields = dict(field.split("=", 1) for field in result["status_line"].split())
    assert fields["ok"] == "1"
    assert fields["hosts_ok"] == "4"
    assert fields["skipped_cooldown"] == "2"
    print("ib-refresh cooldown status: OK")


if __name__ == "__main__":
    main()
