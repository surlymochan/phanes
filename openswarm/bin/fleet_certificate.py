#!/usr/bin/env python3
"""The fleet certificate rules shared by every fleet path.

`openswarm fleet status`, `openswarm fleet exec` and the `fleet-capacity`
admission gate must judge one host the same way, so the expiry parsing and the
command lookup live here instead of in two drifting copies.

A host is configured for the check with `renew_command` (a command that renews
the certificate and prints it) or with `check_certificate` (the opt-in that
inspects the certificate the host already carries). `cert` and `certificate`
keep their meaning of a certificate file path and never switch the check on.

The rules reach a host over the transport options of that host (`jump`,
`ssh_options`, `connect_timeout`, `control_persist`), the same ones every other
fleet command uses, so a host behind a jump host is never judged unreachable.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

RENEW_ENV = "OPENSWARM_FLEET_RENEW_COMMAND"
DEFAULT_CERTIFICATE = "~/.ssh/id_ed25519-cert.pub"


def parse_cert_expiry(output):
    """Parse the certificate expiry (UTC) from a renewal command output.

    Returns an aware UTC datetime, or None when the output carries no expiry.
    A renewal whose expiry cannot be parsed is never treated as a healthy host.
    """
    if isinstance(output, bytes):
        text = output.decode(errors="replace")
        raw = output
    else:
        text = str(output)
        raw = text.encode()

    def as_utc(value):
        """Read one expiry as an aware UTC instant, or None when it is unreadable."""
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            try:
                return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        value = str(value).strip()
        parsed = None
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%b %d %H:%M:%S %Y %Z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
                try:
                    parsed = dt.datetime.strptime(value, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            try:
                return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
            except (TypeError, ValueError, OverflowError, OSError):
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)

    try:
        data = json.loads(text.strip())
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("expiry", "expires_at", "notAfter", "not_after"):
            if data.get(key) is not None:
                expiry = as_utc(data[key])
                if expiry is not None:
                    return expiry

    if "-----BEGIN CERTIFICATE-----" in text:
        try:
            proc = subprocess.run(["openssl", "x509", "-noout", "-enddate"],
                                  input=raw, capture_output=True, timeout=5)
            enddate = proc.stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""
            if "notAfter=" in enddate:
                expiry = as_utc(enddate.split("notAfter=", 1)[1].strip())
                if expiry is not None:
                    return expiry
        except (OSError, subprocess.SubprocessError):
            pass

    match = re.search(r'(?:notAfter|expiry|expires)[:=]\s*([^\n\r]+)', text, re.I)
    if match:
        expiry = as_utc(match.group(1).strip())
        if expiry is not None:
            return expiry

    # An SSH certificate inspection prints its validity window, e.g. ssh-keygen -L.
    match = re.search(r'Valid:\s+from\s+.*?\s+to\s+([^\s\n\r]+)', text, re.I)
    if match:
        return as_utc(match.group(1).strip())
    return None


def command_argv(command):
    """Return a configured command as an argv list, or None when it carries none.

    A command that cannot be split (unbalanced quotes) carries none: the caller
    turns that into an unusable certificate command instead of a traceback.
    """
    if isinstance(command, str):
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
    elif isinstance(command, (list, tuple)):
        parts = [str(part) for part in command]
    else:
        return None
    return parts or None


def fleet_home():
    """The openswarm home that holds fleet.json (OPENSWARM_HOME, then the legacy name)."""
    for name in ("OPENSWARM_HOME", "XSWARM_HOME"):
        value = os.environ.get(name)
        if value:
            return value
    return str(Path.home() / ".claude/openswarm")


def fleet_config_paths():
    """The fleet.json location a fleet host may be configured in."""
    return [Path(fleet_home()) / "fleet.json"]


def host_entry(name):
    """The fleet.json entry of one host, or None when no config declares it.

    A host is found by its fleet name and by its ssh alias, so the capacity
    coordinator (which knows a host by its capacity id) finds the very entry
    the fleet CLI judges.
    """
    for path in fleet_config_paths():
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        hosts = config.get("hosts") if isinstance(config, dict) else None
        if not isinstance(hosts, list):
            continue
        for host in hosts:
            if not isinstance(host, dict):
                continue
            if host.get("name") == name or (host.get("ssh") and host.get("ssh") == name):
                return host
    return None


def topology_path():
    """The fleet topology that sits next to the capacity database."""
    root = os.environ.get("OPENSWARM_FLEET_CAPACITY_DIR") or str(Path.home() / ".claude/openswarm-fleet-capacity")
    return Path(root) / "topology.json"


def topology_renew_command(name, aliases=()):
    """The renewal command the fleet topology declares for one host.

    The aliases are the other names one host is known by (a capacity host id),
    so the topology finds the host under either of them.
    """
    try:
        config = json.loads(topology_path().read_text())
    except (OSError, ValueError):
        return None
    commands = config.get("renew_commands") if isinstance(config, dict) else None
    if isinstance(commands, dict):
        for key in (name, *aliases):
            command = command_argv(commands.get(key))
            if command:
                return command
    return None


def ssh_control_path():
    """The default ControlPath of the multiplexed fleet connection."""
    return os.path.join(os.path.expanduser("~"), ".ssh", "openswarm", "cm-%C")


def ssh_option_pairs(host):
    """The SSH option pairs of one fleet host, in the order the fleet CLI uses.

    `connect_timeout` bounds the handshake, `control_persist` turns on the
    ControlMaster reuse, `jump` becomes a ProxyJump chain and `ssh_options`
    carries everything else. The explicit table is applied last, so an operator
    can always override a derived option.
    """
    pairs = {}
    timeout = host.get("connect_timeout")
    if timeout is not None:
        pairs["ConnectTimeout"] = str(timeout)
    persist = host.get("control_persist")
    if persist is not None:
        pairs["ControlMaster"] = "auto"
        pairs["ControlPath"] = ssh_control_path()
        pairs["ControlPersist"] = str(persist)
    jump = host.get("jump")
    if jump:
        hops = [jump] if isinstance(jump, str) else list(jump)
        pairs["ProxyJump"] = ",".join(hops)
    for key, value in (host.get("ssh_options") or {}).items():
        pairs[key] = str(value)
    return list(pairs.items())


def ssh_argv(host):
    """The ssh prefix of one fleet host: its transport options, then the host.

    The certificate check uses it, so a host behind a jump host or with a
    ProxyCommand is reached the way every other fleet command reaches it.
    """
    alias = host.get("ssh") or host.get("name")
    options = []
    for key, value in ssh_option_pairs(host):
        options += ["-o", f"{key}={value}"]
    return ["ssh", *options, alias]


def inspect_command(name, host, ssh_prefix=None):
    """The default certificate inspection command of one ssh host.

    `ssh <alias> ssh-keygen -L -f <certificate>` prints the validity window of
    the host certificate, so its expiry can be read. A bare interactive ssh
    would print nothing and is never used.
    """
    if (host.get("kind") or "ssh") != "ssh":
        return None
    alias = host.get("ssh") or name
    certificate = host.get("check_certificate")
    path = certificate if isinstance(certificate, str) and certificate else DEFAULT_CERTIFICATE
    prefix = list(ssh_prefix) if ssh_prefix else ssh_argv(host)
    return prefix + ["ssh-keygen", "-L", "-f", path]


def resolve_command(name, host=None, ssh_prefix=None, aliases=()):
    """The certificate command of one fleet host and why it is unusable.

    Returns (command, error): an empty command with an empty error means the
    host has no certificate check, an error means the host declares a check
    that cannot run, which leaves the host without a usable certificate.
    Every fleet path resolves a host the same way: the host's own
    `renew_command`, then its `check_certificate` opt-in, then the topology
    `renew_commands`, then the OPENSWARM_FLEET_RENEW_COMMAND environment
    variable. The command never comes from a request.
    """
    entry = host if isinstance(host, dict) else host_entry(name)
    kind = (entry.get("kind") or "ssh") if isinstance(entry, dict) else "ssh"
    if isinstance(entry, dict):
        if entry.get("renew_command") is not None:
            command = command_argv(entry.get("renew_command"))
            if not command:
                return None, "certificate renewal command is not usable"
            return command, ""
        if entry.get("check_certificate"):
            command = inspect_command(name, entry, ssh_prefix)
            if not command:
                return None, "certificate check command is not usable"
            return command, ""
    command = topology_renew_command(name, aliases)
    if command:
        return command, ""
    if kind != "ssh":
        # The global fallback reaches the ssh hosts only.
        return None, ""
    fallback = os.environ.get(RENEW_ENV)
    if fallback:
        command = command_argv(fallback)
        if not command:
            return None, "certificate renewal command is not usable"
        return command, ""
    return None, ""


def certificate_status(command, timeout=20):
    """Run the certificate command of a host and judge the result.

    Returns (ok, error): a failed command, a missing expiry and an expiry in
    the past all leave the host without a usable certificate. The command never
    reads stdin, so a status query cannot hang on an interactive ssh.
    """
    if not command:
        return True, ""
    try:
        proc = subprocess.run(command, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False, "certificate renewal command failed"
    if proc.returncode != 0:
        return False, f"certificate renewal command failed (rc={proc.returncode})"
    expiry = parse_cert_expiry(proc.stdout)
    if expiry is None:
        return False, "certificate expiry missing from renewal output"
    if expiry <= dt.datetime.now(dt.timezone.utc):
        return False, "certificate expired"
    return True, ""


def host_certificate_status(name, timeout=20, host=None, ssh_prefix=None, aliases=()):
    """The certificate status of one fleet host, resolved from its own config."""
    command, error = resolve_command(name, host=host, ssh_prefix=ssh_prefix, aliases=aliases)
    if error:
        return False, error
    return certificate_status(command, timeout=timeout)
