#!/usr/bin/env python3
"""Offline contract check for the ib-refresh `need_ok` floor (Refs #970).

The check drives `tools/ib-refresh.py` with a fake host table (ib1/ib2 cooling
down, ib3-ib5 renewable) and a fake renewal command that only copies a locally
generated certificate.  It never runs the production renewal command, never
touches real credentials and never reads the real fleet config.
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "ib-refresh.py"
SPEC = importlib.util.spec_from_file_location("ib_refresh", MODULE_PATH)
IB_REFRESH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(IB_REFRESH)

HOSTS = ("ib1", "ib2", "ib3", "ib4", "ib5")
COOLDOWN_HOSTS = ("ib1", "ib2")
REACHABLE_HOSTS = ("ib3", "ib4", "ib5")
# The fake short certificate expires below the threshold, the fake renewed one
# does not, so the renewable hosts are renewed and end up valid.
THRESHOLD = 2 * 24 * 3600
OPENSSL_CONFIG = """[ req ]
default_bits = 2048
distinguished_name = req_dn
x509_extensions = v3_ca
prompt = no
[ req_dn ]
CN = fake
[ v3_ca ]
basicConstraints = critical,CA:TRUE
"""


def make_cert(root, name, days):
    """Create a local self-signed certificate; no real credential is involved."""
    cert = root / f"{name}.cert"
    config = root / "openssl.cnf"
    config.write_text(OPENSSL_CONFIG)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(root / f"{name}.key"), "-out", str(cert),
                    "-days", str(days), "-subj", "/CN=fake", "-config", str(config)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert


def make_renewer(root, cert, log):
    """Return a fake renewal command that records its host names."""
    renew = root / "fake-renew.py"
    renew.write_text(
        "import pathlib, sys\n"
        f"with pathlib.Path({str(log)!r}).open('a') as handle:\n"
        "    handle.write(sys.argv[1] + '\\n')\n"
        f"sys.stdout.buffer.write(pathlib.Path({str(cert)!r}).read_bytes())\n"
    )
    return f"{sys.executable} {renew}"


def scenario(base, hosts=HOSTS, cooldown=COOLDOWN_HOSTS, expiring=REACHABLE_HOSTS, valid=()):
    """Build a fake fleet config, its fake certificates and the fake renewer."""
    base.mkdir(parents=True, exist_ok=True)
    renewed_cert = make_cert(base, "renewed", 10)
    expiring_cert = make_cert(base, "expiring", 1)
    log = base / "renew.log"
    renew = make_renewer(base, renewed_cert, log)
    entries = []
    for name in hosts:
        entry = {"name": name, "cert": str(base / f"{name}.cert")}
        if name in cooldown:
            entry["cooldown"] = True
        entries.append(entry)
    config = base / "fleet.json"
    config.write_text(json.dumps({"hosts": entries}, indent=2) + "\n")
    for name in expiring:
        (base / f"{name}.cert").write_bytes(expiring_cert.read_bytes())
    for name in valid:
        (base / f"{name}.cert").write_bytes(renewed_cert.read_bytes())
    return config, renew, log, expiring_cert.read_bytes()


def parse_status(line):
    """Parse the stable status row into integers."""
    fields = dict(field.split("=", 1) for field in line.split())
    return {key: int(value) for key, value in fields.items()}


def main():
    original = os.environ.get("IB_NEED_OK")
    try:
        os.environ.pop("IB_NEED_OK", None)
        scratch_root = ROOT / ".openswarm-tmp"
        scratch_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ib-need-ok-", dir=scratch_root) as tmp:
            root = pathlib.Path(tmp)

            # need_ok=3 with two hosts cooling down: ok=1, and only the
            # reachable machines are written to the output fleet config.
            config, renew, log, _ = scenario(root / "a")
            output = root / "a" / "fleet-out.json"
            result = IB_REFRESH.refresh(config, renew, THRESHOLD, output, 3)
            status = parse_status(result["status_line"])
            assert result["ok"] is True, result
            assert result["start_lane_autodispatch"] is True, result
            assert status["ok"] == 1, status
            assert status["hosts_ok"] == 3, status
            assert status["skipped_cooldown"] == 2, status
            assert status["min_ttl"] > 0, status
            written = json.loads(output.read_text())
            assert [host["name"] for host in written["hosts"]] == list(REACHABLE_HOSTS), written
            assert log.read_text().split() == list(REACHABLE_HOSTS), log.read_text()

            # Without a floor every host of the fleet config must be valid:
            # the run fails, nothing is installed and no config is written.
            config, renew, _log, expiring_bytes = scenario(root / "b")
            output = root / "b" / "fleet-out.json"
            strict = IB_REFRESH.refresh(config, renew, THRESHOLD, output, 0)
            assert strict["ok"] is False, strict
            assert strict["start_lane_autodispatch"] is False, strict
            assert not output.exists(), output
            assert (root / "b" / "ib3.cert").read_bytes() == expiring_bytes

            # A floor above the number of reachable hosts keeps the run failed.
            config, renew, _log, _ = scenario(root / "c")
            output = root / "c" / "fleet-out.json"
            too_high = IB_REFRESH.refresh(config, renew, THRESHOLD, output, 4)
            assert too_high["ok"] is False, too_high
            assert not output.exists(), output

            # Two hosts skip the renewal (their certificate is still valid):
            # hosts_ok=3 and skipped_cooldown=2 with need_ok=3.
            config, renew, _log, _ = scenario(root / "d", hosts=REACHABLE_HOSTS, cooldown=(),
                                               expiring=("ib5",), valid=("ib3", "ib4"))
            output = root / "d" / "fleet-out.json"
            reused = IB_REFRESH.refresh(config, renew, THRESHOLD, output, 3)
            status = parse_status(reused["status_line"])
            assert reused["ok"] is True, reused
            assert status["ok"] == 1, status
            assert status["hosts_ok"] == 3, status
            assert status["skipped_cooldown"] == 2, status
            written = json.loads(output.read_text())
            assert [host["name"] for host in written["hosts"]] == list(REACHABLE_HOSTS), written

            # The command line and the status file carry the same contract.
            config, renew, _log, _ = scenario(root / "e")
            output = root / "e" / "fleet-out.json"
            status_file = root / "e" / "ib-refresh.status"
            proc = subprocess.run(
                [sys.executable, str(MODULE_PATH),
                 "--fleet-config", str(config),
                 "--renew-command", renew,
                 "--threshold-seconds", str(THRESHOLD),
                 "--need-ok", "3",
                 "--output-fleet-config", str(output),
                 "--status-file", str(status_file)],
                capture_output=True, text=True, check=False)
            assert proc.returncode == 0, proc.stderr
            payload = json.loads(proc.stdout)
            assert payload["ok"] is True, payload
            assert payload["need_ok"] == 3, payload
            row = status_file.read_text().strip()
            assert row.startswith("ok=1 hosts_ok=3 skipped_cooldown=2"), row
            written = json.loads(output.read_text())
            assert [host["name"] for host in written["hosts"]] == list(REACHABLE_HOSTS), written
    finally:
        if original is None:
            os.environ.pop("IB_NEED_OK", None)
        else:
            os.environ["IB_NEED_OK"] = original
    print("OK ib-refresh need_ok floor contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
