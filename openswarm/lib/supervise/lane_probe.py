"""Lane server health probe: a wedged lane server is restarted and verified (Refs #1709).

Background (2026-11-02, CX9 / gores2): four consecutive runs on four different cards
failed within one or two minutes without a commit and without an error class.  On the
lane host the ``codex app-server`` process had been running since 01:04 and the lane
serve process was alive, but new sessions never started: the server was wedged.  The
control plane had no health probe for the lane server itself, so the failure only
became visible after a run had already failed.

This module owns the *decision*; the lane supervisor owns the processes:

``settings(policy)``
    The probe thresholds of a policy (``lane.server_probe``).  ``interval_s`` of 0
    turns the probe off.

``ServerProbe``
    The bookkeeping of one lane server: consecutive failures, the last successful
    probe, the restart count, and the moment the server counts as wedged.

``restart_message(...)`` / ``brain_line(...)``
    The line the lane writes into the brain log when it restarts a wedged server
    (lane, uptime before the restart, and the basis of the decision).

``status(...)``
    The probe fields the health output carries for every codex lane.

Standard library only, Python 3.9+.
"""
import math
import time

__all__ = [
    "BRAIN_PREFIX",
    "DEFAULT_FAILURES",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_METHOD",
    "DEFAULT_RESTART_LIMIT",
    "DEFAULT_RESTART_WINDOW_S",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_VERIFY_TIMEOUT_S",
    "ServerProbe",
    "brain_line",
    "probe_from",
    "restart_message",
    "settings",
    "status",
]

DEFAULT_INTERVAL_S = 120
DEFAULT_TIMEOUT_S = 20
DEFAULT_FAILURES = 3
DEFAULT_VERIFY_TIMEOUT_S = 180
DEFAULT_RESTART_LIMIT = 3
DEFAULT_RESTART_WINDOW_S = 3600
DEFAULT_METHOD = "initialize"
BRAIN_PREFIX = "lane-probe"


def _number(value, fallback, minimum=0.0):
    """A finite number from a policy value, or the fallback when the value is unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    if not math.isfinite(value) or value < minimum:
        return fallback
    return float(value)


def _count(value):
    """A counter from a state file; a damaged value counts as nothing."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def settings(policy):
    """The probe settings of a policy (``lane.server_probe``).

    An unusable value falls back to the built-in default instead of disabling the
    probe: a broken policy must not silently stop the health check.
    """
    lane = (policy or {}).get("lane") or {}
    cfg = lane.get("server_probe") if isinstance(lane, dict) else None
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "interval_s": _number(cfg.get("interval_s"), DEFAULT_INTERVAL_S),
        "timeout_s": _number(cfg.get("timeout_s"), DEFAULT_TIMEOUT_S, minimum=1.0),
        "failures": int(_number(cfg.get("failures"), DEFAULT_FAILURES, minimum=1.0)),
        "verify_timeout_s": _number(cfg.get("verify_timeout_s"), DEFAULT_VERIFY_TIMEOUT_S, minimum=1.0),
        "restart_limit": int(_number(cfg.get("restart_limit"), DEFAULT_RESTART_LIMIT, minimum=0.0)),
        "restart_window_s": _number(cfg.get("restart_window_s"), DEFAULT_RESTART_WINDOW_S, minimum=1.0),
        "method": str(cfg.get("method") or DEFAULT_METHOD),
    }


class ServerProbe:
    """The probe bookkeeping of one lane server.

    The probe asks the server whether it still answers.  A server that misses the
    probe ``failures`` times in a row counts as wedged: it is restarted and verified
    with an empty run before the lane takes work again.
    """

    def __init__(self, failures=DEFAULT_FAILURES, restart_limit=DEFAULT_RESTART_LIMIT,
                 restart_window_s=DEFAULT_RESTART_WINDOW_S):
        self.threshold = max(1, int(failures or DEFAULT_FAILURES))
        self.restart_limit = max(0, int(restart_limit or 0))
        self.restart_window_s = float(restart_window_s or DEFAULT_RESTART_WINDOW_S)
        self.failures = 0
        self.restarts = 0
        self.alive = None
        self.last_success_at = None
        self.last_check_at = None
        self.last_error = None
        self.restarted_at = []

    # ------------------------------------------------------------------ probes
    def record_success(self, now):
        """A probe answered: the server is alive and the failure streak is over."""
        self.failures = 0
        self.alive = True
        self.last_success_at = float(now)
        self.last_check_at = float(now)
        self.last_error = None

    def record_failure(self, now, reason):
        """A probe did not answer: the failure streak grows by one."""
        self.failures += 1
        self.alive = False
        self.last_check_at = float(now)
        self.last_error = str(reason or "no answer")

    def wedged(self):
        """Whether the server missed the probe often enough to count as wedged."""
        return self.failures >= self.threshold

    # ---------------------------------------------------------------- restarts
    def note_restart(self, now):
        """Record a restart of the lane server."""
        self.restarts += 1
        self.restarted_at.append(float(now))
        self.failures = 0
        self.last_error = None
        return self.restarts

    def record_recovered(self, now):
        """The restarted server passed the empty-run verification."""
        self.failures = 0
        self.alive = True
        self.last_success_at = float(now)
        self.last_check_at = float(now)
        self.last_error = None

    def restart_storm(self, now):
        """Whether the lane used up its restarts inside the restart window.

        A lane that keeps wedging is not restarted forever: the lane stays paused
        and asks for a human instead of looping.
        """
        if self.restart_limit <= 0:
            return False
        moment = float(now)
        self.restarted_at = [stamp for stamp in self.restarted_at
                             if moment - stamp <= self.restart_window_s]
        return len(self.restarted_at) >= self.restart_limit

    # ------------------------------------------------------------------- state
    def to_dict(self):
        """The probe state as the lane state file keeps it."""
        return {
            "alive": self.alive,
            "failures": self.failures,
            "restarts": self.restarts,
            "last_success_at": self.last_success_at,
            "last_check_at": self.last_check_at,
            "last_error": self.last_error,
            "restarted_at": list(self.restarted_at),
        }

    @classmethod
    def from_dict(cls, data, failures=DEFAULT_FAILURES, restart_limit=DEFAULT_RESTART_LIMIT,
                  restart_window_s=DEFAULT_RESTART_WINDOW_S):
        """Restore a probe from the lane state file; a damaged state starts over."""
        probe = cls(failures=failures, restart_limit=restart_limit,
                     restart_window_s=restart_window_s)
        if not isinstance(data, dict):
            return probe
        probe.failures = max(0, _count(data.get("failures")))
        probe.restarts = max(0, _count(data.get("restarts")))
        alive = data.get("alive")
        probe.alive = alive if isinstance(alive, bool) else None
        for key in ("last_success_at", "last_check_at"):
            value = data.get(key)
            setattr(probe, key, float(value)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) else None)
        error = data.get("last_error")
        probe.last_error = str(error) if isinstance(error, str) else None
        stamps = data.get("restarted_at")
        probe.restarted_at = [float(stamp) for stamp in stamps
                              if isinstance(stamp, (int, float)) and not isinstance(stamp, bool)] \
            if isinstance(stamps, list) else []
        return probe


def probe_from(policy, data=None):
    """The probe of one lane: the policy thresholds plus the state of the lane."""
    conf = settings(policy)
    return ServerProbe.from_dict(data, failures=conf["failures"],
                                  restart_limit=conf["restart_limit"],
                                  restart_window_s=conf["restart_window_s"])


def brain_line(lane, message, now=None, prefix=BRAIN_PREFIX):
    """The brain log line of a lane probe event.

    The dispatch engine writes its short lines in the same shape
    (``- HH:MM UTC+8 dispatch: ...``), so the lane lines read the same way.
    """
    stamp = time.strftime("%H:%M", time.localtime(float(now if now is not None else time.time())))
    return f"- {stamp} UTC+8 {prefix}: {lane} {message}"


def restart_message(uptime_s, reason, failures):
    """The brain log line of a wedged lane server.

    It names the three things an operator needs: how long the server ran before the
    restart, and the basis of the decision (the probe failures).
    """
    seconds = max(0, int(round(float(uptime_s or 0))))
    return (f"lane server wedged after {failures} failed probes "
            f"(uptime {seconds}s, basis: {reason or 'no answer'}); restarting the lane server")


def status(data):
    """The probe fields the health output carries for one lane."""
    if not isinstance(data, dict):
        data = {}
    alive = data.get("alive")
    return {
        "alive": alive if isinstance(alive, bool) else None,
        "last_success_at": data.get("last_success_at"),
        "last_check_at": data.get("last_check_at"),
        "restarts": max(0, _count(data.get("restarts"))),
        "failures": max(0, _count(data.get("failures"))),
        "last_error": data.get("last_error"),
    }
