"""Shell-timeout regression tests for the e2e runner.

The runner starts the visible and hidden test commands through ``run_shell``,
which calls ``subprocess.run(..., shell=True)``. When such a command exceeds
its timeout only the shell itself is killed; the background children it
started keep running, keep writing into the work tree and hold resources that
pollute the following benchmark cells.

The tests below drive ``run_shell`` with a command that leaves a background
child behind and check that the whole process group is gone once the timeout
was reported, that the command runs isolated from the runner, and that the
captured stdout/stderr tails are preserved.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from run import run_shell  # noqa: E402

TIMEOUT_S = 2
SLEEP_S = 30
WRITER_SOURCE = """\
import os
import sys
import time

marker, pidfile = sys.argv[1], sys.argv[2]
with open(pidfile, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
with open(marker, "a", encoding="utf-8") as handle:
    while True:
        handle.write("tick\\n")
        handle.flush()
        time.sleep(0.05)
"""
PROBE_SOURCE = """\
import os
import sys

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write(f"{os.getpid()} {os.getpgrp()} {os.getsid(0)}\\n")
"""


def _terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


class ShellTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="shell-timeout-"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)

    def _cleanup_writer(self, pidfile: Path) -> None:
        """Kill a background child the runner failed to terminate."""
        try:
            pid = int(pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return
        _terminate(pid)

    def test_timeout_terminates_background_children(self):
        marker = self.workdir / "leak.txt"
        pidfile = self.workdir / "writer.pid"
        writer = self.workdir / "writer.py"
        writer.write_text(WRITER_SOURCE, encoding="utf-8")
        command = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(writer))} "
            f"{shlex.quote(str(marker))} {shlex.quote(str(pidfile))} >/dev/null 2>&1 & "
            f"sleep {SLEEP_S}"
        )
        try:
            result = run_shell(command, self.workdir, TIMEOUT_S)
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["exit_code"], 124)
            self.assertFalse(result["passed"])
            self.assertTrue(marker.is_file(), "the background child never started")
            size_at_timeout = marker.stat().st_size
            self.assertGreater(size_at_timeout, 0, "the background child never wrote")
            time.sleep(1.0)
            size_after_wait = marker.stat().st_size
            self.assertEqual(
                size_after_wait,
                size_at_timeout,
                "background child kept writing after the timeout: the whole shell "
                "process group has to be terminated",
            )
        finally:
            self._cleanup_writer(pidfile)

    def test_command_runs_in_its_own_process_group(self):
        info = self.workdir / "probe.txt"
        probe = self.workdir / "probe.py"
        probe.write_text(PROBE_SOURCE, encoding="utf-8")
        command = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))} "
            f"{shlex.quote(str(info))}; sleep {SLEEP_S}"
        )
        result = run_shell(command, self.workdir, TIMEOUT_S)
        self.assertTrue(result["timed_out"])
        self.assertTrue(info.is_file(), "the command never reported its process ids")
        pid, pgid, sid = (int(part) for part in info.read_text(encoding="utf-8").split())
        self.assertNotEqual(
            pgid,
            os.getpgrp(),
            f"the test command (pid {pid}) shares the runner process group {pgid}; "
            "it has to run in its own session so that the whole group can be killed",
        )
        self.assertNotEqual(
            sid,
            os.getsid(0),
            f"the test command (pid {pid}) still runs in the runner session {sid}",
        )

    def test_timeout_keeps_output_tails(self):
        command = f"echo stdout-line; echo stderr-line >&2; sleep {SLEEP_S}"
        result = run_shell(command, self.workdir, TIMEOUT_S)
        self.assertTrue(result["timed_out"])
        self.assertIn("stdout-line", result["stdout_tail"])
        self.assertIn("stderr-line", result["stderr_tail"])


    def test_completed_commands_preserve_status_and_streams(self):
        for code in (0, 7):
            result = run_shell(f"printf out; printf err >&2; exit {code}", self.workdir, 2)
            self.assertEqual(result["exit_code"], code)
            self.assertEqual(result["passed"], code == 0)
            self.assertFalse(result["timed_out"])
            self.assertEqual(result["stdout_tail"], "out")
            self.assertEqual(result["stderr_tail"], "err")

    def test_timeout_kills_child_after_shell_exits_and_keeps_bounded_tails(self):
        child = self.workdir / "child.py"
        child.write_text(
            "import signal,sys,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('x' * 2500 + 'ENDOUT', flush=True)\n"
            "print('y' * 2500 + 'ENDERR', file=sys.stderr, flush=True)\n"
            "time.sleep(30)\n", encoding="utf-8")
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(child))} & exit 0"
        started = time.monotonic()
        result = run_shell(command, self.workdir, 1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["exit_code"], 124)
        self.assertEqual(result["stdout_tail"], "x" * 1993 + "ENDOUT\n")
        self.assertEqual(result["stderr_tail"], "y" * 1993 + "ENDERR\n")


if __name__ == "__main__":
    unittest.main()
