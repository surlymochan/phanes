#!/usr/bin/env python3
"""Renew fleet SSH certificates before the control plane starts dispatching."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

STATUS_KEYS = ("ok", "hosts_ok", "skipped_cooldown", "min_ttl", "min_exp")
COOLDOWN_RE = re.compile(r"^auto:cooldown:until=(\S+)", re.MULTILINE)
BATCHMODE_OPTION = "BatchMode=yes"
CONNECT_TIMEOUT_OPTION = "ConnectTimeout=5"
PROBE_COMMAND = "true"
PROBE_TIMEOUT_SECONDS = 15
DEFAULT_SSH_CONFIG = "~/.ssh/openswarm-hub/config"


def _status_line(ok, hosts_ok, skipped_cooldown, min_ttl, min_exp):
    """Return the stable, whitespace-delimited control-plane status row."""
    values = (int(bool(ok)), int(hosts_ok), int(skipped_cooldown), int(min_ttl), int(min_exp))
    return " ".join(f"{key}={value}" for key, value in zip(STATUS_KEYS, values))


def _is_cooldown(row):
    return row.get("cooldown") is True or row.get("status") in {"cooldown", "skipped_cooldown"}


def _cooldown_until(pause, now):
    """Return an active ISO-8601 cooldown from a host pause file."""
    try:
        body = pause.read_text()
    except OSError:
        return None
    match = COOLDOWN_RE.search(body)
    if not match:
        return None
    try:
        deadline = dt.datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=dt.timezone.utc)
    return match.group(1) if deadline > now else None


def _cooldown_hosts(pause_dir, now):
    """Read active host cooldowns without modifying the pause directory."""
    if pause_dir is None:
        return {}
    active = {}
    try:
        pauses = pause_dir.glob("PAUSE-HOST-*")
    except OSError:
        return active
    for pause in pauses:
        until = _cooldown_until(pause, now)
        if until is not None:
            active[pause.name.removeprefix("PAUSE-HOST-")] = until
    return active


def _probe_failed(row):
    """True when the host's SSH BatchMode probe exited non-zero (Refs #954)."""
    code = row.get("probe_exit_code")
    return code is not None and code != 0


def _usable_hosts(rows, known_hosts=None):
    return [row for row in rows
            if (known_hosts is None or row.get("host") in known_hosts)
            and row.get("final_status", row.get("status")) == "valid"
            and not _is_cooldown(row)
            and not _probe_failed(row)]


def _probe_argv(host, ssh_config=None):
    """Build the non-interactive BatchMode probe command for one fleet host."""
    argv = ["ssh", "-o", BATCHMODE_OPTION, "-o", CONNECT_TIMEOUT_OPTION]
    if ssh_config:
        argv.extend(["-F", str(Path(ssh_config).expanduser())])
    argv.extend([str(host), "--", PROBE_COMMAND])
    return argv


def _probe_host(host, *, ssh_config=None, runner=None, timeout=PROBE_TIMEOUT_SECONDS):
    """Probe one fleet host over SSH and classify the result.

    Any non-zero exit code (`Permission denied`, `Connection closed`, timeout) marks the
    host as failed.  A failed host must never be counted in hosts_ok (Refs #954).
    `runner` replaces the real SSH call so an offline check can feed fake probe output.
    """
    argv = _probe_argv(host, ssh_config=ssh_config)
    try:
        if runner is None:
            proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
            exit_code, stderr = proc.returncode, proc.stderr
        else:
            exit_code, _stdout, stderr = runner(argv)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        exit_code, stderr = 255, str(exc)
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        code = 255
    return {"host": str(host), "exit_code": code, "ok": code == 0,
            "probe_status": "ok" if code == 0 else "failed",
            "probe_error": (stderr or "").strip()[-200:]}


def _probe_settings(config, ssh_config=None, enable=None):
    """Resolve the SSH BatchMode probe settings for one refresh run.

    The fleet config enables the probe with `"ssh_probe"` (true, or a mapping with
    `enabled`, `ssh_config` and `timeout`); `--ssh-probe` and `--no-ssh-probe` override
    the fleet config.
    """
    settings = config.get("ssh_probe")
    timeout = PROBE_TIMEOUT_SECONDS
    if isinstance(settings, dict):
        enabled = bool(settings.get("enabled", True))
        ssh_config = settings.get("ssh_config") or ssh_config
        try:
            timeout = int(settings.get("timeout", PROBE_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout = PROBE_TIMEOUT_SECONDS
    elif isinstance(settings, bool):
        enabled = settings
    else:
        enabled = False
    if enable is not None:
        enabled = enable
    if enabled and not ssh_config:
        default = Path(DEFAULT_SSH_CONFIG).expanduser()
        ssh_config = str(default) if default.exists() else None
    return {"enabled": enabled, "ssh_config": ssh_config, "timeout": timeout}


def _need_ok(hosts_ok, skipped_cooldown, need_ok):
    """Allow the gate when enough hosts are usable; cooldown is not a failure."""
    if need_ok < 0:
        raise ValueError("need_ok must be non-negative")
    return hosts_ok >= need_ok


def _status_for(result, hosts, config_path, now, need_ok=None):
    rows = result.get("hosts", [])
    known_hosts = {host.get("name") for host in hosts}
    unknown = [row for row in rows if row.get("host") not in known_hosts]
    if unknown:
        result.setdefault("errors", []).extend(
            {"host": row.get("host"), "status": "unknown", "error": "host absent from topology"}
            for row in unknown
        )
        result["ok"] = False
    hosts_ok = len(_usable_hosts(rows, known_hosts))
    skipped = sum(_is_cooldown(row) for row in rows if row.get("host") in known_hosts)
    required = len(hosts) if need_ok is None else need_ok
    result["ok"] = bool(result["ok"] and _need_ok(hosts_ok, skipped, required))
    result["start_lane_autodispatch"] = bool(result.get("start_lane_autodispatch") and result["ok"])
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


def refresh(config_path, renew_command, threshold, output_path=None, pause_dir=None,
            probe_runner=None, probe_enabled=None, ssh_config=None):
    config_path = Path(config_path)
    output_path = Path(output_path or config_path)
    try:
        config = json.loads(config_path.read_text())
        hosts = config["hosts"]
        if not isinstance(hosts, list):
            raise ValueError("fleet config hosts must be a list")
        need_ok = int(config.get("need_ok", len(hosts)))
        probe = _probe_settings(config, ssh_config=ssh_config, enable=probe_enabled)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return {"ok": False, "start_lane_autodispatch": False, "error": f"fleet config: {exc}",
                "status_line": _status_line(False, 0, 0, -1, -1)}

    def finish(result):
        result["ssh_probe"] = "enabled" if probe["enabled"] else "disabled"
        return result

    now = dt.datetime.now(dt.timezone.utc)
    pause_dir = Path(pause_dir) if pause_dir is not None else config_path.parent / "autodispatch"
    cooldowns = _cooldown_hosts(pause_dir, now)
    rows, pending = [], []
    config_errors = []
    for host in hosts:
        name = host.get("name")
        if _is_cooldown(host) or name in cooldowns:
            row = {"host": name, "status": "cooldown"}
            if name in cooldowns:
                row["cooldown_until"] = cooldowns[name]
            rows.append(row)
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
        return finish(_status_for({"ok": False, "start_lane_autodispatch": False, "hosts": rows,
                                    "renewed": [], "errors": config_errors}, hosts, config_path, now, need_ok))

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
            return finish(_status_for({"ok": False, "start_lane_autodispatch": False, "hosts": rows,
                                        "renewed": [], "errors": errors}, hosts, config_path, now, need_ok))

        for cert, staged_cert in replacements:
            if cert is None:
                continue
            cert.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_cert, cert)
        if output_path != config_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            staged_config = staged / "fleet.json"
            written_hosts = [host for host, row in zip(hosts, rows) if not _is_cooldown(row)]
            written_config = {**config, "hosts": written_hosts}
            staged_config.write_text(json.dumps(written_config, indent=2) + "\n")
            os.replace(staged_config, output_path)

    # Re-read after replacement so the result is useful as a boot gate.
    result_rows = []
    for row in rows:
        cert_name = next((h.get("cert") or h.get("certificate") or h.get("ssh_cert")
                          for h in hosts if h.get("name") == row["host"]), None)
        final = _state(_cert_path(config_path, cert_name), now, threshold) if cert_name else "missing"
        result_rows.append({**row, "final_status": final})
    # Probe the fleet over SSH after the renewal, so a host that still refuses a
    # non-interactive login never enters hosts_ok (Refs #954).
    if probe["enabled"]:
        for row in result_rows:
            if _is_cooldown(row) or not row.get("host"):
                continue
            outcome = _probe_host(row["host"], ssh_config=probe["ssh_config"],
                                  runner=probe_runner, timeout=probe["timeout"])
            row["probe_exit_code"] = outcome["exit_code"]
            row["probe_status"] = outcome["probe_status"]
            if outcome["probe_error"]:
                row["probe_error"] = outcome["probe_error"]
    return finish(_status_for({"ok": True, "start_lane_autodispatch": True, "hosts": result_rows,
                                "renewed": renewed, "errors": []}, hosts, config_path, now, need_ok))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fleet-config", required=True)
    parser.add_argument("--renew-command", required=True,
                        help="command receiving the host name and writing a certificate to stdout")
    parser.add_argument("--threshold-seconds", type=int,
                        help="override IB_CERT_REUSE_TTL for this run")
    parser.add_argument("--output-fleet-config")
    parser.add_argument("--pause-dir", help="read-only directory containing PAUSE-HOST-* files")
    parser.add_argument("--status-file")
    probe = parser.add_mutually_exclusive_group()
    probe.add_argument("--ssh-probe", dest="ssh_probe", action="store_true", default=None,
                       help="probe every fleet host with `ssh -o BatchMode=yes`; a non-zero exit "
                            "keeps the host out of hosts_ok")
    probe.add_argument("--no-ssh-probe", dest="ssh_probe", action="store_false", default=None,
                       help="skip the SSH BatchMode probe")
    parser.add_argument("--ssh-config", help="SSH config file for the probe (passed to ssh with -F)")
    args = parser.parse_args(argv)
    threshold = cert_reuse_ttl(args.threshold_seconds)
    # Naming the SSH config for the probe asks for the probe as well.
    probe_enabled = True if args.ssh_probe is None and args.ssh_config else args.ssh_probe
    result = refresh(args.fleet_config, args.renew_command, threshold,
                     args.output_fleet_config, args.pause_dir,
                     probe_enabled=probe_enabled, ssh_config=args.ssh_config)
    payload = json.dumps(result, sort_keys=True)
    print(payload)
    if args.status_file:
        Path(args.status_file).write_text(result["status_line"] + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
