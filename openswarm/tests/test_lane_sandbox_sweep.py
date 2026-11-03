"""A lane run sweeps the sandbox it reuses: what a run left behind must not outlive it.

The sweep may only terminate processes that provably belong to the lane sandbox, and it
may never block the run teardown.  These tests drive the real run teardown
(``lane_finalize_run``) against a real process tree: a stand-in for bubblewrap (it keeps
the process shape of a sandbox, a root with the sandbox inside it), the resident
executor, its broker bridge, the background loop a run left behind and an unrelated
process the supervisor adopted from somewhere else.

The cases that must never be swept are covered as well: the sandbox of another lane, a
lane child that owns two sandboxes, a sandbox without a recognizable resident executor
and a run that has no lane child at all.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()
xs.__path__ = [str(ROOT)]

LANE_CHILD = object()  # "the lane child of this test", so a run may also run without one

BWRAP_STANDIN = '''#!/usr/bin/env python3
"""A stand-in for bubblewrap: the same process shape, without the namespaces.

Real bubblewrap starts the sandbox in its own session (``--new-session``) and keeps a
root process that waits for the sandbox, so the sandbox processes are told apart from the
rest of the host by that session.  The root names itself ``bwrap`` in its argument
vector, the way the native binary does.
"""
import os
import sys

if len(sys.argv) > 1 and sys.argv[1] == "--sandbox":
    try:
        os.setsid()
    except OSError:
        pass
    pid = os.fork()
    if pid:
        _, status = os.waitpid(pid, 0)
        os._exit(os.WEXITSTATUS(status))
    os.execv(sys.argv[2], sys.argv[2:])
os.execv(sys.executable, [sys.argv[0], sys.argv[0], "--sandbox"] + sys.argv[1:])
'''

LAUNCHER_STANDIN = '''#!/usr/bin/env python3
"""A stand-in for the isolated Pi launcher: it starts the sandbox and waits for it."""
import subprocess
import sys

sys.exit(subprocess.call(sys.argv[1:]))
'''

LAUNCHER_TWO_STANDIN = '''#!/usr/bin/env python3
"""A stand-in launcher that starts two sandboxes: the lane child owns more than one.

A sweep that cannot tell the sandboxes apart may not sweep either of them.
"""
import subprocess
import sys

children = [subprocess.Popen(sys.argv[1:]) for _ in range(2)]
for child in children:
    child.wait()
'''

RESIDENT_STANDIN = '''#!/usr/bin/env python3
"""A stand-in for the resident Pi executor: it stays alive between the runs of a lane.

It also starts the background jobs a run asks for, the way an executor starts a
background job for a test: a request file names the command, the executor runs it and
drops the request.
"""
import os
import subprocess
import time

root = {root!r}
request = os.path.join(root, "request")
while True:
    if os.path.exists(request):
        try:
            os.remove(request)
        except OSError:
            pass
        try:
            with open(os.path.join(root, "request.cmd")) as handle:
                command = handle.read().strip()
        except OSError:
            command = ""
        if command:
            subprocess.Popen(command, shell=True)
    time.sleep(0.05)
'''

BRIDGE_STANDIN = '''#!/usr/bin/env python3
"""A stand-in for the broker bridge (socat): it lives as long as the sandbox does."""
import time

while True:
    time.sleep(3600)
'''

SPIN_STANDIN = '''#!/bin/sh
# The leftover of a run: a background loop the run started and never waited for.
echo $$ > "$1"
while :; do sleep 0.05; done
'''


def pid_alive(pid):
    """Whether a pid still runs.

    A zombie is gone already: it burns no CPU, it only waits for the sandbox to reap it.
    """
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError, ValueError):
        return True


def pgrep_f(pattern):
    """The processes whose command line matches a pattern, read with pgrep.

    The acceptance of the sweep is read from the real process table, not from the
    summary the sweep wrote about itself.
    """
    result = subprocess.run(["pgrep", "-af", pattern], capture_output=True, text=True)
    return result.stdout.strip()


def parent_of(pid):
    """The parent pid of a process, read from /proc, or None when it cannot be read."""
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as handle:
            return int(handle.read().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


class Sandbox:
    """The process tree of a lane sandbox: a launcher, a sandbox root and the sandbox."""

    def __init__(self, root, sandboxes=1, resident=True):
        self.root = pathlib.Path(root)
        self.bin = self.root / "bin"
        self.runtime = self.root / "runtime"
        self.spin_pid_file = self.root / "spin.pid"
        write_exec(self.bin / "bwrap", BWRAP_STANDIN)
        launcher = LAUNCHER_STANDIN if sandboxes == 1 else LAUNCHER_TWO_STANDIN
        write_exec(self.bin / "launcher", launcher)
        write_exec(self.bin / "socat", BRIDGE_STANDIN)
        write_exec(self.bin / "spin", SPIN_STANDIN)
        if resident:
            self.resident = write_exec(self.runtime / "node_modules" / ".bin" / "pi",
                                       RESIDENT_STANDIN.format(root=str(self.root)))
            body = (f"{self.bin / 'socat'} TCP-LISTEN:18765 UNIX-CONNECT:/run/broker.sock &\n"
                    f"exec {self.resident}\n")
        else:
            # A sandbox without the resident executor of a lane: a sweep may not touch it,
            # because it cannot tell the lane's own processes from the leftovers.
            self.resident = None
            body = "exec /bin/sleep 300\n"
        self.init = write_exec(self.root / "init.sh", "#!/bin/sh\n" + body)
        self.launcher = None

    def start(self):
        """Start the sandbox and return the pid of the lane child (the launcher)."""
        self.launcher = subprocess.Popen([str(self.bin / "launcher"), str(self.bin / "bwrap"),
                                          "/bin/sh", str(self.init)])
        return self.launcher.pid

    def start_background(self, command):
        """Ask the resident executor to start a background job, the way a run's test does."""
        (self.root / "request.cmd").write_text(command)
        (self.root / "request").write_text("go")

    def start_orphan_background(self, command, pid_file):
        """Start a background job whose parent inside the sandbox dies right away.

        The job is reparented to the supervisor (the subreaper), so it is no longer part of
        the sandbox tree, but it keeps the session of the sandbox: that is the shape of the
        leftover a run leaves behind when the shell that started it is gone.
        """
        (self.root / "request.cmd").write_text(f"{command} &")
        (self.root / "request").write_text("go")
        return pid_file

    def wait_for(self, predicate, timeout=15):
        """Wait until a predicate holds, so a test never races the sandbox startup."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def spin_pid(self):
        """The pid of the background loop a run left behind, or None."""
        try:
            return int(self.spin_pid_file.read_text().strip())
        except (OSError, ValueError):
            return None

    def pids(self):
        """Every pid of the sandbox tree."""
        pids = []
        if self.launcher is None:
            return pids
        pids.append(self.launcher.pid)
        try:
            table, _ = xs.process_table()
        except Exception:  # pragma: no cover - the table only finds the tree
            return pids
        pids.extend(xs.process_descendants(self.launcher.pid, xs.process_child_index(table)))
        return pids

    def stop(self):
        for pid in self.pids():
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        if self.launcher is not None:
            try:
                self.launcher.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


@unittest.skipUnless(sys.platform.startswith("linux"), "the sandbox sweep is a Linux feature")
class LaneSandboxSweepTests(unittest.TestCase):
    """The run teardown sweeps the sandbox and keeps everything that has to survive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oslane-sweep-")
        self.root = pathlib.Path(self.tmp.name)
        # The lane supervisor is the subreaper: a process whose parent died is adopted by
        # it, which is how a leftover of another lane could end up next to the sandbox.
        self.assertTrue(xs.lane_become_subreaper(), "the supervisor is not a subreaper")
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Sweep Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "sweep@example.invalid"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("base\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.repo, check=True)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.sandbox = Sandbox(self.root / "sandbox")
        self.lane_pid = self.sandbox.start()
        self.unrelated = self.spawn_unrelated_orphan()
        self.sandbox.start_background(f"{self.sandbox.bin / 'spin'} {self.sandbox.spin_pid_file}")
        self.assertTrue(self.sandbox.wait_for(lambda: self.sandbox.spin_pid()),
                        "the sandbox did not start its background loop")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.sandbox.stop)
        self.addCleanup(self.kill_unrelated)

    def spawn_unrelated_orphan(self):
        """A process the supervisor adopted from somewhere else: it is not the sandbox's."""
        pid_file = self.root / "unrelated.pid"
        proc = subprocess.run(["/bin/sh", "-c", f"sleep 300 & echo $! > {pid_file}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0)
        pid = int(pid_file.read_text().strip())
        # The shell that started it is gone, so the process is now a child of the supervisor.
        self.assertTrue(self.sandbox.wait_for(lambda: parent_of(pid) == os.getpid()),
                        "the orphan was not adopted by the supervisor")
        return pid

    def kill_unrelated(self):
        try:
            os.kill(self.unrelated, 9)
        except OSError:
            pass

    def sandbox_pids(self):
        """The resident executor and the broker bridge of the sandbox."""
        table, _ = xs.process_table()
        index = xs.process_child_index(table)
        descendants = xs.process_descendants(self.lane_pid, index)
        resident = next(pid for pid in descendants
                        if "node_modules/.bin/pi" in (xs.process_cmdline(pid) or ""))
        bridge = next(pid for pid in descendants
                      if "/run/broker.sock" in (xs.process_cmdline(pid) or ""))
        return resident, bridge

    def finalize(self, run_id, child_pid=LANE_CHILD):
        """Run the real run teardown of a lane and return the result of the run."""
        rdir = self.runs / run_id
        rdir.mkdir(exist_ok=True)
        event_file = str(rdir / "events.jsonl")
        lane = {"name": "L1", "backend": "pi", "model": "fake-model", "provider": "fake-provider"}
        spec = {"run_id": run_id, "task": "work", "title": "sweep", "started": 1000.0,
                "budget_minutes": 60}
        ws = {"mode": "repo", "path": str(self.repo), "root": str(self.repo),
              "repo": str(self.repo), "base": "HEAD"}
        old = {key: getattr(xs, key) for key in ("RUNS", "LEDGER")}
        xs.RUNS = str(self.runs)
        xs.LEDGER = str(self.root / "ledger.jsonl")
        try:
            with mock.patch.object(xs, "NOW", return_value=1001.0), \
                 mock.patch.object(xs, "git", return_value=(0, "", "")), \
                 mock.patch.object(xs, "finalize_workspace", return_value={
                     "branch": None, "commit": None, "diffstat": "", "excluded_files": [],
                     "patch": None, "worktree": str(self.repo), "merge_hint": None}), \
                 mock.patch.object(xs, "append_ledger"), \
                 mock.patch.object(xs, "cleanup_executor_tmp"):
                out = xs.lane_finalize_run(lane, spec, ws, None, {}, "exec_error", 1, event_file,
                                           child_pid=self.lane_pid if child_pid is LANE_CHILD else child_pid)
        finally:
            for key, value in old.items():
                setattr(xs, key, value)
        events = [json.loads(line) for line in pathlib.Path(event_file).read_text().splitlines()]
        return out, events

    def test_run_teardown_kills_the_background_loop_and_keeps_the_resident(self):
        spin = self.sandbox.spin_pid()
        resident, bridge = self.sandbox_pids()

        started = time.monotonic()
        out, events = self.finalize("run-1")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 30, "the sweep blocked the run teardown")
        self.assertFalse(pid_alive(spin), "the background loop of the run survived the sweep")
        self.assertTrue(pid_alive(resident), "the resident executor was swept")
        self.assertTrue(pid_alive(bridge), "the broker bridge was swept")
        self.assertTrue(pid_alive(self.unrelated), "a process outside the sandbox was swept")
        # The process table itself is the witness: the leftover is gone from it and the
        # resident executor of the lane is still in it.
        self.assertEqual(pgrep_f(str(self.sandbox.bin / "spin")), "",
                         "the background loop is still in the process table")
        self.assertIn(str(self.sandbox.resident), pgrep_f(str(self.sandbox.resident)),
                      "the resident executor is not in the process table any more")
        sweep = out["sandbox_sweep"]
        self.assertGreaterEqual(sweep["killed"], 1)
        self.assertIn(spin, sweep["killed_pids"])
        self.assertEqual(sweep["error"], None)
        self.assertGreaterEqual(sweep["scanned"], 1)
        self.assertTrue(any("spin" in row["cmd"] for row in sweep["processes"]), sweep["processes"])
        sweeps = [event for event in events if event.get("type") == "sandbox_sweep"]
        self.assertEqual(len(sweeps), 1, events)
        self.assertEqual(sweeps[0]["killed"], sweep["killed"])
        self.assertTrue(sweeps[0]["processes"])

    def test_a_leftover_whose_parent_died_is_still_swept(self):
        # The leftover of the incident was a background loop whose parent inside the
        # sandbox was gone, so it was reparented away from the sandbox tree.  It keeps the
        # session of the sandbox, so the sweep still recognizes it as part of the sandbox.
        pid_file = self.root / "orphan.pid"
        self.sandbox.start_orphan_background(f"{self.sandbox.bin / 'spin'} {pid_file}", pid_file)
        self.assertTrue(self.sandbox.wait_for(
            lambda: pid_file.exists() and pid_file.read_text().strip()),
            "the sandbox did not start its background loop")
        spin = int(pid_file.read_text().strip())
        self.assertTrue(self.sandbox.wait_for(lambda: parent_of(spin) == os.getpid()),
                        "the leftover was not reparented away from the sandbox tree")

        summary = xs.lane_sweep_sandbox(self.lane_pid)

        self.assertEqual(summary["error"], None)
        self.assertIn(spin, summary["killed_pids"])
        self.assertFalse(pid_alive(spin), "the leftover whose parent died survived the sweep")
        self.assertTrue(pid_alive(self.unrelated), "a process outside the sandbox was swept")

    def test_two_runs_of_the_same_lane_both_finish_and_both_sweep(self):
        first, _ = self.finalize("run-1")
        self.assertEqual(first["status"], "failed")
        # The second run starts another background loop in the same sandbox.
        self.sandbox.spin_pid_file.unlink(missing_ok=True)
        self.sandbox.start_background(f"{self.sandbox.bin / 'spin'} {self.sandbox.spin_pid_file}")
        self.assertTrue(self.sandbox.wait_for(lambda: self.sandbox.spin_pid()))
        spin = self.sandbox.spin_pid()

        second, events = self.finalize("run-2")

        self.assertEqual(second["status"], "failed")
        self.assertFalse(pid_alive(spin), "the second run left its background loop behind")
        self.assertIn(spin, second["sandbox_sweep"]["killed_pids"])
        self.assertEqual([event["type"] for event in events].count("sandbox_sweep"), 1)
        self.assertTrue((self.runs / "run-1" / "result.json").exists())
        self.assertTrue((self.runs / "run-2" / "result.json").exists())

    def test_a_lane_without_a_sandbox_is_skipped(self):
        other = subprocess.Popen(["/bin/sleep", "300"])

        def stop():
            other.kill()
            other.wait()

        self.addCleanup(stop)
        summary = xs.lane_sweep_sandbox(other.pid)
        self.assertEqual(summary["skipped"], "no sandbox")
        self.assertEqual(summary["killed"], 0)
        self.assertEqual(summary["error"], None)

    def test_a_failing_sweep_is_logged_and_the_run_still_finishes(self):
        with mock.patch.object(xs, "sandbox_sweep_plan", side_effect=RuntimeError("boom")):
            out, events = self.finalize("run-fail")
        self.assertIn("boom", out["sandbox_sweep"]["error"])
        self.assertTrue((self.runs / "run-fail" / "result.json").exists())
        sweeps = [event for event in events if event.get("type") == "sandbox_sweep"]
        self.assertEqual(len(sweeps), 1, events)
        self.assertIn("boom", sweeps[0]["error"])

    def test_a_sweep_that_runs_out_of_budget_gives_up_and_leaves_the_run_alone(self):
        spin = self.sandbox.spin_pid()
        summary = xs.lane_sweep_sandbox(self.lane_pid, deadline=-1.0)
        self.assertIn("budget", summary["error"])
        self.assertEqual(summary["killed"], 0)
        self.assertTrue(pid_alive(spin), "a sweep without budget may not kill anything")

    def test_the_sweep_leaves_another_lane_sandbox_alone(self):
        other = Sandbox(self.root / "other-sandbox")
        self.addCleanup(other.stop)
        other_pid = other.start()
        other.start_background(f"{other.bin / 'spin'} {other.spin_pid_file}")
        self.assertTrue(other.wait_for(lambda: other.spin_pid()),
                        "the other sandbox did not start its background loop")
        other_spin = other.spin_pid()

        summary = xs.lane_sweep_sandbox(self.lane_pid)

        self.assertEqual(summary["error"], None)
        self.assertGreaterEqual(summary["killed"], 1)
        self.assertTrue(pid_alive(other_spin), "the sweep touched another lane's sandbox")
        self.assertTrue(pid_alive(other_pid), "the sweep touched another lane's sandbox")

    def test_a_run_without_a_lane_child_sweeps_no_sandbox(self):
        other = Sandbox(self.root / "other-sandbox")
        self.addCleanup(other.stop)
        other.start()
        other.start_background(f"{other.bin / 'spin'} {other.spin_pid_file}")
        self.assertTrue(other.wait_for(lambda: other.spin_pid()),
                        "the other sandbox did not start its background loop")
        other_spin = other.spin_pid()
        # The host runner writes its own pid into the run status and supervises every
        # lane, so a teardown that used it would sweep a foreign sandbox.
        rdir = self.runs / "run-nochild"
        rdir.mkdir(exist_ok=True)
        (rdir / "status.json").write_text(json.dumps({"executor_pid": os.getpid()}))

        out, events = self.finalize("run-nochild", child_pid=None)

        self.assertEqual(out["sandbox_sweep"]["skipped"], "no lane child")
        self.assertEqual(out["sandbox_sweep"]["killed"], 0)
        self.assertTrue(pid_alive(other_spin), "the sweep touched a foreign sandbox")
        self.assertTrue(pid_alive(self.sandbox.spin_pid()),
                        "a run without a lane child swept a sandbox")
        sweeps = [event for event in events if event.get("type") == "sandbox_sweep"]
        self.assertEqual(len(sweeps), 1, events)
        self.assertEqual(sweeps[0]["skipped"], "no lane child")

    def test_an_ambiguous_sandbox_is_not_swept(self):
        two = Sandbox(self.root / "two-sandbox", sandboxes=2)
        self.addCleanup(two.stop)
        two_pid = two.start()
        two.start_background(f"{two.bin / 'spin'} {two.spin_pid_file}")
        self.assertTrue(two.wait_for(lambda: two.spin_pid()),
                        "the sandbox did not start its background loop")
        spin = two.spin_pid()

        summary = xs.lane_sweep_sandbox(two_pid)

        self.assertIn("ambiguous", summary["skipped"])
        self.assertEqual(summary["killed"], 0)
        self.assertEqual(summary["error"], None)
        self.assertTrue(pid_alive(spin), "an ambiguous sandbox was swept")

    def test_a_sandbox_without_a_resident_executor_is_not_swept(self):
        bare = Sandbox(self.root / "bare-sandbox", resident=False)
        self.addCleanup(bare.stop)
        bare_pid = bare.start()
        roots = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not roots:
            table, _ = xs.process_table()
            roots = xs.sandbox_roots(bare_pid, xs.process_child_index(table))
            if not roots:
                time.sleep(0.05)
        self.assertTrue(roots, "the sandbox did not start")

        summary = xs.lane_sweep_sandbox(bare_pid)

        self.assertEqual(summary["skipped"], "no resident executor in the sandbox")
        self.assertEqual(summary["killed"], 0)
        self.assertEqual(summary["error"], None)
        for pid in roots:
            self.assertTrue(pid_alive(pid), "a sandbox without a resident executor was swept")


if __name__ == "__main__":
    unittest.main()
