"""Pi broker socket health, reconnect and budget-free validation (Refs #1714).

The MiniMax broker is a single point of failure on every Pi host: the Pi lane
talks to the model through one AF_UNIX socket.  A broker that died, or a socket
that went stale, took every Pi run of that host down, and the run ledger only
ever saw a vague ``exec_error``.  Recovering the host needed a human.

This module is the recovery half of the fix.  It probes the socket, restarts the
broker with a capped exponential backoff, verifies the fresh broker with a
minimal session (one tiny request over the socket: no run is created and no run
budget is spent) and keeps the state of every host, so the fleet status can
report that the Pi channel of a host is interrupted.

The module is pure: it reads no policy, starts no broker and opens no provider
connection by itself.  The caller injects the restart and the validation; the
module decides when to call them, how long to wait in between and what to
record.

Usage::

    recovery = BrokerRecovery("/run/swrm-pi/broker.sock", host="hostA",
                              restart=restart_broker, validate=minimal_session)
    status = recovery.ensure_ready()
    if not status["ok"]:
        ...  # the Pi channel of this host stays interrupted until a session validates
"""

import json
import contextlib
import fcntl
import tempfile
import os
import socket
import time

# The socket the fleet runtime exports when no lane names one.
DEFAULT_SOCKET_PATH = "/run/swrm-pi/broker.sock"

# A probe is one connect() with a short timeout: it must not slow a dispatch down.
DEFAULT_PROBE_TIMEOUT_S = 0.5

# The minimal session waits for the provider, so it gets a longer bound than a probe.
DEFAULT_SESSION_TIMEOUT_S = 30.0

# Fixed budget for restart attempts, socket waits, backoff and one confirmation.
RECOVERY_TIMEOUT_S = 30.0

# How often a dead socket is restarted before the host is declared interrupted.
DEFAULT_ATTEMPTS = 5

# The reconnect backoff: one second, doubling, capped at half a minute.
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_CAP_S = 30.0

# The reconnect failures a host may collect before the health output warns about it.
DEFAULT_ALERT_FAILURES = 5

# How long a restarted broker may need before its socket answers.
DEFAULT_SOCKET_WAIT_S = 10.0
DEFAULT_SOCKET_WAIT_INTERVAL_S = 0.2

# The outcomes of a recovery attempt.
RESULT_HEALTHY = "healthy"
RESULT_RECOVERED = "recovered"
RESULT_RECONNECT_FAILED = "reconnect_failed"
RESULT_VALIDATION_FAILED = "validation_failed"
RESULT_RESTART_UNAVAILABLE = "restart_unavailable"

# The alert the health output carries for a host whose Pi channel is interrupted.
ALERT_INTERRUPTED = "pi channel interrupted"

# The brain log prefix of a broker line.
BRAIN_PREFIX = "pi-broker"

# The smallest session the broker accepts: one short user turn and one token.
MINIMAL_SESSION_MESSAGES = [{"role": "user", "content": "ping"}]
MINIMAL_SESSION_MAX_TOKENS = 1


def probe_socket(path, timeout=DEFAULT_PROBE_TIMEOUT_S):
    """True when the broker socket accepts a connection."""
    if not path:
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(path)
        return True
    except OSError:
        return False
    finally:
        client.close()


def minimal_session(path, model=None, timeout=DEFAULT_SESSION_TIMEOUT_S, max_tokens=MINIMAL_SESSION_MAX_TOKENS):
    """Run one minimal session against the broker socket.

    The request is the smallest one the broker accepts: the configured model and
    a single short user turn.  It creates no run, so it spends no run budget; it
    only proves that the fresh broker can still serve a session.  Returns
    ``(ok, detail)``.
    """
    if not path or not model:
        return False, "no broker socket or model for the minimal session"
    body = json.dumps({"model": model, "messages": MINIMAL_SESSION_MESSAGES,
                       "max_tokens": max_tokens}).encode("utf-8")
    request = (b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\n"
               b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode("ascii") +
               b"\r\nConnection: close\r\n\r\n" + body)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, "minimal session deadline exceeded"
        client.settimeout(remaining)
        client.connect(path)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, "minimal session deadline exceeded"
        client.settimeout(remaining)
        client.sendall(request)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, "minimal session deadline exceeded"
        client.settimeout(remaining)
        head = client.recv(256)
    except OSError as error:
        return False, "minimal session failed: %s" % (error,)
    finally:
        client.close()
    if not head:
        return False, "minimal session failed: the broker closed the connection"
    status_line = head.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        return False, "minimal session failed: %s" % (status_line or "no status line",)
    status = int(parts[1])
    if 200 <= status < 300:
        return True, "minimal session answered with %d" % status
    return False, "minimal session answered with %d" % status


def backoff_delay(attempt, base=DEFAULT_BACKOFF_BASE_S, cap=DEFAULT_BACKOFF_CAP_S):
    """The wait before the next reconnect attempt: doubling, capped."""
    try:
        number = max(1, int(attempt))
    except (TypeError, ValueError):
        number = 1
    try:
        first = max(0.0, float(base))
    except (TypeError, ValueError):
        first = DEFAULT_BACKOFF_BASE_S
    try:
        limit = max(0.0, float(cap))
    except (TypeError, ValueError):
        limit = DEFAULT_BACKOFF_CAP_S
    return min(limit, first * (2 ** (number - 1)))


def brain_line(host, attempt, result, detail="", now=None, prefix=BRAIN_PREFIX):
    """One brain log line about a broker reconnect: the host, the count and the result."""
    stamp = time.strftime("%H:%M", time.localtime(now if now is not None else time.time()))
    line = "- %s UTC+8 %s: host=%s reconnect#%s result=%s" % (stamp, prefix, host, attempt, result)
    if detail:
        line += " detail=%s" % (str(detail)[:160],)
    return line


def read_state(path):
    """The recovery state of every host, from the state file."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as stream:
            state = json.load(stream)
    except (OSError, ValueError, UnicodeError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(path, state):
    """Keep the recovery state of every host; a broken write never breaks a dispatch."""
    if not path:
        return
    parent = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(parent, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=parent, prefix=".broker-state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=1)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except (OSError, TypeError, ValueError):
        pass


def host_state(state, host, socket_path=None):
    """The recovery record of one host."""
    hosts = state.get("hosts") if isinstance(state, dict) else None
    record = hosts.get(host) if isinstance(hosts, dict) else None
    if socket_path is not None and isinstance(record, dict):
        channels = record.get("channels", {})
        return channels.get(socket_path, record if record.get("socket") == socket_path else {})
    return record if isinstance(record, dict) else {}


def interrupted_hosts(state):
    """The hosts whose Pi channel is interrupted, with the reason of the last failure."""
    hosts = state.get("hosts") if isinstance(state, dict) else None
    rows = []
    for name, record in (hosts or {}).items():
        if isinstance(record, dict) and record.get("interrupted"):
            rows.append({"host": name, "result": record.get("result"), "detail": record.get("detail"),
                         "failures": record.get("failures"), "alert": record.get("alert")})
    return sorted(rows, key=lambda row: row["host"])


class BrokerRecovery:
    """Keep the Pi broker channel of one host alive: probe, reconnect, validate.

    ``restart(timeout=remaining)`` is called when the socket is gone; it may raise, and the failure
    is recorded instead of crashing the dispatch.  ``validate(timeout=remaining)`` runs the minimal
    session; it returns ``(ok, detail)``.  Both are optional: without a restart
    the module can only report that the socket is gone.
    """

    def __init__(self, socket_path, host="localhost", restart=None, validate=None,
                 attempts=DEFAULT_ATTEMPTS, backoff_base_s=DEFAULT_BACKOFF_BASE_S,
                 backoff_cap_s=DEFAULT_BACKOFF_CAP_S, alert_failures=None,
                 state_path=None, brain_log=None, sleep=time.sleep, now=time.time,
                 probe=None, socket_wait_s=DEFAULT_SOCKET_WAIT_S, wait_sleep=time.sleep,
                 clock=time.monotonic):
        self.socket_path = socket_path
        self.host = host
        self.restart = restart
        self.validate = validate
        self.attempts = max(1, int(attempts or DEFAULT_ATTEMPTS))
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.alert_failures = max(1, int(alert_failures if alert_failures is not None else self.attempts))
        self.state_path = state_path
        self.brain_log = brain_log
        self.sleep = sleep
        self.now = now
        self.probe = probe
        self.socket_wait_s = max(0.0, float(socket_wait_s or 0))
        self.wait_sleep = wait_sleep
        self.clock = clock

    # ---------------------------------------------------------------- state

    def previous_record(self):
        """The record of this host from the state file."""
        return host_state(read_state(self.state_path), self.host, self.socket_path)

    def _save(self, record):
        state = read_state(self.state_path)
        hosts = state.get("hosts") if isinstance(state.get("hosts"), dict) else {}
        previous = hosts.get(self.host, {})
        channels = dict(previous.get("channels", {}))
        if previous.get("socket") and not channels:
            channels[previous["socket"]] = {k: v for k, v in previous.items() if k != "channels"}
        channels[self.socket_path] = record
        failures = [row for row in channels.values() if row.get("interrupted")]
        hosts[self.host] = dict(failures[0] if failures else record, channels=channels)
        state["hosts"] = hosts
        state["updated_at"] = self.now()
        write_state(self.state_path, state)
        return record

    def _brain(self, attempt, result, detail):
        """Write the reconnect event into the brain log."""
        if not self.brain_log:
            return
        line = brain_line(self.host, attempt, result, detail, now=self.now())
        try:
            parent = os.path.dirname(os.path.abspath(self.brain_log))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.brain_log, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            pass

    # ---------------------------------------------------------------- recovery

    def _probe(self, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            return False
        if self.probe is not None:
            return self.probe()
        return probe_socket(self.socket_path, timeout=min(DEFAULT_PROBE_TIMEOUT_S, remaining))

    def _restart(self, deadline):
        """Restart the broker. Returns (result, detail); (None, None) means it worked."""
        if self.clock() >= deadline:
            return RESULT_RECONNECT_FAILED, "broker recovery deadline exceeded"
        if self.restart is None:
            return RESULT_RESTART_UNAVAILABLE, "no broker restart is configured for this host"
        try:
            self.restart(timeout=max(0.0, deadline - self.clock()))
        except Exception as error:
            return RESULT_RECONNECT_FAILED, "broker restart failed: %s" % (error,)
        return None, None

    def _validate_session(self, deadline):
        if self.validate is None:
            return True, "no minimal session configured"
        try:
            ok, detail = self.validate(timeout=max(0.0, deadline - self.clock()))
        except Exception as error:
            return False, "minimal session failed: %s" % (error,)
        return bool(ok), str(detail or "")

    def _wait_for_socket(self, deadline):
        """Wait for the socket of a restarted broker to answer."""
        if self.socket_wait_s <= 0:
            return self._probe(deadline)
        deadline = min(deadline, self.clock() + self.socket_wait_s)
        while True:
            if self._probe(deadline):
                return True
            if self.clock() >= deadline:
                return False
            self.wait_sleep(min(DEFAULT_SOCKET_WAIT_INTERVAL_S, max(0.0, deadline - self.clock())))

    def _attempt(self, need_validation, restart_first, deadline):
        """One attempt: make the socket answer, then validate it when the channel needs it."""
        restarted = False
        if restart_first:
            result, detail = self._restart(deadline)
            if result is not None:
                return result, detail
            restarted = True
        if not self._probe(deadline):
            if not restarted:
                result, detail = self._restart(deadline)
                if result is not None:
                    return result, detail
                restarted = True
            if not self._wait_for_socket(deadline):
                return RESULT_RECONNECT_FAILED, "broker socket is still unreachable"
        if restarted or need_validation:
            # A restarted broker has to prove itself: only a minimal session that
            # answers releases the Pi channel of the host again.
            if self.clock() >= deadline:
                return RESULT_VALIDATION_FAILED, "broker recovery deadline exceeded"
            ok, detail = self._validate_session(deadline)
            if self.clock() >= deadline:
                return RESULT_VALIDATION_FAILED, "broker recovery deadline exceeded"
            if ok:
                return RESULT_RECOVERED, detail or "minimal session answered"
            return RESULT_VALIDATION_FAILED, detail or "minimal session failed"
        return RESULT_HEALTHY, "broker socket answers"

    def _record_ok(self, attempt, result, detail):
        """The channel works again: the interruption is cleared."""
        record = {"host": self.host, "socket": self.socket_path, "ok": True, "interrupted": False,
                  "result": result, "detail": detail, "attempts": attempt, "reconnects": 0,
                  "failures": 0, "alert": None, "updated_at": self.now()}
        return self._save(record)

    def _record_failure(self, attempt, result, detail):
        """The channel is still broken: the host stays interrupted and is alerted."""
        previous = self.previous_record()
        failures = int(previous.get("failures") or 0) + 1
        reconnects = int(previous.get("reconnects") or 0) + (1 if result != RESULT_HEALTHY else 0)
        record = {"host": self.host, "socket": self.socket_path, "ok": False, "interrupted": True,
                  "result": result, "detail": detail, "attempts": attempt, "reconnects": reconnects,
                  "failures": failures, "updated_at": self.now()}
        if failures >= self.alert_failures:
            record["alert"] = ALERT_INTERRUPTED
        else:
            record["alert"] = None
        self._brain(attempt, result, detail)
        return self._save(record)

    def ensure_ready(self):
        deadline = self.clock() + RECOVERY_TIMEOUT_S
        # Serialize the whole probe/restart/validation transaction across dispatches.
        if not self.state_path:
            return self._ensure_ready(deadline)
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        with open(str(self.state_path) + ".lock", "a") as lock:
            while True:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    # Do not write shared state without owning the recovery lock.
                    return {"host": self.host, "socket": self.socket_path, "ok": False,
                            "interrupted": True, "result": "lock_timeout",
                            "detail": "broker recovery lock timeout", "attempts": 0}
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.wait_sleep(min(0.05, remaining))
            return self._ensure_ready(deadline)

    def _ensure_ready(self, deadline):
        """Probe the broker socket and recover it when it is gone.

        The probe is light: a dead socket is restarted with a capped exponential
        backoff and the fresh broker is verified with a minimal session.  The
        returned record says whether the Pi channel may be used (``ok``) and
        whether the host stays interrupted (``interrupted``).  A channel that was
        interrupted is only released by a session that answers: a socket that
        accepts a connection again is not enough.
        """
        previous = self.previous_record()
        need_validation = bool(previous.get("interrupted")) and previous.get("socket") == self.socket_path
        restart_first = False
        record = previous
        self._save(dict(previous, host=self.host, socket=self.socket_path, ok=False,
                        interrupted=True, result="checking", detail="broker validation in progress"))
        for attempt in range(1, self.attempts + 1):
            result, detail = self._attempt(need_validation, restart_first, deadline)
            if result in (RESULT_HEALTHY, RESULT_RECOVERED):
                if result == RESULT_RECOVERED:
                    self._brain(attempt, result, detail)
                return self._record_ok(attempt, result, detail)
            record = self._record_failure(attempt, result, detail)
            # A failed confirmation is terminal: never spend another session.
            if result == RESULT_VALIDATION_FAILED or self.clock() >= deadline:
                return record
            need_validation = True
            restart_first = True
            if attempt < self.attempts:
                self.sleep(min(backoff_delay(attempt, self.backoff_base_s, self.backoff_cap_s),
                               max(0.0, deadline - self.clock())))
                if self.clock() >= deadline:
                    return record
        return record
