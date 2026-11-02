#!/usr/bin/env python3
"""Offline dual-mode contract: ib-refresh.status hosts_ok vs reachable lanes (Refs #1637).

The control plane boots from two facts that must agree before dispatch:

* ``~/phanes-ctl/ib-refresh.status`` — the stable row
  ``ok=... hosts_ok=... skipped_cooldown=... min_ttl=... min_exp=...``
  (field order is fixed; see ``STATUS_KEYS`` in ``tools/ib-refresh.py``).
  ``hosts_ok`` counts only hosts with a valid certificate, no active
  cooldown, and a zero-exit BatchMode probe (Refs #954, #948).
* ``lanes.json`` — ``{"lanes": [{"name", "host", "backend", ...}]}``.  The
  ``host`` field is the one ``fleet_health_lane_hosts`` reads; a lane with
  no host cannot be attributed to any usable host.

The mapping asserted here:

* a lane is **reachable** exactly when its host is in ``hosts_ok`` and the
  lane is not drained (``drain: true`` refuses new work);
* a lane on a probe-failed, expired, cooldown, or topology-unknown host is
  never reachable, even if its host name appears in ``lanes.json``;
* a lane on an ok host that is drained is not reachable either;
* when ``hosts_ok`` is 0 the reachable set is empty and the boot gate
  (``ok=1`` / ``start_lane_autodispatch``) is closed.

Everything runs on fixtures inside a temporary directory: the status row is
produced by driving the production ``tools/ib-refresh.py`` refresh path with
a fake probe runner and a fake ``openssl`` that reports a far-future expiry,
so no SSH connection is opened, no live instabox node is touched, and no
certificate material is needed.  The real ``~/phanes-ctl`` tree is never
read or written.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ib_refresh", ROOT / "tools" / "ib-refresh.py")
IB_REFRESH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IB_REFRESH)

STATUS_KEYS = ("ok", "hosts_ok", "skipped_cooldown", "min_ttl", "min_exp")

# Probe outcomes for the fake fleet: two refusals, one timeout, two ok hosts.
# ib5 is valid but held by an active PAUSE-HOST cooldown (see below).
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

# Fake lanes.json for the dual-mode fleet: one lane per host, plus the edge
# cases the mapping must reject (drained, unknown host, no host at all).
LANES = [
    {"name": "PI1", "host": "ib1", "backend": "pi"},
    {"name": "PI2", "host": "ib2", "backend": "pi"},
    {"name": "PI3", "host": "ib3", "backend": "pi"},
    {"name": "PI4", "host": "ib4", "backend": "pi"},
    {"name": "PI4D", "host": "ib4", "backend": "pi", "drain": True},
    {"name": "PI5", "host": "ib5", "backend": "pi"},
    {"name": "AG1", "host": "gocc", "backend": "agy"},
    {"name": "CX1", "host": "govpn", "backend": "codex"},
    {"name": "NO1", "backend": "pi"},
]
FLEET_HOSTS = ("ib1", "ib2", "ib3", "ib4", "ib5", "gocc")


def parse_status_row(text: str) -> dict[str, int]:
    """Parse one ib-refresh.status row, enforcing the fixed field order."""
    fields = text.strip().split()
    keys = []
    values: dict[str, int] = {}
    for field in fields:
        key, _, value = field.partition("=")
        if not key or not value:
            raise ValueError(f"malformed status field {field!r} in {text!r}")
        keys.append(key)
        try:
            values[key] = int(value)
        except ValueError as exc:
            raise ValueError(f"non-integer status value {field!r} in {text!r}") from exc
    if tuple(keys) != STATUS_KEYS:
        raise ValueError(f"status field order must be {STATUS_KEYS}, got {tuple(keys)}")
    return values


def lane_reachability(lanes: list[dict], hosts_ok: set[str],
                      known_hosts: set[str]) -> tuple[list[str], dict[str, str]]:
    """Split lanes into reachable names and blocked names with reasons.

    Precedence: a lane with no host cannot be attributed; a lane on a host
    outside the fleet topology is unknown even before its probe is considered;
    a drained lane refuses new work even on an ok host; anything else whose
    host is not in ``hosts_ok`` is unreachable because that host failed its
    certificate, cooldown, or BatchMode probe gate.
    """
    reachable: list[str] = []
    blocked: dict[str, str] = {}
    for lane in lanes:
        name = lane["name"]
        host = lane.get("host")
        if not host:
            blocked[name] = "no host"
        elif host not in known_hosts:
            blocked[name] = "unknown host"
        elif lane.get("drain") is True:
            blocked[name] = "drained"
        elif host not in hosts_ok:
            blocked[name] = "host not in hosts_ok"
        else:
            reachable.append(name)
    return reachable, blocked


def _fake_openssl(directory: Path) -> str:
    """Install a fake `openssl` and return the PATH prefix that selects it."""
    bindir = directory / "bin"
    bindir.mkdir(exist_ok=True)
    openssl = bindir / "openssl"
    openssl.write_text(FAKE_OPENSSL)
    openssl.chmod(0o755)
    return str(bindir)


def main() -> None:
    with tempfile.TemporaryDirectory(dir=ROOT, prefix="dual-hosts-ok-") as fixture:
        root = Path(fixture)
        bindir = _fake_openssl(root)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = bindir + os.pathsep + old_path
        try:
            fleet_hosts = []
            for name in FLEET_HOSTS:
                cert = root / f"{name}.crt"
                cert.write_text("fake certificate\n")
                fleet_hosts.append({"name": name, "cert": str(cert)})
            ssh_config = root / "ssh_config"
            ssh_config.write_text("Host ib1 ib2 ib3 ib4 ib5 gocc\n    User openswarm\n")
            fleet = root / "fleet.json"
            fleet.write_text(json.dumps({
                "need_ok": 2,
                "ssh_probe": {"enabled": True, "ssh_config": str(ssh_config)},
                "hosts": fleet_hosts,
            }))
            # ib5 is valid but cooling: an active auto cooldown until 2035.
            pause_dir = root / "autodispatch"
            pause_dir.mkdir()
            (pause_dir / "PAUSE-HOST-ib5").write_text(
                "auto:cooldown:until=2035-01-01T00:00:00Z\n")

            commands = []

            def fake_runner(argv):
                commands.append(list(argv))
                host = next((name for name in FAKE_OUTPUT if name in argv), None)
                if host is None:
                    # A cooldown host is never probed by the production path.
                    raise AssertionError(f"unexpected probe target: {argv}")
                return FAKE_OUTPUT[host]

            result = IB_REFRESH.refresh(fleet, "false", 86400, pause_dir=pause_dir,
                                        probe_runner=fake_runner)
            assert result["ssh_probe"] == "enabled"
            fields = parse_status_row(result["status_line"])
            assert fields["hosts_ok"] == 2, result["status_line"]
            assert fields["skipped_cooldown"] == 1, result["status_line"]
            assert fields["ok"] == 1, result["status_line"]
            assert result["start_lane_autodispatch"] is True
            assert len(commands) == len(FAKE_OUTPUT), commands
            for argv in commands:
                assert IB_REFRESH.BATCHMODE_OPTION in argv, argv

            usable = [row["host"] for row in
                      IB_REFRESH._usable_hosts(result["hosts"], set(FLEET_HOSTS))]
            assert usable == ["ib4", "gocc"], usable
            assert fields["hosts_ok"] == len(usable), "status count must match usable hosts"

            # The fake lanes.json the mapping is asserted against.
            lanes_path = root / "lanes.json"
            lanes_path.write_text(json.dumps({"lanes": LANES}, indent=1) + "\n")
            lanes = json.loads(lanes_path.read_text())["lanes"]
            reachable, blocked = lane_reachability(lanes, set(usable), set(FLEET_HOSTS))
            assert reachable == ["PI4", "AG1"], reachable
            assert blocked == {
                "PI1": "host not in hosts_ok",
                "PI2": "host not in hosts_ok",
                "PI3": "host not in hosts_ok",
                "PI4D": "drained",
                "PI5": "host not in hosts_ok",
                "CX1": "unknown host",
                "NO1": "no host",
            }, blocked
            print("OK: reachable lanes are exactly the lanes on hosts_ok hosts (PI4, AG1)")

            # A shuffled status row must not parse: the field order is fixed.
            shuffled = "hosts_ok=2 ok=1 skipped_cooldown=1 min_ttl=0 min_exp=0"
            try:
                parse_status_row(shuffled)
            except ValueError:
                pass
            else:
                raise AssertionError("a shuffled status row must be rejected")
            print("OK: a shuffled ib-refresh.status row is rejected (field order is fixed)")

            # A fleet whose probes all fail has no usable capacity and no
            # reachable lane, even with the same lanes.json.
            def failing_runner(argv):
                return 255, "", "Permission denied (publickey)"

            failed = IB_REFRESH.refresh(fleet, "false", 86400, pause_dir=pause_dir,
                                        probe_runner=failing_runner)
            failed_fields = parse_status_row(failed["status_line"])
            assert failed_fields["hosts_ok"] == 0, failed["status_line"]
            assert failed_fields["ok"] == 0, failed["status_line"]
            assert failed["start_lane_autodispatch"] is False
            failed_usable = [row["host"] for row in
                             IB_REFRESH._usable_hosts(failed["hosts"], set(FLEET_HOSTS))]
            assert failed_usable == [], failed_usable
            failed_reachable, _ = lane_reachability(lanes, set(failed_usable),
                                                    set(FLEET_HOSTS))
            assert failed_reachable == [], failed_reachable
            print("OK: with hosts_ok=0 no lane is reachable and the boot gate is closed")
        finally:
            os.environ["PATH"] = old_path

    print("dual hosts_ok to reachable lanes: OK")


if __name__ == "__main__":
    main()
