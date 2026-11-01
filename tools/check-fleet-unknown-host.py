#!/usr/bin/env python3
"""Offline contract for rejecting hosts outside the configured topology."""

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
    assert result["status_line"].split()[1] == "hosts_ok=1"
    assert any(error["host"] == "mystery" for error in result["errors"])
    assert [row["host"] for row in IB_REFRESH._usable_hosts(result["hosts"], {"ib1"})] == ["ib1"]
    print("fleet unknown host topology: OK")


if __name__ == "__main__":
    main()
