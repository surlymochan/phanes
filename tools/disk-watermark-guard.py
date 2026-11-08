#!/usr/bin/env python3
"""govpn2 disk watermark guard: measure the remote lane host and pause it (Refs #1666).

The lane host `govpn2` runs the CX2/CX4 review lanes; when its disk fills up
the reviews stall.  The guard is the resident loop of the box control plane:

* it measures the **remote** filesystem of the lane host over ssh (the box and
  govpn2 have very different disks, so the local disk says nothing about the
  lane host);
* above the block threshold it pauses the host (a `PAUSE-HOST-<host>` marker
  that the dispatcher reads) and writes an alert row into the brain log;
* between the block and the recovery threshold it **keeps** an existing pause
  (the hysteresis band) and restores the marker when another sweep removed it;
  a host that was not blocked only gets a warning;
* at or below the recovery threshold it releases **its own** marker only: a pause
  that belongs to another guard or to an operator is never taken over and never
  released;
* a failed probe is never a healthy host: the guard fails closed (the lane
  stays paused) and says so in the brain log.

The usage probe (`tools/disk-usage-probe.py`) is a hard dependency of the
guard: it lives next to the guard and is loaded from there, so the deployed
pair must always be installed together (`tools/install-disk-watermark-guard.sh`).

Usage:

    python3 tools/disk-watermark-guard.py --pause-dir ~/phanes-ctl/autodispatch \
        --brain-log /workspace/phanes/brain/log.md --host govpn2
    python3 tools/disk-watermark-guard.py --loop --host govpn2
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

GUARD_VERSION = 1
DEFAULT_HOST = "govpn2"
BLOCK_PCT = 95          # at or above this usage the lane host is paused
RECOVER_PCT = 87        # at or below this usage the guard releases its own pause
INTERVAL = 300          # seconds between two measurements of the resident loop
SSH_TIMEOUT = 60        # seconds the remote probe may take
MARKER_PREFIX = "auto:disk-watermark"   # the body prefix of the marker of this guard
STATE_PREFIX = "disk-watermark-"        # the state file prefix of this guard


class ProbeError(RuntimeError):
    """The remote filesystem could not be measured."""


def load_probe():
    """Load the usage probe that sits next to this guard.

    The probe is a hard dependency: without it the guard cannot read a df row,
    so it must not start and pretend that the lane host is healthy.
    """
    path = Path(__file__).resolve().parent / "disk-usage-probe.py"
    if not path.is_file():
        raise SystemExit(f"missing {path}: the usage probe of the guard is not installed")
    spec = importlib.util.spec_from_file_location("disk_usage_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROBE = load_probe()


def marker_path(pause_dir, host):
    """The pause marker of a host (the dispatcher reads `PAUSE-HOST-<host>`)."""
    return Path(pause_dir) / f"PAUSE-HOST-{host}"


def state_path(pause_dir, host):
    """The state file of a host (the independent gate of the dispatcher)."""
    return Path(pause_dir) / f"{STATE_PREFIX}{host}.json"


def owns_marker(body):
    """Whether a pause marker body belongs to this guard."""
    return bool(body) and body.startswith(MARKER_PREFIX)


def decide(used_pct, block_pct=BLOCK_PCT, recover_pct=RECOVER_PCT, was_blocked=False):
    """The watermark state of a measured host.

    `block`     - at or above the block threshold: the lane host is paused;
    `hold`      - between the thresholds after a block: the host stays paused;
    `warn`      - between the thresholds without a block: dispatch stays open;
    `recovered` - at or below the recovery threshold: the guard may release its own
                  pause.
    """
    if used_pct >= block_pct:
        return "block"
    if used_pct > recover_pct:
        return "hold" if was_blocked else "warn"
    return "recovered"


def ensure_marker(path, host, used_pct, state):
    """Create or refresh our own pause marker.

    The marker is restored when another sweep removed it (the health sweep
    clears stale `auto:` host pauses): the gate has to stay closed for the
    whole hysteresis band.  A marker of another owner is never overwritten.

    Returns `created` (the marker was missing and is back), `refreshed` (our
    marker was updated) or `foreign` (the pause belongs to somebody else).
    """
    body = f"{MARKER_PREFIX} host={host} state={state} used_pct={used_pct}\n"
    if path.exists():
        try:
            current = path.read_text(errors="replace")
        except OSError:
            current = ""
        if not owns_marker(current):
            return "foreign"        # somebody else owns the pause: never take it over
        write_atomic(path, body)
        return "refreshed"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, body)
    return "created"


def release_marker(path):
    """Release our own pause marker; a marker of another owner stays."""
    if not path.exists():
        return False
    try:
        current = path.read_text(errors="replace")
    except OSError:
        return False
    if not owns_marker(current):
        return False
    path.unlink()
    return True


def write_atomic(path, text):
    """Write a file in one step: a reader never sees a half written file.

    The dispatcher reads the state file and the pause marker of this guard
    while the guard writes them.  A torn write would be read as "no state" and
    would open the dispatch gate for one tick, so the files are replaced
    atomically.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The hidden name keeps the temporary file out of the scans of the pause
    # directory (the health sweep globs `PAUSE-HOST-*`, the dispatcher globs
    # `disk-watermark-*.json`).
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temp.write_text(text)
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def write_state(path, host, state, blocked, usage):
    """Write the state file the dispatcher reads as an independent disk gate."""
    record = {"host": host, "state": state, "blocked": bool(blocked),
              "used_pct": usage.get("used_pct"), "used_bytes": usage.get("used_bytes"),
              "free_bytes": usage.get("free_bytes"), "guard": GUARD_VERSION,
              "updated_at": time.time()}
    write_atomic(path, json.dumps(record, sort_keys=True) + "\n")
    return record


def read_state(path):
    """The last state record of the guard (an unreadable file is no state)."""
    try:
        with open(path) as stream:
            record = json.load(stream)
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def brain_row(path, **fields):
    """Append one alert row to the brain log and return the written row."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [stamp, "disk-watermark", f"guard={GUARD_VERSION}"]
    parts.extend(f"{key}={value}" for key, value in fields.items())
    row = " ".join(parts)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(row + "\n")
    return row


def measure_remote(host, path="/", ssh_binary="ssh", timeout=SSH_TIMEOUT):
    """Measure the remote filesystem of ``host`` over ssh.

    The control plane box and the lane hosts have different disks, so the
    guard never measures its own filesystem.
    """
    command = [ssh_binary, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
               host, "df", "-Pk", path]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise ProbeError(f"the ssh probe of {host} failed: {error}") from error
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        raise ProbeError(f"the ssh probe of {host} failed with rc={done.returncode}"
                         + (f": {detail[-1]}" if detail else ""))
    try:
        return PROBE.parse_df(done.stdout, path)
    except ValueError as error:
        raise ProbeError(f"the ssh probe of {host} returned no filesystem row: {error}") from error


def run_once(options):
    """Measure the remote host once and apply the watermark policy.

    Returns the exit code of the run: 0 for a measured host, 1 for a failed
    probe (the guard fails closed, it never reports a healthy host).
    """
    host = options.host
    marker = marker_path(options.pause_dir, host)
    state_file = state_path(options.pause_dir, host)
    previous = read_state(state_file)
    try:
        usage = measure_remote(host, options.path, options.ssh_binary, options.timeout)
    except ProbeError as error:
        # A failed probe is not a healthy host: fail closed and say so.
        ensure_marker(marker, host, -1, "probe-failed")
        write_state(state_file, host, "probe-failed", True, {})
        brain_row(options.brain_log, host=host, state="probe-failed",
                  action="fail-closed", detail=str(error).replace(" ", "_"))
        print(f"disk-watermark: {error}", file=sys.stderr)
        return 1
    used_pct = usage["used_pct"]
    state = decide(used_pct, options.block_pct, options.recover_pct,
                   was_blocked=bool(previous.get("blocked")))
    if state in ("recovered", "warn"):
        released = release_marker(marker) if state == "recovered" else False
        write_state(state_file, host, state, False, usage)
        if previous.get("state") != state or released:
            brain_row(options.brain_log, host=host, state=state,
                      used_pct=used_pct, used_bytes=usage["used_bytes"],
                      free_bytes=usage["free_bytes"],
                      action="release-pause" if state == "recovered" else "warn-only")
        print(f"disk-watermark: {host} used_pct={used_pct} state={state}")
        return 0
    marker_state = ensure_marker(marker, host, used_pct, state)
    write_state(state_file, host, state, True, usage)
    if previous.get("state") != state or marker_state == "created":
        action = "pause-lane" if state == "block" else "keep-pause"
        brain_row(options.brain_log, host=host, state=state, used_pct=used_pct,
                  used_bytes=usage["used_bytes"], free_bytes=usage["free_bytes"],
                  action=action)
    print(f"disk-watermark: {host} used_pct={used_pct} state={state}")
    return 0


def main(argv=None):
    home = os.path.expanduser("~")
    parser = argparse.ArgumentParser(
        description="Measure a remote lane host and pause its lanes when the disk is too full.")
    parser.add_argument("--pause-dir", default=os.path.join(home, "phanes-ctl", "autodispatch"),
                        help="the directory with the PAUSE-HOST-* markers")
    parser.add_argument("--brain-log", default=os.environ.get("DISK_WATERMARK_BRAIN_LOG", "/workspace/phanes/brain/log.md"),
                        help="the brain log the alert rows are appended to")
    parser.add_argument("--path", default="/",
                        help="the remote filesystem to measure")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="the remote lane host to measure")
    parser.add_argument("--block-pct", type=int, default=BLOCK_PCT,
                        help="the usage percentage that pauses the lane host")
    parser.add_argument("--recover-pct", type=int, default=RECOVER_PCT,
                        help="the usage percentage that releases the pause of the guard")
    parser.add_argument("--interval", type=int, default=INTERVAL,
                        help="the seconds between two measurements of the resident loop")
    parser.add_argument("--ssh-binary", default="ssh",
                        help="the ssh binary of the remote probe")
    parser.add_argument("--timeout", type=int, default=SSH_TIMEOUT,
                        help="the seconds the remote probe may take")
    parser.add_argument("--loop", action="store_true",
                        help="keep measuring the host instead of running once")
    options = parser.parse_args(argv)
    if options.recover_pct > options.block_pct:
        parser.error("--recover-pct must not be above --block-pct")
    if not options.loop:
        return run_once(options)

    running = {"stop": False}

    def stop(signum, frame):          # noqa: ARG001 - the signal handler takes the usual arguments
        running["stop"] = True

    for number in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(number, stop)
        except (OSError, ValueError):
            pass
    brain_row(options.brain_log, host=options.host, state="start",
              action="measure-loop", interval=options.interval,
              block_pct=options.block_pct, recover_pct=options.recover_pct)
    while not running["stop"]:
        try:
            run_once(options)
        except Exception as error:       # a broken tick must not kill the resident loop
            print(f"disk-watermark: the measurement tick failed: {error}", file=sys.stderr)
        deadline = time.time() + max(1, options.interval)
        while not running["stop"] and time.time() < deadline:
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
