"""Box channel failover: a second tunnel takes over when the first one dies (Refs #1717).

The box reaches the control plane through one websocket tunnel.  #1700 made the
tunnel loop rebuild a dead connection, but the link itself stayed a single
point of failure: when the tunnel service on the far side dies, rebuilding does
not help and every box lane stops at once.

This module gives the box a *second, independent* channel and moves the shim
target to it when the primary channel keeps failing:

* a channel is a tunnel endpoint (host/port) plus the probe that tells whether
  it answers.  The default probe is a plain TCP connect -- the cheapest probe
  there is, and the one that reports the "Connection refused" the field saw;
* the primary channel is used until it failed ``failure_threshold`` times in a
  row; then the backup channel is probed and, when it answers, the shim target
  moves to it (one brain log line, with the switch time and the number of
  primary failures);
* the primary channel is used again only after it answered ``recovery_threshold``
  times in a row.  That hysteresis is what keeps a flapping tunnel from
  ping-ponging between the two channels;
* the channel state (current channel, last success per channel, switch count)
  is part of the health output, so the watchdog can read it directly.

Nothing here talks to GitHub, schedules work or decides a lane state: the
module only says which tunnel the box should use right now.

Standard library only, Python 3.9+.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

__all__ = [
    "CHANNEL_BACKUP",
    "CHANNEL_PRIMARY",
    "ChannelError",
    "ChannelFailover",
    "ChannelState",
    "TunnelChannel",
    "brain_logger",
    "iso_timestamp",
    "load_state",
    "probe_tcp",
    "write_target",
]

# The two channels of a box.  The names are the keys used in the health output
# and in the brain log lines.
CHANNEL_PRIMARY = "primary"
CHANNEL_BACKUP = "backup"

# Consecutive probe failures before the shim moves to the backup channel, and
# consecutive probe successes before it moves back.  Both default to three: a
# single refused connection is a hiccup, three in a row is an outage.
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_RECOVERY_THRESHOLD = 3

# A probe is a connect and a close; two seconds is long enough for a busy
# tunnel and short enough for a health tick.
DEFAULT_PROBE_TIMEOUT = 2.0

# The brain log of the box is written in UTC+8 (see brain/lessons-37min-silence.md).
LOCAL_TZ = timezone(timedelta(hours=8))


class ChannelError(RuntimeError):
    """Raised when a channel configuration is unusable."""


def iso_timestamp(when=None):
    """Return ``when`` (epoch seconds) as an ISO-8601 stamp in UTC+8."""
    moment = time.time() if when is None else float(when)
    return datetime.fromtimestamp(moment, LOCAL_TZ).isoformat(timespec="seconds")


def probe_tcp(host, port, timeout=DEFAULT_PROBE_TIMEOUT):
    """True when the tunnel endpoint accepts a TCP connection.

    A refused connection is exactly what the field reported ("Connection
    refused" from the shim probe), so the probe stays the cheapest one there
    is: connect, close, done.
    """
    try:
        with socket.create_connection((str(host), int(port)), timeout=float(timeout)):
            return True
    except (OSError, TypeError, ValueError):
        return False


class TunnelChannel:
    """One tunnel endpoint of the box: where the shim points and how to probe it.

    ``probe`` is optional; without one the channel is probed with a TCP
    connect.  A custom probe receives the channel and returns a truthy value
    when the channel answers.  A probe that raises is a failed probe, never a
    crashed loop.
    """

    def __init__(self, name, host, port, probe=None, timeout=DEFAULT_PROBE_TIMEOUT):
        if not isinstance(name, str) or not name.strip():
            raise ChannelError("channel name must be a non-empty string")
        if not isinstance(host, str) or not host.strip():
            raise ChannelError("channel %s: host must be a non-empty string" % name)
        try:
            self.port = int(port)
        except (TypeError, ValueError):
            raise ChannelError("channel %s: port must be a number" % name)
        if not 0 < self.port < 65536:
            raise ChannelError("channel %s: port must be between 1 and 65535" % name)
        self.name = name.strip()
        self.host = host.strip()
        self.timeout = float(timeout)
        self.last_error: Optional[str] = None
        self._probe = probe

    @property
    def target(self):
        """The endpoint the shim connects to while this channel is in use."""
        return "%s:%d" % (self.host, self.port)

    def check(self):
        """True when the channel answers right now; a probe error is a failure."""
        try:
            alive = self._probe(self) if self._probe is not None else probe_tcp(
                self.host, self.port, self.timeout)
        except Exception as error:  # a broken probe must not kill the control loop
            self.last_error = "%s: %s" % (type(error).__name__, error)
            return False
        if alive:
            self.last_error = None
            return True
        self.last_error = self.last_error or "probe failed"
        return False

    def __repr__(self):
        return "TunnelChannel(%r, %r)" % (self.name, self.target)


class ChannelState:
    """The counters of one channel: failure streak, success streak, timestamps."""

    def __init__(self, name):
        self.name = name
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.last_success_at: Optional[float] = None
        self.last_failure_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.reachable: Optional[bool] = None

    def record_success(self, now):
        self.consecutive_failures = 0
        self.consecutive_successes += 1
        self.last_success_at = now
        self.reachable = True
        self.last_error = None

    def record_failure(self, now, error=None):
        self.consecutive_successes = 0
        self.consecutive_failures += 1
        self.last_failure_at = now
        self.reachable = False
        if error:
            self.last_error = error

    def snapshot(self):
        """The channel state as it appears in the health output."""
        return {"name": self.name,
                "reachable": self.reachable,
                "consecutive_failures": self.consecutive_failures,
                "consecutive_successes": self.consecutive_successes,
                "last_success_at": self.last_success_at,
                "last_failure_at": self.last_failure_at,
                "last_error": self.last_error}


class ChannelFailover:
    """Keep the box on the primary tunnel until it fails, then use the backup.

    One :meth:`tick` probes the channels once and switches when the thresholds
    say so:

    * on the primary channel, the primary is probed; once it failed
      ``failure_threshold`` times in a row the backup is probed too and the
      shim target moves to the backup when the backup answers;
    * on the backup channel, both channels are probed; the shim target moves
      back to the primary only after the primary answered ``recovery_threshold``
      times in a row.

    Every switch appends one brain log line and bumps the switch count.
    """

    def __init__(self, primary: TunnelChannel, backup: TunnelChannel,
                 failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
                 recovery_threshold: int = DEFAULT_RECOVERY_THRESHOLD,
                 brain_log: Optional[Callable[[str], None]] = None,
                 clock: Optional[Callable[[], float]] = None):
        if primary is None or backup is None:
            raise ChannelError("a box needs a primary and a backup channel")
        if primary.name == backup.name:
            raise ChannelError("the two channels must have different names")
        if int(failure_threshold) < 1 or int(recovery_threshold) < 1:
            raise ChannelError("thresholds must be positive")
        self.primary = primary
        self.backup = backup
        self.failure_threshold = int(failure_threshold)
        self.recovery_threshold = int(recovery_threshold)
        self.brain_log = brain_log
        self.clock = clock or time.time
        self.current = primary.name
        self.switch_count = 0
        self.last_switch_at: Optional[float] = None
        self.last_switch: Optional[Dict[str, Any]] = None
        self.state: Dict[str, ChannelState] = {
            primary.name: ChannelState(primary.name),
            backup.name: ChannelState(backup.name),
        }

    # ------------------------------------------------------------------ state

    def channel(self, name):
        """The channel object of ``name``."""
        if name == self.primary.name:
            return self.primary
        if name == self.backup.name:
            return self.backup
        raise ChannelError("unknown channel: %s" % name)

    def target(self):
        """The endpoint the shim must connect to right now."""
        return self.channel(self.current).target

    def health(self):
        """The channel state for the health output.

        The watchdog reads the current channel, the last success of each
        channel and the switch count straight from this block.
        """
        return {"current": self.current,
                "target": self.target(),
                "switch_count": self.switch_count,
                "last_switch_at": self.last_switch_at,
                "last_switch": self.last_switch,
                "channels": {name: state.snapshot() for name, state in self.state.items()}}

    def state_dict(self):
        """The persisted state: the health block plus the update time."""
        payload = self.health()
        payload["updated_at"] = self.clock()
        return payload

    def restore(self, payload):
        """Restore the counters of a saved state, so a restart keeps the channel."""
        if not isinstance(payload, dict):
            raise ChannelError("channel state must be a JSON object")
        current = payload.get("current")
        if current in (self.primary.name, self.backup.name):
            self.current = current
        try:
            self.switch_count = int(payload.get("switch_count") or 0)
        except (TypeError, ValueError):
            self.switch_count = 0
        last_switch_at = payload.get("last_switch_at")
        self.last_switch_at = float(last_switch_at) if isinstance(last_switch_at, (int, float)) else None
        last_switch = payload.get("last_switch")
        self.last_switch = dict(last_switch) if isinstance(last_switch, dict) else None
        channels = payload.get("channels")
        if isinstance(channels, dict):
            for name, record in channels.items():
                state = self.state.get(name)
                if state is None or not isinstance(record, dict):
                    continue
                state.consecutive_failures = int(record.get("consecutive_failures") or 0)
                state.consecutive_successes = int(record.get("consecutive_successes") or 0)
                state.last_success_at = record.get("last_success_at")
                state.last_failure_at = record.get("last_failure_at")
                state.last_error = record.get("last_error")
                state.reachable = record.get("reachable")
        return self

    # ------------------------------------------------------------------- tick

    def tick(self):
        """Probe the channels once; returns the switch event or None.

        The event carries the switch time, the channel it came from and went
        to, the reason and the failure (or success) streak of the primary
        channel that caused it.
        """
        now = self.clock()
        if self.current == self.primary.name:
            return self._tick_primary(now)
        return self._tick_backup(now)

    def _tick_primary(self, now):
        if self._probe(self.primary, now):
            return None
        failures = self.state[self.primary.name].consecutive_failures
        if failures < self.failure_threshold:
            return None
        # The primary is down: the backup has to answer before the shim moves.
        if not self._probe(self.backup, now):
            return None
        return self._switch(self.backup, now,
                            reason="primary probe failed %d consecutive times" % failures,
                            primary_failures=failures, primary_successes=0)

    def _tick_backup(self, now):
        # The backup keeps its own counters fresh, and the primary is probed
        # for its recovery.
        self._probe(self.backup, now)
        if not self._probe(self.primary, now):
            return None
        successes = self.state[self.primary.name].consecutive_successes
        if successes < self.recovery_threshold:
            return None
        return self._switch(self.primary, now,
                            reason="primary probe succeeded %d consecutive times" % successes,
                            primary_failures=0, primary_successes=successes)

    def _probe(self, channel, now):
        alive = channel.check()
        state = self.state[channel.name]
        if alive:
            state.record_success(now)
        else:
            state.record_failure(now, channel.last_error)
        return alive

    # ----------------------------------------------------------------- switch

    def _switch(self, channel, now, reason, primary_failures, primary_successes):
        previous = self.current
        self.current = channel.name
        self.switch_count += 1
        self.last_switch_at = now
        event = {"event": "channel_switch",
                 "at": iso_timestamp(now),
                 "ts": now,
                 "from": previous,
                 "to": channel.name,
                 "reason": reason,
                 "primary_failures": primary_failures,
                 "primary_successes": primary_successes,
                 "primary_last_error": self.state[self.primary.name].last_error,
                 "switch_count": self.switch_count,
                 "target": channel.target}
        self.last_switch = event
        self._log(event)
        return event

    def _log(self, event):
        if self.brain_log is None:
            return
        message = ("switch %s->%s at %s after %s"
                   % (event["from"], event["to"], event["at"], event["reason"]))
        error = event.get("primary_last_error")
        if error:
            message += " (last error: %s)" % error
        self.brain_log(message)


def brain_logger(path, prefix="box-channel"):
    """Return a callable that appends one brain log line per call.

    The line has the same shape as the other box brain log lines
    (``- HH:MM UTC+8 <prefix>: <message>``), so the switch history can be read
    in the same file as the rest of the control plane history.
    """
    def write(message):
        line = "- %s UTC+8 %s: %s\n" % (time.strftime("%H:%M"), prefix, message)
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(line)
    return write


def write_target(path, health):
    """Write the shim target (the endpoint in use) where the shim reads it."""
    payload = {"channel": health.get("current"),
               "target": health.get("target"),
               "switch_count": health.get("switch_count"),
               "updated_at": health.get("updated_at", time.time())}
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return payload


def load_state(path):
    """Read a saved channel state; a missing or broken file is an empty state."""
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
