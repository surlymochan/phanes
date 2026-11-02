#!/usr/bin/env python3
"""External heartbeat probe for the box control plane (Refs #1550).

Runs on an always-on host and tells a supervisor whether a silent box is
paused as a VM (no heartbeat, but a pause record covers the gap) or hung
(no heartbeat and no pause to explain it).

Heartbeat files are ``touch``ed by the control-plane loops, so their mtime
(``stat -c %Y``) is the heartbeat time.  Pause records come from
``box-pauses.jsonl`` as ``{"started": ..., "resumed": ...}``; an open pause
has no ``resumed`` and covers everything up to now.  A pause that ended more
than ``stale_s`` ago no longer explains silence: a resumed box must beat again
within that grace period.  A stale heartbeat is explained by pauses only when
no stretch of awake silence after it exceeds ``stale_s``.

Exit codes: 0 ok, 2 vm_paused, 3 hung, 4 unreachable, 64 usage error.
Standard library only, Python 3.9+.
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone

OK = "ok"
VM_PAUSED = "vm_paused"
HUNG = "hung"
UNREACHABLE = "unreachable"

EXIT_CODES = {OK: 0, VM_PAUSED: 2, HUNG: 3, UNREACHABLE: 4}
EXIT_USAGE = 64

AUTODISPATCH_DIR = "phanes-ctl/autodispatch"
PAUSE_FILE = "box-pauses.jsonl"
PAUSE_TAIL_LINES = 200
DEFAULT_FILES = ("heartbeat", "merge-heartbeat", "health.heartbeat")
DEFAULT_STALE_S = 180
SSH_TIMEOUT_S = 30
_NAME_RE = re.compile(r"(?!\.+\Z)[A-Za-z0-9._-]+")


def _epoch(value):
    """Return a POSIX timestamp for a number or ISO-8601 string, else None.

    Offsets are honoured, so ``+08:00`` and ``Z`` stamps compare by instant.
    A naive ISO stamp is taken as UTC.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_heartbeats(stdout, files):
    """Map each file name to its epoch time, or None when missing or unreadable."""
    found = {name: None for name in files}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "HB" and parts[1] in found:
            found[parts[1]] = _epoch(parts[2])
    return found


def parse_pauses(stdout):
    """Return ``(started, resumed)`` pairs; a bad line is skipped, not fatal.

    ``resumed`` is None for a pause that is still open.
    """
    pauses = []
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        started = _epoch(record.get("started"))
        if started is None:
            continue
        resumed = _epoch(record.get("resumed"))
        if resumed is not None and resumed < started:
            continue
        pauses.append((started, resumed))
    return pauses


def _explained(pauses, window_start, now, stale_s):
    """True when pauses explain silence in ``(window_start, now]``.

    Pauses are clipped to the window and must cover at least part of it.  The
    uncovered time, which is awake silence, must never run longer than
    ``stale_s``: a stretch of that length without a beat means the loop hung,
    even if a pause came before or after it.
    """
    spans = []
    for started, resumed in pauses:
        end = now if resumed is None else min(resumed, now)
        lo = max(started, window_start)
        hi = min(end, now)
        if hi > lo:
            spans.append((lo, hi))
    if not spans:
        return False
    cursor = window_start
    longest_awake = 0.0
    for lo, hi in sorted(spans):
        longest_awake = max(longest_awake, lo - cursor)
        cursor = max(cursor, hi)
    longest_awake = max(longest_awake, now - cursor)
    return longest_awake <= stale_s


def judge(heartbeats, pauses, now, stale_s):
    """Return ``(status, detail)`` for the heartbeat map and pause list.

    Every stale file must be explained by pauses; the first one that is not
    is reported as hung.
    """
    stale = [name for name, beat in heartbeats.items()
             if beat is None or now - beat > stale_s]
    if not stale:
        return OK, ""
    known = [beat for beat in heartbeats.values() if beat is not None]
    for name in stale:
        beat = heartbeats[name]
        if beat is not None:
            window_start = beat
        else:
            window_start = min(known) if known else now - stale_s
        if not _explained(pauses, window_start, now, stale_s):
            return HUNG, name
    return VM_PAUSED, ""


def default_runner(host, command):
    """Run ``command`` on ``host`` over ssh; return ``(returncode, stdout)``."""
    argv = ["ssh", "-o", "BatchMode=yes", host, command]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=SSH_TIMEOUT_S, check=False)
    return proc.returncode, proc.stdout


def build_command(files):
    """Build one remote shell command printing heartbeat times and pause tail."""
    base = f'"$HOME/{AUTODISPATCH_DIR}"'
    parts = []
    for name in files:
        path = f'{base}/{shlex.quote(name)}'
        parts.append(f'printf "HB %s %s\\n" {shlex.quote(name)} '
                     f'"$(stat -c %Y {path} 2>/dev/null)"')
    parts.append("echo PAUSES")
    parts.append(f'tail -n {PAUSE_TAIL_LINES} {base}/{PAUSE_FILE} 2>/dev/null || true')
    return "; ".join(parts)


def probe(host, files, stale_s=DEFAULT_STALE_S, now=None, runner=None):
    """Probe ``host`` and return a result dict with status, exit code and detail."""
    now = time.time() if now is None else now
    runner = runner or default_runner
    for name in files:
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"invalid heartbeat file name: {name!r}")
    try:
        rc, out = runner(host, build_command(files))
    except (OSError, subprocess.SubprocessError) as exc:
        return _result(host, UNREACHABLE, str(exc), now, stale_s)
    if rc != 0:
        return _result(host, UNREACHABLE, f"ssh exit {rc}", now, stale_s)
    head, _sep, tail = out.partition("PAUSES")
    heartbeats = parse_heartbeats(head, files)
    pauses = parse_pauses(tail)
    status, detail = judge(heartbeats, pauses, now, stale_s)
    result = _result(host, status, detail, now, stale_s)
    result["heartbeats"] = heartbeats
    result["pauses"] = len(pauses)
    return result


def _result(host, status, detail, now, stale_s):
    reason = status if not detail else f"{status}:{detail}"
    return {"host": host, "status": status, "reason": reason,
            "exit": EXIT_CODES[status], "checked_at": now, "stale_s": stale_s}


class _UsageParser(argparse.ArgumentParser):
    """Usage errors exit 64 so they never look like the exit 2 vm_paused code."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def main(argv=None, runner=None, now=None):
    parser = _UsageParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True, help="ssh host alias of the box")
    parser.add_argument("--files", default=",".join(DEFAULT_FILES),
                        help="comma-separated heartbeat files under ~/phanes-ctl/autodispatch")
    parser.add_argument("--stale-s", type=float, default=DEFAULT_STALE_S,
                        help="seconds without a heartbeat before it counts as stale")
    parser.add_argument("--json", action="store_true", help="print JSON result")
    args = parser.parse_args(argv)
    files = [name.strip() for name in args.files.split(",") if name.strip()]
    if not files:
        parser.error("--files must name at least one heartbeat file")
    try:
        result = probe(args.host, files, args.stale_s, now=now, runner=runner)
    except ValueError as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"{result['host']} {result['reason']}")
    return result["exit"]


if __name__ == "__main__":
    sys.exit(main())
