#!/usr/bin/env python3
"""Offline contract for rejecting hosts outside the configured topology (Refs #1164).

The expected fleet hosts come from `docs/fleet-topology-expected.md` (the single
source of truth), so this check follows the documented topology instead of a second
copy of it.  The Instabox nodes are retired since 2026-11-02 (Refs #1480).
"""

import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY_DOC = ROOT / "docs" / "fleet-topology-expected.md"
SPEC = importlib.util.spec_from_file_location("ib_refresh", ROOT / "tools" / "ib-refresh.py")
IB_REFRESH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IB_REFRESH)


def documented_hosts():
    """Return the fleet hosts documented in docs/fleet-topology-expected.md."""
    text = TOPOLOGY_DOC.read_text(encoding="utf-8")
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        raise AssertionError(f"{TOPOLOGY_DOC} must contain a machine-readable topology block")
    hosts = json.loads(match.group(1))["hosts"]
    assert isinstance(hosts, list) and hosts, f"{TOPOLOGY_DOC} topology block has no hosts"
    return hosts


def main() -> None:
    known = documented_hosts()
    sample = known[0]
    topology = [{"name": sample}]
    result = IB_REFRESH._status_for(
        {
            "ok": True,
            "start_lane_autodispatch": True,
            "hosts": [
                {"host": sample, "status": "valid", "final_status": "valid"},
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
    assert [row["host"] for row in IB_REFRESH._usable_hosts(result["hosts"], {sample})] == [sample]

    # Unknown hosts with cooldown must not be counted in hosts_ok or skipped_cooldown
    result_cooldown = IB_REFRESH._status_for(
        {
            "ok": True,
            "start_lane_autodispatch": True,
            "hosts": [
                {"host": sample, "status": "valid", "final_status": "valid"},
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

    # Unknown-only hosts must yield hosts_ok=0 and fail validation
    result_unknown_only = IB_REFRESH._status_for(
        {
            "ok": True,
            "start_lane_autodispatch": True,
            "hosts": [
                {"host": "mystery_only", "status": "valid", "final_status": "valid"},
            ],
        },
        topology,
        ROOT / "fleet.json",
        datetime.now(timezone.utc),
        need_ok=1,
    )
    assert result_unknown_only["ok"] is False
    assert result_unknown_only["start_lane_autodispatch"] is False
    unk_fields = dict(field.split("=", 1) for field in result_unknown_only["status_line"].split())
    assert unk_fields["ok"] == "0"
    assert unk_fields["hosts_ok"] == "0"
    assert any(error["host"] == "mystery_only" and error.get("status") == "unknown" for error in result_unknown_only["errors"])

    print(f"fleet unknown host topology: OK (documented hosts: {', '.join(known)})")


if __name__ == "__main__":
    main()
