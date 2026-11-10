"""Reproduction of #1697 (reader side): the health output must judge the snapshot.

``~/phanes-ctl/autodispatch/lanes-status.json`` is the queue watermark of the control
plane: the pool level, the number of parallel lanes and every supervisor decision are
taken from it.  On 2026-11-02 the ``_at`` stamp froze at 22:13:20 while the dispatcher
kept dispatching, and no reader noticed: the health output only reported the hosts.

The issue asks for a self check on the reader side:

* a snapshot whose ``_at`` is older than the threshold (3 minutes) is reported as
  expired, together with the last ``_at``;
* the health check also verifies that the writer (the dispatcher) is still alive;
* both are alarms: the health output must not report a healthy control plane while the
  snapshot is frozen.

The repository ships no such reader on the base commit: the lanes-status snapshot is
written by ``tools/lane-autodispatch/autodispatch.py`` and read by nobody.  The tests
below look for the probe that reports the verdict and fail when there is none.

The probe is looked up by the naming conventions of the repository (an offline
``tools/check-*.py`` probe, like ``tools/check-ad-heartbeat-stall.py`` for the
heartbeat, or a ``swrm fleet lanes-health`` command).  A probe that answers the questions
below satisfies the contract, whatever its name is.
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]

# The stamp the issue reports: the dispatcher froze there on 2026-11-02.
STALLED_AT = "2026-11-02 22:13:20"

# The freshness threshold of the issue (3 minutes).
THRESHOLD_MINUTES = 3

# The words a probe may use to report an expired snapshot.
STALE_WORDS = ("stale", "expired", "outdated", "frozen", "过期")

# The words a probe may use to report a dispatcher that is not alive any more.
DEAD_WORDS = (r"\bdead\b", "not alive", "not running", "已退出", "已死亡")

# The words that show that a probe read the snapshot and judged it.
VERDICT_WORDS = STALE_WORDS + ("fresh", "dead")

# A probe that does not understand the arguments prints a usage error.
USAGE_ERRORS = ("unrecognized arguments", "invalid choice", "usage:", "error: argument")

# The probes the repository may ship.  The first list is the documented naming
# convention, the glob catches a probe with another name.
NAMED_PROBES = (
    "tools/check-lanes-status-stall.py",
    "tools/check-lanes-status-stale.py",
    "tools/check-lanes-status-freshness.py",
    "tools/check-lanes-snapshot-stall.py",
    "tools/check-ad-lanes-stall.py",
    "tools/check-lanes-status-health.py",
    "tools/lanes-status-health.py",
)

# The command line spellings a probe may use.  The first one that the probe
# understands wins.
ARGUMENT_SETS = (
    ("--lanes-status-file", "--threshold-minutes"),
    ("--lanes-status-file", "--stale-minutes"),
    ("--lanes-status-file", "--threshold-seconds"),
    ("--snapshot-file", "--threshold-minutes"),
    ("--file", "--threshold-minutes"),
    ("--lanes-status-file", None),
    ("--snapshot-file", None),
    (None, "--threshold-minutes"),
    (None, None),
)


def candidate_probes():
    """The commands that may report the freshness of the snapshot."""
    probes = []
    for name in NAMED_PROBES:
        path = ROOT / name
        if path.is_file():
            probes.append([sys.executable, str(path)])
    for path in sorted((ROOT / "tools").glob("*lanes*.py")):
        argv = [sys.executable, str(path)]
        if argv not in probes:
            probes.append(argv)
    swrm = ROOT / "swrm" / "bin" / "swrm"
    if swrm.is_file():
        probes.append([sys.executable, str(swrm), "fleet", "lanes-health", "--json"])
    return probes


def build_argv(argv, snapshot, file_flag, threshold_flag):
    """The command line of one probe, with the snapshot and the threshold."""
    command = list(argv)
    if file_flag:
        command += [file_flag, str(snapshot)]
    if threshold_flag:
        value = str(THRESHOLD_MINUTES if "minute" in threshold_flag else THRESHOLD_MINUTES * 60)
        command += [threshold_flag, value]
    return command


def run_probe(argv, home, timeout=20):
    """Run one probe in an isolated home; returns (returncode, output)."""
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("OPENSWARM_", "XSWARM_"))}
    env["HOME"] = str(home)
    env["TMPDIR"] = str(home)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=env, cwd=str(ROOT))
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"{argv} could not be run: {error}"
    return proc.returncode, proc.stdout + proc.stderr


def probe_verdict(home, snapshot, stamp):
    """Ask every known probe for its verdict about the snapshot.

    Returns (returncode, output) of the first probe that judged the snapshot, or
    (None, "") when the repository ships no such probe.
    """
    for argv in candidate_probes():
        for file_flag, threshold_flag in ARGUMENT_SETS:
            command = build_argv(argv, snapshot, file_flag, threshold_flag)
            code, output = run_probe(command, home)
            if code is None or any(marker in output for marker in USAGE_ERRORS):
                continue
            if stamp in output or any(word in output.lower() for word in VERDICT_WORDS):
                return code, output
    return None, ""


def dead_pid():
    """A pid that is not running any more."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class LanesStatusHealthProbeTest(unittest.TestCase):
    """The health output of the control plane must judge the snapshot it reads."""

    def fixture(self, directory, stamp):
        """The deployed layout of the control plane in an isolated home."""
        home = pathlib.Path(directory)
        state = home / "phanes-ctl" / "autodispatch"
        state.mkdir(parents=True)
        snapshot = state / "lanes-status.json"
        snapshot.write_text(json.dumps({"_at": stamp, "_ready": 4, "_busy": 1}),
                             encoding="utf-8")
        # the heartbeat of the dispatcher loop: it is fresh
        (state / "heartbeat").write_text(time.strftime("%F %T") + "\n", encoding="utf-8")
        return home, snapshot

    def test_a_stalled_snapshot_is_reported_as_expired(self):
        """The 22:13 sample: the health output must mark the snapshot as expired."""
        with tempfile.TemporaryDirectory() as directory:
            home, snapshot = self.fixture(directory, STALLED_AT)
            code, output = probe_verdict(home, snapshot, STALLED_AT)
            self.assertIsNotNone(code, self.missing_probe_message())
            self.assertNotEqual(code, 0,
                                 f"a frozen snapshot must raise an alarm, got exit {code}: {output}")
            self.assertTrue(any(word in output.lower() for word in STALE_WORDS),
                            f"the health output does not mark the snapshot as expired: {output}")
            self.assertIn(STALLED_AT, output,
                          f"the health output does not name the last _at: {output}")

    def test_a_fresh_snapshot_is_not_reported_as_expired(self):
        """A snapshot that the dispatcher just refreshed is not an alarm."""
        with tempfile.TemporaryDirectory() as directory:
            stamp = time.strftime("%F %T")
            home, snapshot = self.fixture(directory, stamp)
            code, output = probe_verdict(home, snapshot, stamp)
            self.assertIsNotNone(code, self.missing_probe_message())
            self.assertEqual(code, 0, f"a fresh snapshot must not raise an alarm: {output}")

    def test_a_dead_dispatcher_is_reported_even_with_a_fresh_heartbeat(self):
        """The heartbeat is not proof of life: a dead pid must be reported."""
        with tempfile.TemporaryDirectory() as directory:
            stamp = time.strftime("%F %T")
            home, snapshot = self.fixture(directory, stamp)
            record = json.dumps({"pid": dead_pid(), "started_at": time.time()})
            for path in (home / "phanes-ctl" / "state" / "autodispatch.pid",
                         home / "phanes-ctl" / "autodispatch" / "autodispatch.pid"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(record, encoding="utf-8")
            code, output = probe_verdict(home, snapshot, stamp)
            self.assertIsNotNone(code, self.missing_probe_message())
            self.assertNotEqual(code, 0,
                                 f"a dead dispatcher must raise an alarm, got exit {code}: {output}")
            self.assertTrue(any(re.search(word, output, re.I) for word in DEAD_WORDS),
                            f"the health output does not report the dead dispatcher: {output}")

    def missing_probe_message(self):
        """What the repository has to ship for these tests to pass."""
        return (
            "the repository ships no probe that judges the lanes-status snapshot. "
            "The health output must mark a snapshot whose _at is older than the threshold "
            f"({THRESHOLD_MINUTES} minutes) as expired, name the last _at and alarm "
            "(a non-zero exit), and it must report a dispatcher that is not alive any more. "
            "Expected an offline probe under tools/ (like tools/check-ad-heartbeat-stall.py "
            "for the heartbeat) or a `swrm fleet lanes-health` command.")


if __name__ == "__main__":
    unittest.main()
