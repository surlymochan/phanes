#!/usr/bin/env python3
"""Offline contract check for the SSH fleet BatchMode probe and hosts_ok rollup (Refs #954).

`tools/ib-refresh.py` probes the fleet with `ssh -o BatchMode=yes` and rolls the probe
result up into the control-plane status row.  A non-zero exit code (`Permission denied`,
`Connection closed`, timeout) must keep the host out of `hosts_ok`; only a zero exit code
may add one.

The check drives the production refresh path with a fake probe runner and a fake
`openssl` that reports a far-future expiry, so it never opens an SSH connection, never
touches a live instabox node and needs no certificate material.
"""

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ib_refresh", ROOT / "tools" / "ib-refresh.py")
IB_REFRESH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IB_REFRESH)

HOSTS = ("ib1", "ib2", "ib3", "ib4", "gocc")
FAKE_OUTPUT = {
    "ib1": (255, "", "ib1: Permission denied (publickey,keyboard-interactive)."),
    "ib2": (255, "", "Connection closed by remote host"),
    "ib3": (124, "", "ssh: connect to host ib3 port 22: Connection timed out"),
    "ib4": (0, "OK\n", ""),
    "gocc": (0, "OK\n", ""),
}
FAKE_OPENSSL = """#!/bin/sh
# Fake `openssl` for the offline contract check: report a far-future expiry so the
# refresh path sees a valid certificate without any certificate material.
for arg in "$@"; do
  case "$arg" in
    -enddate) printf 'notAfter=Jan  1 00:00:00 2035 GMT\\n'; exit 0;;
  esac
done
exit 1
"""


def _fake_openssl(directory):
    """Install a fake `openssl` and return the PATH prefix that selects it."""
    bindir = Path(directory) / "bin"
    bindir.mkdir(exist_ok=True)
    openssl = bindir / "openssl"
    openssl.write_text(FAKE_OPENSSL)
    openssl.chmod(0o755)
    return str(bindir)


def main() -> None:
    with tempfile.TemporaryDirectory(dir=ROOT, prefix="fleet-batchmode-") as fixture:
        root = Path(fixture)
        bindir = _fake_openssl(root)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = bindir + os.pathsep + old_path
        try:
            fleet_hosts = []
            for name in HOSTS:
                cert = root / f"{name}.crt"
                cert.write_text("fake certificate\n")
                fleet_hosts.append({"name": name, "cert": str(cert)})
            ssh_config = root / "ssh_config"
            ssh_config.write_text("Host ib1 ib2 ib3 ib4 gocc\n    User openswarm\n")
            fleet = root / "fleet.json"
            fleet.write_text(json.dumps({
                "need_ok": 2,
                "ssh_probe": {"enabled": True, "ssh_config": str(ssh_config)},
                "hosts": fleet_hosts,
            }))

            commands = []

            def fake_runner(argv):
                commands.append(list(argv))
                host = next((name for name in FAKE_OUTPUT if name in argv), None)
                assert host is not None, f"unexpected probe target: {argv}"
                return FAKE_OUTPUT[host]

            result = IB_REFRESH.refresh(fleet, "false", 86400, probe_runner=fake_runner)
            assert result["renewed"] == [], "a valid certificate must not be renewed"
            assert result["ssh_probe"] == "enabled"
            rows = {row["host"]: row for row in result["hosts"]}
            assert rows["ib1"]["probe_exit_code"] == 255
            assert rows["ib1"]["probe_status"] == "failed"
            assert "Permission denied" in rows["ib1"]["probe_error"]
            assert rows["ib2"]["probe_status"] == "failed"
            assert rows["ib3"]["probe_exit_code"] == 124
            assert rows["ib4"]["probe_status"] == "ok"
            assert rows["gocc"]["probe_status"] == "ok"

            fields = dict(field.split("=", 1) for field in result["status_line"].split())
            assert fields["hosts_ok"] == "2", result["status_line"]
            assert fields["ok"] == "1", result["status_line"]

            usable = [row["host"] for row in IB_REFRESH._usable_hosts(result["hosts"], set(HOSTS))]
            assert usable == ["ib4", "gocc"], usable

            assert len(commands) == len(HOSTS), commands
            for argv in commands:
                assert IB_REFRESH.BATCHMODE_OPTION in argv, argv
                assert "-F" in argv and str(ssh_config) in argv, argv

            # A fleet whose probes all fail has no usable capacity at all.
            def failing_runner(argv):
                commands.append(list(argv))
                return 255, "", "Permission denied (publickey)"

            failed = IB_REFRESH.refresh(fleet, "false", 86400, probe_runner=failing_runner)
            failed_fields = dict(field.split("=", 1) for field in failed["status_line"].split())
            assert failed_fields["hosts_ok"] == "0", failed["status_line"]
            assert failed_fields["ok"] == "0", failed["status_line"]
            assert failed["start_lane_autodispatch"] is False

            # The production rollup itself must ignore a failed probe.
            rolled = IB_REFRESH._status_for(
                {"ok": True, "start_lane_autodispatch": True, "hosts": [
                    {"host": "ib1", "status": "valid", "final_status": "valid",
                     "probe_exit_code": 255, "probe_status": "failed"},
                    {"host": "ib2", "status": "valid", "final_status": "valid",
                     "probe_exit_code": 0, "probe_status": "ok"},
                ]},
                [{"name": "ib1"}, {"name": "ib2"}],
                ROOT / "fleet.json",
                dt.datetime.now(dt.timezone.utc),
                need_ok=1,
            )
            assert rolled["status_line"].split()[1] == "hosts_ok=1", rolled["status_line"]
        finally:
            os.environ["PATH"] = old_path

    print("fleet batchmode probe rollup: OK")


if __name__ == "__main__":
    main()
