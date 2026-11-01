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


DEFAULT_CERT_REUSE_TTL = 1500


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


def _state(cert, now, threshold):
    if not cert.exists():
        return "missing"
    expiry = _expiry(cert)
    if expiry is None:
        return "expired"
    remaining = (expiry - now).total_seconds()
    if remaining <= 0:
        return "expired"
    if need_renew(remaining, threshold):
        return "expiring"
    return "valid"


def _cert_path(config_path, value):
    path = Path(value)
    return path if path.is_absolute() else config_path.parent / path


def refresh(config_path, renew_command, threshold, output_path=None):
    config_path = Path(config_path)
    output_path = Path(output_path or config_path)
    try:
        config = json.loads(config_path.read_text())
        hosts = config["hosts"]
        if not isinstance(hosts, list):
            raise ValueError("fleet config hosts must be a list")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return {"ok": False, "start_lane_autodispatch": False, "error": f"fleet config: {exc}"}

    now = dt.datetime.now(dt.timezone.utc)
    rows, pending = [], []
    config_errors = []
    for host in hosts:
        name = host.get("name")
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
        return {"ok": False, "start_lane_autodispatch": False, "hosts": rows,
                "renewed": [], "errors": config_errors}

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
                if _expiry(target) is None:
                    raise RuntimeError("renew command did not return an X.509 certificate")
                replacements.append((cert, target))
                renewed.append(name)
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append({"host": name, "status": state, "error": str(exc)})
                break

        if errors:
            return {"ok": False, "start_lane_autodispatch": False, "hosts": rows,
                    "renewed": [], "errors": errors}

        for cert, staged_cert in replacements:
            if cert is None:
                continue
            cert.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_cert, cert)
        if output_path != config_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            staged_config = staged / "fleet.json"
            staged_config.write_text(json.dumps(config, indent=2) + "\n")
            os.replace(staged_config, output_path)

    # Re-read after replacement so the result is useful as a boot gate.
    result_rows = []
    for row in rows:
        cert_name = next((h.get("cert") or h.get("certificate") or h.get("ssh_cert")
                          for h in hosts if h.get("name") == row["host"]), None)
        final = _state(_cert_path(config_path, cert_name), now, threshold) if cert_name else "missing"
        result_rows.append({**row, "final_status": final})
    return {"ok": True, "start_lane_autodispatch": True, "hosts": result_rows, "renewed": renewed,
            "errors": []}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fleet-config", required=True)
    parser.add_argument("--renew-command", required=True,
                        help="command receiving the host name and writing a certificate to stdout")
    parser.add_argument("--threshold-seconds", type=int,
                        help="override IB_CERT_REUSE_TTL for this run")
    parser.add_argument("--output-fleet-config")
    parser.add_argument("--status-file")
    args = parser.parse_args(argv)
    threshold = cert_reuse_ttl(args.threshold_seconds)
    result = refresh(args.fleet_config, args.renew_command, threshold,
                     args.output_fleet_config)
    payload = json.dumps(result, sort_keys=True)
    print(payload)
    if args.status_file:
        Path(args.status_file).write_text(payload + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
