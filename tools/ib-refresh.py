#!/usr/bin/env python3
"""Renew fleet SSH certificates before the control plane starts dispatching."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

STATUS_KEYS = ("ok", "hosts_ok", "skipped_cooldown", "min_ttl", "min_exp")
COOLDOWN_STATUS = "cooldown"


def _status_line(ok, hosts_ok, skipped_cooldown, min_ttl, min_exp):
    """Return the stable, whitespace-delimited control-plane status row."""
    values = (int(bool(ok)), int(hosts_ok), int(skipped_cooldown), int(min_ttl), int(min_exp))
    return " ".join(f"{key}={value}" for key, value in zip(STATUS_KEYS, values))


def _status_for(result, hosts, config_path, now):
    rows = result.get("hosts", [])
    hosts_ok = sum(row.get("final_status", row.get("status")) == "valid" for row in rows)
    skipped = sum(row.get("status") in ("valid", COOLDOWN_STATUS) for row in rows)
    expiries = []
    for host in hosts:
        cert_name = host.get("cert") or host.get("certificate") or host.get("ssh_cert")
        if cert_name:
            expiry = _expiry(_cert_path(config_path, cert_name))
            if expiry is not None:
                expiries.append(expiry)
    min_expiry = min(expiries) if expiries else None
    min_exp = int(min_expiry.timestamp()) if min_expiry else -1
    min_ttl = max(0, int((min_expiry - now).total_seconds())) if min_expiry else -1
    result["status_line"] = _status_line(result["ok"], hosts_ok, skipped, min_ttl, min_exp)
    return result


DEFAULT_CERT_REUSE_TTL = 1500
DEFAULT_NEED_OK = 0


def cert_reuse_ttl(value=None):
    """Return the reuse window, allowing operators to force earlier renewal."""
    raw = (os.environ.get("IB_CERT_REUSE_TTL", str(DEFAULT_CERT_REUSE_TTL))
           if value is None else value)
    try:
        ttl = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("IB_CERT_REUSE_TTL must be an integer") from exc
    if ttl < 0:
        raise ValueError("IB_CERT_REUSE_TTL must be non-negative")
    return ttl


def need_renew(ttl, reuse_ttl=None):
    """Renew only when the remaining TTL is below the configured reuse window."""
    return ttl < cert_reuse_ttl(reuse_ttl)


def need_ok(value=None):
    """Return the number of valid hosts required for a successful run.

    `0` keeps the strict rule: every host of the fleet config must hold a
    valid certificate.  A positive value is the floor of reachable hosts that
    is enough to start dispatching on them.
    """
    raw = (os.environ.get("IB_NEED_OK", str(DEFAULT_NEED_OK))
           if value is None else value)
    try:
        count = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("IB_NEED_OK must be an integer") from exc
    if count < 0:
        raise ValueError("IB_NEED_OK must be non-negative")
    return count


def _is_cooldown(host):
    """Return True when a fleet entry is parked in cooldown."""
    return bool(host.get("cooldown"))


def _scoped_hosts(hosts, names):
    """Return only the fleet entries whose host name is in `names`."""
    return [host for host in hosts if host.get("name") in names]


def _scoped_config(config, names):
    """Return the fleet config that only keeps the reachable hosts."""
    scoped = dict(config)
    scoped["hosts"] = _scoped_hosts(config.get("hosts", []), names)
    return scoped


def _expiry(cert):
    result = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-enddate"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        return None
    value = result.stdout.strip().removeprefix("notAfter=")
    try:
        return dt.datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _classify(expiry, now, threshold):
    """Classify a certificate expiry against the reuse threshold."""
    if expiry is None:
        return "expired"
    remaining = (expiry - now).total_seconds()
    if remaining <= 0:
        return "expired"
    if need_renew(remaining, threshold):
        return "expiring"
    return "valid"


def _state(cert, now, threshold):
    if not cert.exists():
        return "missing"
    return _classify(_expiry(cert), now, threshold)


def _cert_path(config_path, value):
    path = Path(value)
    return path if path.is_absolute() else config_path.parent / path


def refresh(config_path, renew_command, threshold, output_path=None, min_ok=None):
    config_path = Path(config_path)
    output_path = Path(output_path or config_path)
    floor = need_ok() if min_ok is None else int(min_ok)
    try:
        config = json.loads(config_path.read_text())
        hosts = config["hosts"]
        if not isinstance(hosts, list):
            raise ValueError("fleet config hosts must be a list")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return {"ok": False, "start_lane_autodispatch": False, "need_ok": floor,
                "error": f"fleet config: {exc}",
                "status_line": _status_line(False, 0, 0, -1, -1)}

    now = dt.datetime.now(dt.timezone.utc)
    rows, pending = [], []
    config_errors = []
    for host in hosts:
        name = host.get("name")
        if name and _is_cooldown(host):
            # A host in cooldown is parked on purpose: it is not renewed, it
            # is not reachable, and it is left out of the written fleet config.
            rows.append({"host": name, "status": COOLDOWN_STATUS})
            continue
        cert_name = host.get("cert") or host.get("certificate") or host.get("ssh_cert")
        if not name or not cert_name:
            rows.append({"host": name, "status": "missing", "error": "certificate path missing"})
            config_errors.append({"host": name, "status": "missing", "error": "certificate path missing"})
            continue
        cert = _cert_path(config_path, cert_name)
        state = _state(cert, now, threshold)
        rows.append({"host": name, "status": state})
        if state != "valid":
            pending.append((host, cert, state))

    if config_errors:
        return _status_for({"ok": False, "start_lane_autodispatch": False, "need_ok": floor,
                            "hosts": rows, "renewed": [], "errors": config_errors},
                           hosts, config_path, now)

    errors = []
    renewed = []
    # Every replacement is staged below.  A failed renewal therefore cannot
    # leave a half-renewed fleet or a partially updated fleet config.
    with tempfile.TemporaryDirectory(prefix="ib-refresh-", dir=config_path.parent) as staging:
        staged = Path(staging)
        replacements = []
        for host, cert, state in pending:
            name = host.get("name")
            try:
                proc = subprocess.run(
                    shlex.split(renew_command) + [name],
                    capture_output=True, text=False, check=False,
                )
                if proc.returncode or not proc.stdout:
                    detail = proc.stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
                    raise RuntimeError(detail)
                target = staged / f"{len(replacements)}.cert"
                target.write_bytes(proc.stdout)
                expiry = _expiry(target)
                if expiry is None:
                    raise RuntimeError("renew command did not return an X.509 certificate")
                replacements.append((name, cert, target, expiry))
                renewed.append(name)
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append({"host": name, "status": state, "error": str(exc)})

        # Decide before touching the filesystem: the floor is the number of
        # hosts that must hold a valid certificate, every host of the fleet
        # config when no floor is configured.
        planned = {row["host"]: row["status"] for row in rows}
        for name, _cert, _target, expiry in replacements:
            planned[name] = _classify(expiry, now, threshold)
        reachable = [name for name, state in planned.items() if state == "valid"]
        needed = floor if floor > 0 else len(hosts)
        if len(reachable) < needed:
            return _status_for({"ok": False, "start_lane_autodispatch": False, "need_ok": floor,
                                "hosts": rows, "renewed": [], "errors": errors},
                               hosts, config_path, now)

        for _name, cert, staged_cert, _expiry_value in replacements:
            if cert is None:
                continue
            cert.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_cert, cert)

        # Re-read after replacement so the result is useful as a boot gate.
        result_rows = []
        for row in rows:
            if row["status"] == COOLDOWN_STATUS:
                result_rows.append({**row, "final_status": COOLDOWN_STATUS})
                continue
            cert_name = next((h.get("cert") or h.get("certificate") or h.get("ssh_cert")
                              for h in hosts if h.get("name") == row["host"]), None)
            final = _state(_cert_path(config_path, cert_name), now, threshold) if cert_name else "missing"
            result_rows.append({**row, "final_status": final})
        reachable = [row["host"] for row in result_rows if row["final_status"] == "valid"]

        if output_path != config_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            staged_config = staged / "fleet.json"
            staged_config.write_text(json.dumps(_scoped_config(config, reachable), indent=2) + "\n")
            os.replace(staged_config, output_path)

    return _status_for({"ok": True, "start_lane_autodispatch": True, "need_ok": floor,
                        "hosts": result_rows, "renewed": renewed, "errors": errors},
                       _scoped_hosts(hosts, reachable), config_path, now)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fleet-config", required=True)
    parser.add_argument("--renew-command", required=True,
                        help="command receiving the host name and writing a certificate to stdout")
    parser.add_argument("--threshold-seconds", type=int,
                        help="override IB_CERT_REUSE_TTL for this run")
    parser.add_argument("--need-ok", type=int,
                        help="number of valid hosts required for ok=1 "
                             "(override IB_NEED_OK; 0 = every host must be valid)")
    parser.add_argument("--output-fleet-config")
    parser.add_argument("--status-file")
    args = parser.parse_args(argv)
    threshold = cert_reuse_ttl(args.threshold_seconds)
    floor = need_ok(args.need_ok)
    result = refresh(args.fleet_config, args.renew_command, threshold,
                     args.output_fleet_config, floor)
    payload = json.dumps(result, sort_keys=True)
    print(payload)
    if args.status_file:
        Path(args.status_file).write_text(result["status_line"] + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
