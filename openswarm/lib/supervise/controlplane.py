"""Control plane supervision: service table -> pidfile supervisor + boot hooks.

The control plane loops of a box without systemd (``ctl-loop``,
``lane-autodispatch``, ``lane-merge``, ``bus-worker-loop``, ``health-loop``) used to be
started by hand written ``nohup`` wrappers.  After a reboot nobody brought them back.

This module turns a JSON service table into two artifacts:

``render_supervisor(services, state_dir)``
    A POSIX ``sh`` script that supervises every service with one pid file per
    service, restarts a crashed service with an exponential backoff (capped at 60s)
    and understands ``start`` / ``stop`` / ``status``.

``render_boot_hook(kind, supervisor_path)``
    The boot hook text for ``cron-reboot`` (one ``@reboot`` line), ``systemd-user``
    (a user unit) or ``launchd`` (a plist).  Only text is produced; installing it is
    left to the caller.

``load_services(cfg)`` reads and validates the service table, ``status(services,
state_dir)`` reports ``running`` / ``dead`` / ``stale-pid`` per service.

The service table lives in the deployment config, not in this repository.

The generated script keeps one file per service in ``state_dir``:

``<name>.pid``
    The pid of the running service.
``<name>.sup.pid``
    The pid of the background supervisor.
``<name>.stop``
    Stop flag: ``stop`` sets it, the supervisor removes it when it exits.  The
    supervisor checks the flag before it starts a service and after every backoff
    sleep, so a stopped service is never revived.
``<name>.lock``
    Lock file (taken with a noclobber redirection) that serialises start/stop.

Standard library only, Python 3.9+.  The CLI wiring (and replacing the hand written
loops on the box) belongs to #1357.
"""
import json
import os
import re
import shlex
import xml.sax.saxutils

__all__ = [
    "load_services",
    "render_boot_hook",
    "render_supervisor",
    "status",
]

_SERVICE_KEYS = {"name", "cmd", "cwd", "env", "restart_backoff_max"}

# The issue caps the restart backoff at one minute.
MAX_BACKOFF_SECONDS = 60

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Shell control operators: a command using them needs a real shell, everything else
# can be exec'd directly so that the pid file holds the service pid itself.
_CONTROL_CHARS = "|;&<>()"


def load_services(cfg):
    """Load and validate a JSON service table from a path, file, JSON string or list."""
    if hasattr(cfg, "read"):
        services = json.load(cfg)
    elif isinstance(cfg, (list, tuple)):
        services = list(cfg)
    else:
        value = os.fspath(cfg)
        try:
            with open(value, encoding="utf-8") as stream:
                services = json.load(stream)
        except (OSError, TypeError):
            services = json.loads(value)
    if not isinstance(services, list):
        raise ValueError("service table must be a JSON array")
    names = set()
    validated = []
    for index, service in enumerate(services):
        prefix = "services[%d]" % index
        if not isinstance(service, dict):
            raise ValueError("%s must be an object" % prefix)
        unknown = set(service) - _SERVICE_KEYS
        missing = _SERVICE_KEYS - set(service)
        if unknown or missing:
            raise ValueError("%s fields invalid (missing=%s, unknown=%s)" % (prefix, sorted(missing), sorted(unknown)))
        name = service["name"]
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValueError("%s.name must be a safe service name" % prefix)
        if name in names:
            raise ValueError("duplicate service name: %s" % name)
        names.add(name)
        cmd = service["cmd"]
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError("%s.cmd must be a non-empty string" % prefix)
        if "\n" in cmd or "\r" in cmd:
            raise ValueError("%s.cmd must be a single line" % prefix)
        if "#" in cmd:
            raise ValueError("%s.cmd must not contain '#'" % prefix)
        cwd = service["cwd"]
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("%s.cwd must be a non-empty string" % prefix)
        env = service["env"]
        if not isinstance(env, dict) or any(
            not isinstance(k, str) or not _ENV_KEY_RE.fullmatch(k) or not isinstance(v, str) for k, v in env.items()
        ):
            raise ValueError("%s.env must map variable names to strings" % prefix)
        maximum = service["restart_backoff_max"]
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_BACKOFF_SECONDS:
            raise ValueError("%s.restart_backoff_max must be an integer between 1 and %d" % (prefix, MAX_BACKOFF_SECONDS))
        validated.append(dict(service))
    return validated


def _shell_identifiers(services):
    """Map every service name to a unique, POSIX safe shell identifier.

    POSIX function names only allow ``[A-Za-z_][A-Za-z0-9_]*`` while the box uses
    names such as ``health-loop`` or ``merge-loop.sh``; the mapping happens here so
    that a hand written table cannot produce a script the shell refuses to parse.
    """
    identifiers = {}
    used = set()
    for service in services:
        name = service["name"]
        base = re.sub(r"[^A-Za-z0-9_]", "_", name)
        if not base or not re.match(r"[A-Za-z_]", base[0]):
            base = "_" + base
        identifier = base
        suffix = 2
        while identifier in used:
            identifier = "%s_%d" % (base, suffix)
            suffix += 1
        used.add(identifier)
        identifiers[name] = identifier
    return identifiers


def _state_file(name):
    """Return the shell expression for a file inside the state directory."""
    return '"$STATE_DIR"/%s' % shlex.quote(name)


def _command_line(cmd):
    """Return the shell line that starts the service.

    A command without control operators is exec'd through ``sh -c 'exec ...'`` so the
    pid file holds the pid of the service itself and stopping it really stops it.
    """
    if any(char in cmd for char in _CONTROL_CHARS):
        return "exec sh -c %s" % shlex.quote(cmd)
    return "exec sh -c %s" % shlex.quote("exec " + cmd)


_HELPERS = [
    "_sup_lock() {",
    "  # $1: lock file. Serialises start/stop of one service.",
    "  # The lock is taken with a noclobber redirection (O_EXCL): mkdir is not",
    "  # atomic enough on every filesystem.",
    "  _sup_try=0",
    "  while :; do",
    '    if ( set -C; printf \'%s\\n\' "$$" > "$1" ) 2>/dev/null; then',
    "      return 0",
    "    fi",
    '    _sup_holder=$(cat "$1" 2>/dev/null || :)',
    '    case "$_sup_holder" in',
    "      ''|*[!0-9]*) ;;",
    '      *) kill -0 "$_sup_holder" 2>/dev/null || { rm -f "$1"; continue; } ;;',
    "    esac",
    "    _sup_try=$((_sup_try + 1))",
    '    [ "$_sup_try" -lt 200 ] || return 1',
    "    sleep 0.1",
    "  done",
    "}",
    "",
    "_sup_unlock() {",
    '  rm -f "$1"',
    "}",
    "",
    "_sup_alive() {",
    "  # $1: pid file. Succeeds while the recorded pid is running.",
    '  [ -f "$1" ] || return 1',
    '  _sup_pid=$(cat "$1" 2>/dev/null || :)',
    '  case "$_sup_pid" in',
    "    ''|*[!0-9]*) return 1 ;;",
    "  esac",
    '  kill -0 "$_sup_pid" 2>/dev/null',
    "}",
    "",
]


def _service_block(service, identifier):
    """Return the shell functions supervising one service."""
    name = service["name"]
    quoted_name = shlex.quote(name)
    pidfile = _state_file(name + ".pid")
    supfile = _state_file(name + ".sup.pid")
    stopfile = _state_file(name + ".stop")
    lockfile = _state_file(name + ".lock")
    try:
        backoff = int(service.get("restart_backoff_max", MAX_BACKOFF_SECONDS))
    except (TypeError, ValueError):
        backoff = MAX_BACKOFF_SECONDS
    backoff = max(1, min(backoff, MAX_BACKOFF_SECONDS))
    body = ["    (", "      cd %s || exit 125" % shlex.quote(str(service.get("cwd") or "."))]
    for key, value in sorted((service.get("env") or {}).items()):
        if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key) or not isinstance(value, str):
            continue
        body.append("      export %s=%s" % (key, shlex.quote(value)))
    body.append("      %s" % _command_line(str(service.get("cmd") or "true")))
    body.append("    ) >/dev/null 2>&1 &")
    return [
        "# --- %s ---" % name,
        "_supervise_%s() {" % identifier,
        "  # Background supervisor: restart the service with an exponential backoff.",
        "  _sup_backoff=1",
        "  while :; do",
        "    [ -f %s ] && break" % stopfile,
    ] + body + [
        "    _sup_child=$!",
        "    printf '%%s\\n' \"$_sup_child\" > %s" % pidfile,
        '    [ -f %s ] && kill "$_sup_child" 2>/dev/null || :' % stopfile,
        '    wait "$_sup_child"',
        "    _sup_rc=$?",
        '    if [ "$(cat %s 2>/dev/null || :)" = "$_sup_child" ]; then' % pidfile,
        "      rm -f %s" % pidfile,
        "    fi",
        '    [ "$_sup_rc" -eq 0 ] && break',
        "    [ -f %s ] && break" % stopfile,
        "    # exponential backoff, 1s .. %ds" % backoff,
        "    _sup_waited=0",
        '    while [ "$_sup_waited" -lt "$_sup_backoff" ]; do',
        "      [ -f %s ] && break" % stopfile,
        "      sleep 1",
        "      _sup_waited=$((_sup_waited + 1))",
        "    done",
        "    [ -f %s ] && break" % stopfile,
        '    if [ "$_sup_backoff" -lt %d ]; then _sup_backoff=$((_sup_backoff * 2)); fi' % backoff,
        '    if [ "$_sup_backoff" -gt %d ]; then _sup_backoff=%d; fi' % (backoff, backoff),
        "  done",
        "  rm -f %s %s %s" % (pidfile, stopfile, supfile),
        "}",
        "start_%s() {" % identifier,
        "  _sup_lock %s || return 1" % lockfile,
        "  rm -f %s" % stopfile,
        "  if _sup_alive %s || _sup_alive %s; then" % (supfile, pidfile),
        "    _sup_unlock %s" % lockfile,
        "    return 0",
        "  fi",
        "  rm -f %s %s" % (supfile, pidfile),
        "  # the supervisor must not keep the caller's stdout/stderr open",
        "  ( exec 3>&- 4>&-; _supervise_%s ) >/dev/null 2>&1 &" % identifier,
        "  printf '%%s\\n' \"$!\" > %s" % supfile,
        "  # the service pid file must exist before start returns",
        "  _sup_try=0",
        '  while [ "$_sup_try" -lt 40 ]; do',
        "    [ -f %s ] && break" % pidfile,
        "    _sup_alive %s || break" % supfile,
        "    sleep 0.1",
        "    _sup_try=$((_sup_try + 1))",
        "  done",
        "  _sup_unlock %s" % lockfile,
        "  return 0",
        "}",
        "stop_%s() {" % identifier,
        "  _sup_lock %s || return 1" % lockfile,
        "  # the stop flag keeps the supervisor from starting the service again",
        "  : > %s" % stopfile,
        '  _sup_pid=$(cat %s 2>/dev/null || :)' % pidfile,
        '  case "$_sup_pid" in',
        "    ''|*[!0-9]*) _sup_pid='' ;;",
        "  esac",
        '  if [ -n "$_sup_pid" ]; then',
        '    kill "$_sup_pid" 2>/dev/null || :',
        "    _sup_try=0",
        '    while [ "$_sup_try" -lt 50 ]; do',
        '      kill -0 "$_sup_pid" 2>/dev/null || break',
        "      sleep 0.1",
        "      _sup_try=$((_sup_try + 1))",
        "    done",
        '    kill -0 "$_sup_pid" 2>/dev/null && kill -9 "$_sup_pid" 2>/dev/null || :',
        "  fi",
        "  # wait until the supervisor itself is gone",
        '  _sup_pid=$(cat %s 2>/dev/null || :)' % supfile,
        '  case "$_sup_pid" in',
        "    ''|*[!0-9]*) _sup_pid='' ;;",
        "  esac",
        '  if [ -n "$_sup_pid" ]; then',
        "    _sup_try=0",
        '    while [ "$_sup_try" -lt 200 ]; do',
        "      [ -f %s ] || break" % supfile,
        '      kill -0 "$_sup_pid" 2>/dev/null || break',
        "      sleep 0.1",
        "      _sup_try=$((_sup_try + 1))",
        "    done",
        '    if [ -f %s ] && kill -0 "$_sup_pid" 2>/dev/null; then' % supfile,
        '      kill -9 "$_sup_pid" 2>/dev/null || :',
        "    fi",
        "  fi",
        "  # a service started between the flag and the kill is cleaned up as well",
        '  _sup_pid=$(cat %s 2>/dev/null || :)' % pidfile,
        '  case "$_sup_pid" in',
        "    ''|*[!0-9]*) _sup_pid='' ;;",
        "  esac",
        '  if [ -n "$_sup_pid" ]; then',
        '    kill "$_sup_pid" 2>/dev/null || :',
        "  fi",
        "  rm -f %s %s" % (pidfile, supfile),
        "  _sup_unlock %s" % lockfile,
        "  return 0",
        "}",
        "status_%s() {" % identifier,
        "  if [ ! -f %s ]; then" % pidfile,
        "    echo %s:dead >&3" % quoted_name,
        "    return 0",
        "  fi",
        '  _sup_pid=$(cat %s 2>/dev/null || :)' % pidfile,
        '  case "$_sup_pid" in',
        "    ''|*[!0-9]*) echo %s:stale-pid >&3; return 0 ;;" % quoted_name,
        "  esac",
        '  if kill -0 "$_sup_pid" 2>/dev/null; then',
        "    echo %s:running >&3" % quoted_name,
        "  else",
        "    echo %s:stale-pid >&3" % quoted_name,
        "  fi",
        "}",
        "",
    ]


def render_supervisor(services, state_dir):
    """Return a POSIX shell script implementing start, stop, and status."""
    services = list(services)
    identifiers = _shell_identifiers(services)
    state = shlex.quote(os.fspath(state_dir))
    lines = [
        "#!/bin/sh",
        "# Generated by openswarm/lib/supervise/controlplane.py (Refs #1460). Do not edit.",
        "set -u",
        "# The real stdout/stderr are kept on fd 3/4: the background supervisor must",
        "# not hold the caller's pipe open, so everything but the status output goes",
        "# to /dev/null.",
        "exec 3>&1 4>&2",
        "exec >/dev/null 2>&1",
        "STATE_DIR=%s" % state,
        'mkdir -p "$STATE_DIR" 2>&4 || exit 1',
        "",
    ]
    lines.extend(_HELPERS)
    for service in services:
        lines.extend(_service_block(service, identifiers[service["name"]]))
    lines.append("start_all() {")
    lines.append("  _sup_failed=0")
    for service in services:
        lines.append("  start_%s || _sup_failed=1" % identifiers[service["name"]])
    lines.extend(["  return \"$_sup_failed\"", "}", ""])
    lines.append("stop_all() {")
    lines.append("  _sup_failed=0")
    for service in reversed(services):
        lines.append("  stop_%s || _sup_failed=1" % identifiers[service["name"]])
    lines.extend(["  return \"$_sup_failed\"", "}", ""])
    lines.append("status_all() {")
    for service in services:
        lines.append("  status_%s" % identifiers[service["name"]])
    lines.extend(["}", ""])
    lines.extend([
        'case "${1:-}" in',
        "  start) start_all ;;",
        "  stop) stop_all ;;",
        "  status) status_all ;;",
        '  *) echo "usage: $0 start|stop|status" >&4; exit 2 ;;',
        "esac",
        "",
    ])
    return "\n".join(lines)


def _systemd_quote(value):
    """Quote a path for a systemd unit (double quotes, escaped specials)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"%s"' % escaped


def render_boot_hook(kind, supervisor_path):
    """Render an installable cron, systemd user, or launchd boot hook."""
    path = os.fspath(supervisor_path)
    if kind == "cron-reboot":
        return "@reboot %s start\n" % shlex.quote(path)
    if kind == "systemd-user":
        quoted = _systemd_quote(path)
        return """[Unit]
Description=OpenSwarm control plane supervisor
After=default.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=%s start
ExecStop=%s stop

[Install]
WantedBy=default.target
""" % (quoted, quoted)
    if kind == "launchd":
        escaped = xml.sax.saxutils.escape(path, {'"': "&quot;"})
        return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>org.openswarm.controlplane</string>
<key>ProgramArguments</key><array><string>%s</string><string>start</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>
""" % escaped
    raise ValueError("unsupported boot hook kind: %s" % kind)


def status(services, state_dir):
    """Return pidfile status records for each configured service."""
    result = {}
    state = os.fspath(state_dir)
    for service in services:
        name = service["name"]
        try:
            with open(os.path.join(state, name + ".pid"), encoding="ascii") as stream:
                raw = stream.read().strip()
            pid = int(raw)
            if pid <= 0:
                raise ValueError
        except FileNotFoundError:
            result[name] = {"state": "dead", "pid": None}
            continue
        except (OSError, ValueError):
            result[name] = {"state": "stale-pid", "pid": None}
            continue
        try:
            os.kill(pid, 0)
            result[name] = {"state": "running", "pid": pid}
        except ProcessLookupError:
            result[name] = {"state": "stale-pid", "pid": pid}
        except PermissionError:
            result[name] = {"state": "running", "pid": pid}
    return result
