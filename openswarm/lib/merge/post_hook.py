"""Post-merge hook: run a command once per new sha of `main` (Refs #1446).

After a merge lands on `main` the workspace has to be refreshed (build, install,
deploy, ...).  Running that on every merge is wasteful, and running it after a
*failed* merge is worse: a half applied hook leaves the workspace broken.

This module owns the decision, not the wiring:

* the hook only runs when `main` moved -- the sha of the last successful run is
  remembered in a small JSON state file,
* a failed run is remembered too and is retried on the next round; the version
  that worked stays in place (the hook never rolls anything back),
* repeated failures on the same sha back off exponentially, so a hook that is
  broken does not eat the budget of every following merge.

The command is an argv list and is executed without a shell, so nothing in the
sha or in the arguments can be interpreted by a shell.

State file format::

    {"last_ok_sha": "abc123", "last_fail_sha": "abc123",
     "fail_count": 2, "last_error": "boom", "last_fail_at": 1700000000.0}

``last_ok_sha`` is the sha of the last successful run, ``last_fail_sha`` the sha
of the last failed one, ``fail_count`` the number of consecutive failures of
that sha (``last_error`` is cleared by a success), ``last_error`` the tail of the
stderr of the last failure and ``last_fail_at`` the moment of that failure (the
backoff window is measured from it).

A damaged state file is never trusted partially: one unusable field makes the
whole file damaged and the hook then starts over with an empty state, so the
hook runs again instead of skipping a sha it cannot vouch for.

The file is written atomically (temporary file + ``os.replace``), so a crashed
writer can never leave a half written state file behind.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Sequence

# The outcomes `maybe_run` reports.
STATUS_OK = "ok"            # the command ran and exited 0
STATUS_FAILED = "failed"    # the command ran and failed (non zero exit or timeout)
STATUS_SKIPPED = "skipped"  # the sha did not move since the last successful run
STATUS_BACKOFF = "backoff"  # too many failures on this sha, waiting for the window

# The environment variable that carries the sha of `main` to the hook.
SHA_ENV_VAR = "OPENSWARM_MAIN_SHA"

# How many consecutive failures on the same sha are tolerated before the hook
# starts to back off.
BACKOFF_MIN_FAILS = 3
# The backoff window is 2**fail_count * BACKOFF_BASE_S seconds, capped here.
BACKOFF_BASE_S = 60
BACKOFF_MAX_S = 3600
# The stderr of a failed run is truncated to its last characters.
ERROR_TAIL_CHARS = 500
# The default timeout of one hook run.
DEFAULT_TIMEOUT_S = 600


def fresh_state() -> Dict[str, Any]:
    """The empty state: no run happened yet."""
    return {
        "last_ok_sha": None,
        "last_fail_sha": None,
        "fail_count": 0,
        "last_error": None,
        "last_fail_at": None,
    }


def load_state(state_path: Any) -> Dict[str, Any]:
    """Read the state file; a missing or damaged file is an empty state."""
    try:
        with open(state_path, encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError):
        return fresh_state()
    if not isinstance(raw, dict):
        return fresh_state()
    state = fresh_state()
    for key in ("last_ok_sha", "last_fail_sha"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            return fresh_state()
        state[key] = value or None
    count = raw.get("fail_count", 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return fresh_state()
    state["fail_count"] = count
    error = raw.get("last_error")
    if error is not None and not isinstance(error, str):
        return fresh_state()
    state["last_error"] = error
    last_fail_at = raw.get("last_fail_at")
    if last_fail_at is not None:
        if isinstance(last_fail_at, bool) or not isinstance(last_fail_at, (int, float)):
            return fresh_state()
        last_fail_at = float(last_fail_at)
        if not math.isfinite(last_fail_at):
            return fresh_state()
    state["last_fail_at"] = last_fail_at
    return state


def save_state(state_path: Any, state: Dict[str, Any]) -> None:
    """Write the state file atomically (temporary file + ``os.replace``)."""
    target = os.path.abspath(str(state_path))
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=os.path.basename(target) + ".",
        suffix=".tmp",
        delete=False,
    )
    temporary = handle.name
    try:
        with handle:
            json.dump(state, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def backoff_window(fail_count: Any) -> float:
    """The backoff window of a failure streak, in seconds.

    The window doubles with every failure of the same sha (2**fail_count
    minutes) and never grows beyond :data:`BACKOFF_MAX_S`.
    """
    try:
        count = int(fail_count)
    except (TypeError, ValueError):
        return 0.0
    if count <= 0:
        return 0.0
    # The exponent is capped: 2**16 * 60 already is far beyond the maximum.
    exponent = min(count, 16)
    return float(min(2 ** exponent * BACKOFF_BASE_S, BACKOFF_MAX_S))


def _sha(main_sha: Any) -> str:
    """The sha of `main` as text; an empty sha is a bug of the caller."""
    if main_sha is None:
        raise ValueError("main_sha must be a sha, not None")
    text = str(main_sha).strip()
    if not text:
        raise ValueError("main_sha must not be empty")
    return text


def _error_tail(stderr: Any) -> str:
    """The last :data:`ERROR_TAIL_CHARS` characters of the stderr of a run.

    The stderr may be text or bytes: a timeout carries the raw output of the
    process, which is bytes even when the run asked for text.  Bytes are
    decoded (undecodable bytes are replaced) instead of being dropped, so the
    error of a timeout stays useful.
    """
    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", errors="replace")
    elif isinstance(stderr, str):
        text = stderr
    else:
        text = ""
    return text[-ERROR_TAIL_CHARS:]


class PostMergeHook:
    """Run a command once per new sha of `main`, with retries and a backoff.

    ``cmd`` is the argv list of the command (no shell is involved), ``state_path``
    is the JSON file that remembers what happened, ``timeout_s`` bounds one run
    and ``runner`` is the function that runs the command (``subprocess.run`` by
    default; the tests pass a fake one).

    The runner is called as::

        runner(cmd, env=env, timeout=timeout_s, capture_output=True, text=True)

    and has to return an object with a ``returncode`` and a ``stderr``.
    """

    def __init__(
        self,
        cmd: Sequence[str],
        state_path: Any,
        timeout_s: Any = DEFAULT_TIMEOUT_S,
        runner: Any = subprocess.run,
    ) -> None:
        if isinstance(cmd, (str, bytes)):
            raise ValueError("cmd must be an argv list, not a string (no shell is used)")
        self.cmd: List[str] = [str(part) for part in cmd]
        if not self.cmd:
            raise ValueError("cmd must not be empty")
        self.state_path = str(state_path)
        self.timeout_s = float(timeout_s)
        self.runner = runner

    # ----------------------------------------------------------------- state

    def state(self) -> Dict[str, Any]:
        """The state as it is on disk (empty when the file is missing or damaged)."""
        return load_state(self.state_path)

    # ------------------------------------------------------------------- run

    def maybe_run(self, main_sha: Any, now: Any = None) -> str:
        """Run the hook when `main` moved and report what happened.

        The hook is skipped when `main_sha` is the sha of the last successful
        run.  A sha that failed is retried on the next round; after
        :data:`BACKOFF_MIN_FAILS` failures on the same sha the hook waits for
        the backoff window to pass before it tries again.
        """
        sha = _sha(main_sha)
        moment = time.time() if now is None else float(now)
        state = load_state(self.state_path)
        if state["last_ok_sha"] == sha:
            return STATUS_SKIPPED
        if self._backing_off(state, sha, moment):
            return STATUS_BACKOFF
        try:
            completed = self.runner(
                list(self.cmd),
                env=self._env(sha),
                timeout=self.timeout_s,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as expired:
            return self._failed(state, sha, moment, self._timeout_error(expired))
        except OSError as error:
            # The command could not even be started: that is a failed run too.
            return self._failed(state, sha, moment, str(error))
        returncode = getattr(completed, "returncode", 0)
        if returncode is None:
            returncode = 0
        if returncode == 0:
            state["last_ok_sha"] = sha
            state["fail_count"] = 0
            state["last_error"] = None
            save_state(self.state_path, state)
            return STATUS_OK
        return self._failed(state, sha, moment, _error_tail(getattr(completed, "stderr", "")))

    # --------------------------------------------------------------- helpers

    def _env(self, sha: str) -> Dict[str, str]:
        """The environment of the hook: the one of the caller plus the sha."""
        env = dict(os.environ)
        env[SHA_ENV_VAR] = sha
        return env

    def _timeout_error(self, expired: BaseException) -> str:
        """The error of a run that hit the timeout."""
        detail = _error_tail(getattr(expired, "stderr", ""))
        message = "timeout after %gs" % self.timeout_s
        return message + (": " + detail if detail else "")

    def _backing_off(self, state: Dict[str, Any], sha: str, moment: float) -> bool:
        """True while the failures of this sha still hold the hook back."""
        if state["last_fail_sha"] != sha:
            return False
        if state["fail_count"] < BACKOFF_MIN_FAILS:
            return False
        last_fail_at = state["last_fail_at"]
        if last_fail_at is None:
            return False
        return (moment - last_fail_at) <= backoff_window(state["fail_count"])

    def _failed(self, state: Dict[str, Any], sha: str, moment: float, error: str) -> str:
        """Remember a failed run and report it.

        The sha of the last success stays untouched: the version that worked
        keeps running until the hook succeeds again.
        """
        same_sha = state["last_fail_sha"] == sha
        state["last_fail_sha"] = sha
        state["fail_count"] = state["fail_count"] + 1 if same_sha else 1
        state["last_error"] = error
        state["last_fail_at"] = moment
        save_state(self.state_path, state)
        return STATUS_FAILED
