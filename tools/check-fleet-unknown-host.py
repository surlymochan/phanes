#!/usr/bin/env python3
"""Offline contract for rejecting hosts outside the configured topology (Refs #1183)."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ib_refresh", ROOT / "tools" / "ib-refresh.py")
IB_REFRESH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IB_REFRESH)


def main() -> None:
    topology = [{"name": "ib1"}]
    result = IB_REFRESH._status_for(
        {
            "ok": True,
            "start_lane_autodispatch": True,
            "hosts": [
                {"host": "ib1", "status": "valid", "final_status": "valid"},
                {"host": "mystery", "status": "valid", "final_status": "valid"},
            ],
        },
        topology,
        ROOT / "fleet.json",
        datetime.now(timezone.utc),
        need_ok=1,
    )

    assert result["ok"] is False
    assert result["start_lane_autodispatch"] is False
    fields = dict(field.split("=", 1) for field in result["status_line"].split())
    assert fields["ok"] == "0"
    assert fields["hosts_ok"] == "1"
    assert result["status_line"].split()[1] == "hosts_ok=1"
    assert any(error["host"] == "mystery" and error.get("status") == "unknown" for error in result["errors"])
    assert [row["host"] for row in IB_REFRESH._usable_hosts(result["hosts"], {"ib1"})] == ["ib1"]

    # Unknown hosts with cooldown must not be counted in hosts_ok or skipped_cooldown
    result_cooldown = IB_REFRESH._status_for(
        {
            "ok": True,
            "start_lane_autodispatch": True,
            "hosts": [
                {"host": "ib1", "status": "valid", "final_status": "valid"},
                {"host": "mystery_cd", "status": "cooldown", "final_status": "valid"},
            ],
        },
        topology,
        ROOT / "fleet.json",
        datetime.now(timezone.utc),
        need_ok=1,
    )
    assert result_cooldown["ok"] is False
    cd_fields = dict(field.split("=", 1) for field in result_cooldown["status_line"].split())
    assert cd_fields["ok"] == "0"
    assert cd_fields["hosts_ok"] == "1"
    assert cd_fields["skipped_cooldown"] == "0"
    assert any(error["host"] == "mystery_cd" for error in result_cooldown["errors"])

    print("fleet unknown host topology: OK")


if __name__ == "__main__":
    main()
