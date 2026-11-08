#!/usr/bin/env python3
"""Regression coverage for fail-closed lane stuck cleanup (Refs #1825)."""
import contextlib
import io
import importlib.machinery
import importlib.util
import json
import os
import signal
import time
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)


class LaneStuckTickTest(unittest.TestCase):
    def make_run_data(self, home, queue_reason=None):
        runs = home / "runs"
        rows = []
        for index in range(3):
            run_id = f"run-{index + 1}"
            run_dir = runs / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "events.1.pi.jsonl").write_text('{"type":"lane_start","lane":"M19"}\n')
            status = {"state": "finished", "execution_started": 1}
            if queue_reason:
                status["queue_reason"] = queue_reason
            (run_dir / "status.json").write_text(json.dumps(status))
            rows.append({"id": run_id, "lane": "M19", "status": "budget_exceeded"})
        (home / "ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def run_probe(self, home):
        env = dict(os.environ, OPENSWARM_HOME=str(home))
        result = subprocess.run([sys.executable, "-", "M19"], input=AUTODISPATCH.REMOTE_STUCK_CLEANUP,
                                text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    def make_fake_process_tree(self, root):
        for pid, cmd, wchan, children in (
                (43210, b"pi\0--mode\0rpc", "do_wait", "43211"),
                (43211, b"bwrap\0--unshare-all", "poll_schedule_timeout", "")):
            proc = root / str(pid)
            (proc / "task" / str(pid)).mkdir(parents=True)
            rest = "S " + " ".join(str(value) for value in range(20))
            (proc / "stat").write_text(f"{pid} (fake-process) {rest}")
            (proc / "wchan").write_text(wchan)
            (proc / "syscall").write_text("1 2 3")
            (proc / "cmdline").write_bytes(cmd)
            (proc / "task" / str(pid) / "children").write_text(children)

    def test_real_ledger_and_events_do_not_clear_queued_budget_runs(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            home = Path(directory)
            self.make_run_data(home, queue_reason="provider concurrency limit")
            result = self.run_probe(home)
            self.assertFalse(result["cleaned"])
            self.assertEqual(result["reason"], "queued-or-status-unknown")
            self.assertFalse((home / "lane-stuck-audit.jsonl").exists())

    def test_unknown_process_evidence_never_marks_cleanup_success(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            home = Path(directory)
            self.make_run_data(home)
            result = self.run_probe(home)
            self.assertFalse(result["cleaned"])
            self.assertEqual(result["reason"], "rpc-pid-unknown")
            self.assertFalse((home / "lane-stuck-audit.jsonl").exists())

    def test_only_local_bookkeeping_is_excluded_from_executor_event_count(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            home = Path(directory)
            self.make_run_data(home)
            path = home / "runs/run-2/events.1.pi.jsonl"
            start = '{"type":"lane_start","lane":"M19"}\n'
            for events, reason in (
                    ("", "rpc-pid-unknown"),
                    (start, "rpc-pid-unknown"),
                    (start + json.dumps({"type": "error", "source": "supervisor",
                                         "error": "executor did not stop within 30s after interrupt"}) + "\n",
                     "rpc-pid-unknown"),
                    (start + '{"type":"message","text":"working"}\n', "events-unknown-or-nonzero"),
                    (start + '{"type":"error","error":"backend error"}\n', "events-unknown-or-nonzero"),
                    (start + '{"type":"lane_start","lane":"M20"}\n', "events-unknown-or-nonzero"),
                    (start + '{broken\n', "events-unknown-or-nonzero"),
                    (start + '[]\n', "events-unknown-or-nonzero")):
                with self.subTest(events=events):
                    path.write_text(events)
                    result = self.run_probe(home)
                    self.assertFalse(result["cleaned"])
                    self.assertEqual(result["reason"], reason)
            path.unlink()
            self.assertEqual(self.run_probe(home)["reason"], "events-unknown-or-nonzero")

    def test_tick_routes_budget_exceeded_terminal_through_cleanup_probe(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            lane = "M19"
            lanes = {lane: {"host": "executor", "backend": "pi"}}
            logs = []
            old_d, old_brain_log = AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG
            AUTODISPATCH.D = directory
            AUTODISPATCH.BRAIN_LOG = str(Path(directory) / "brain.log")
            with mock.patch.object(AUTODISPATCH, "audit_switches"), \
                 mock.patch.object(AUTODISPATCH, "paused_scopes", return_value=set()), \
                 mock.patch.object(AUTODISPATCH, "host_ok", return_value=True), \
                 mock.patch.object(AUTODISPATCH, "remote", return_value={
                     "run": {"status": "budget_exceeded", "branch": None, "commit": None,
                             "error_class": "budget_exceeded", "repo": "repo"},
                     "lane": "idle", "run_id": "run-3"}), \
                 mock.patch.object(AUTODISPATCH, "cleanup_lane_stuck",
                                   return_value={"cleaned": False, "reason": "queued-or-status-unknown"}) as cleanup, \
                 mock.patch.object(AUTODISPATCH, "label_lane_failed"), \
                 mock.patch.object(AUTODISPATCH, "queue", return_value=[]), \
                 mock.patch.object(AUTODISPATCH, "replenish", return_value=[]), \
                 mock.patch.object(AUTODISPATCH, "status_snapshot", return_value={}), \
                 mock.patch.object(AUTODISPATCH, "save_state"), \
                 mock.patch.object(AUTODISPATCH, "log", side_effect=lambda message, **kwargs: logs.append(message)):
                state = {lane: {"issue": 1825, "run": "run-3", "host": "executor"}}
                AUTODISPATCH.tick(lanes, state, 0)
            AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG = old_d, old_brain_log
            cleanup.assert_called_once_with(lane, lanes[lane])
            self.assertTrue(any("stuck cleanup not performed" in message for message in logs))
            self.assertFalse((Path(directory) / "health.alert").exists())

    def test_tick_alerts_only_after_probe_confirms_term(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            lane = "M19"
            lanes = {lane: {"host": "executor", "backend": "pi"}}
            old_d, old_brain_log = AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG
            AUTODISPATCH.D = directory
            AUTODISPATCH.BRAIN_LOG = str(Path(directory) / "brain.log")
            try:
                with mock.patch.object(AUTODISPATCH, "audit_switches"), \
                     mock.patch.object(AUTODISPATCH, "paused_scopes", return_value=set()), \
                     mock.patch.object(AUTODISPATCH, "host_ok", return_value=True), \
                     mock.patch.object(AUTODISPATCH, "remote", return_value={
                         "run": {"status": "budget_exceeded", "branch": None, "commit": None,
                                 "error_class": "budget_exceeded", "repo": "repo"},
                         "lane": "idle", "run_id": "run-3"}), \
                     mock.patch.object(AUTODISPATCH, "cleanup_lane_stuck", return_value={
                         "cleaned": True, "runs": ["run-1", "run-2", "run-3"], "pids": [1234]}), \
                     mock.patch.object(AUTODISPATCH, "label_lane_failed"), \
                     mock.patch.object(AUTODISPATCH, "queue", return_value=[]), \
                     mock.patch.object(AUTODISPATCH, "replenish", return_value=[]), \
                     mock.patch.object(AUTODISPATCH, "status_snapshot", return_value={}), \
                     mock.patch.object(AUTODISPATCH, "save_state"), \
                     mock.patch.object(AUTODISPATCH, "log"):
                    state = {lane: {"issue": 1825, "run": "run-3", "host": "executor"}}
                    AUTODISPATCH.tick(lanes, state, 0)
            finally:
                AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG = old_d, old_brain_log
            alert = (Path(directory) / "health.alert").read_text()
            self.assertIn("auto:stuck", alert)
            self.assertIn("run-1,run-2,run-3", alert)

    def test_waiting_sandbox_is_not_proof_of_no_progress(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            home = Path(directory)
            self.make_run_data(home)
            proc_root = home / "proc"
            self.make_fake_process_tree(proc_root)
            (home / "lanes" / "M19").mkdir(parents=True)
            (home / "lanes" / "M19" / "state.json").write_text('{"child_pid":43210}')
            with mock.patch.dict(os.environ, {"AD_PROC_ROOT": str(proc_root)}):
                result = self.run_probe(home)
            self.assertFalse(result["cleaned"])
            self.assertEqual(result["reason"], "sandbox-progress-unproven")

    def test_unreadable_process_or_status_evidence_never_cleans(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            home = Path(directory)
            self.make_run_data(home)
            (home / "runs" / "run-2" / "status.json").unlink()
            self.assertEqual(self.run_probe(home)["reason"], "queued-or-status-unknown")
            (home / "runs" / "run-2" / "status.json").write_text(
                '{"execution_started":1,"queue_reason":null}')
            proc_root = home / "proc"
            self.make_fake_process_tree(proc_root)
            (home / "lanes" / "M19").mkdir(parents=True)
            (home / "lanes" / "M19" / "state.json").write_text('{"child_pid":43210}')
            (proc_root / "43211" / "syscall").unlink()
            with mock.patch.dict(os.environ, {"AD_PROC_ROOT": str(proc_root)}):
                self.assertFalse(self.run_probe(home)["cleaned"])

    def test_rpc_exit_alone_does_not_confirm_tree_cleanup(self):
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable), \
                 tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
                home = Path(directory)
                self.make_run_data(home)
                proc_root = home / "proc"
                self.make_fake_process_tree(proc_root)
                sandbox_stat = proc_root / "43211/stat"
                sandbox_stat.write_text(sandbox_stat.read_text().replace(") S ", ") T "))
                (proc_root / "43211/wchan").write_text("do_signal_stop")
                (home / "lanes/M19").mkdir(parents=True)
                (home / "lanes/M19/state.json").write_text('{"child_pid":43210}')

                def deliver(pid, sig):
                    path = proc_root / str(pid) / "stat"
                    if pid == 43210:
                        path.write_text(path.read_text().replace(") S ", ") Z "))
                    elif sig == signal.SIGCONT:
                        # The sandbox ignores TERM, or its exit cannot be read.
                        path.write_text("" if unreadable else path.read_text().replace(") T ", ") S "))

                output = io.StringIO()
                with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(home), "AD_PROC_ROOT": str(proc_root)}), \
                     mock.patch.object(sys, "argv", ["-", "M19"]), \
                     mock.patch.object(os, "kill", side_effect=deliver) as kill, \
                     mock.patch.object(time, "sleep"), \
                     mock.patch.object(time, "time", side_effect=[0, 0, 1, 2]), \
                     contextlib.redirect_stdout(output):
                    with self.assertRaises(SystemExit) as stopped:
                        exec(AUTODISPATCH.REMOTE_STUCK_CLEANUP, {})
                self.assertEqual(stopped.exception.code, 0)
                result = json.loads(output.getvalue())
                self.assertFalse(result["cleaned"])
                self.assertEqual(result["reason"], "tree-term-not-confirmed")
                self.assertFalse((home / "lane-stuck-audit.jsonl").exists())
                self.assertEqual(kill.call_args_list, [mock.call(43211, signal.SIGTERM),
                                                      mock.call(43211, signal.SIGCONT),
                                                      mock.call(43210, signal.SIGTERM)])

    def test_tick_real_term_and_engine_terminals_restore_backend_events(self):
        # Adapt the RPC transport, clock and SSH, retaining the real turn driver,
        # workspace preparation, events, results and ledger. The stalled transport
        # also fails to close its child, leaving a real process tree for the probe
        # to clean. Recovery events come from the restarted RPC fixture.
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            root = Path(directory)
            home, repo, control = root / "home", root / "repo", root / "control"
            for path in (home, repo, control):
                path.mkdir()
            for args in (("init", "-q"), ("config", "user.name", "Test"),
                         ("config", "user.email", "test@example.invalid"),
                         ("commit", "--allow-empty", "-qm", "fixture")):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            sandbox = root / "bwrap.py"
            sandbox.write_text("import os,signal,time\nos.kill(os.getpid(),signal.SIGSTOP)\ntime.sleep(300)\n")
            rpc_script = root / "pi_rpc.py"
            rpc_script.write_text(
                "import json,os,pathlib,signal,subprocess,sys,time\n"
                "root=pathlib.Path(sys.argv[1])\n"
                "def term(*args):\n"
                " (root/'terminated').write_text('TERM received')\n"
                " sys.exit(0)\n"
                "signal.signal(signal.SIGTERM,term)\n"
                "if (root/'terminated').exists():\n"
                " report=json.dumps({'status':'done','summary':'backend recovered'})\n"
                " print(json.dumps({'type':'agent_end','messages':[{'role':'assistant',"
                "'content':[{'type':'text','text':report}]}]}),flush=True)\n"
                " time.sleep(300)\n"
                "else:\n"
                " child=subprocess.Popen([sys.executable,str(root/'bwrap.py')])\n"
                " (root/'sandbox-pid').write_text(str(child.pid))\n"
                " child.wait()\n"
                " time.sleep(300)\n")
            processes = []

            def start_rpc():
                process = subprocess.Popen([sys.executable, str(rpc_script), str(root)],
                                           stdout=subprocess.PIPE, text=True)
                processes.append(process)
                return process

            rpc = start_rpc()
            other_lane = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                                          stdout=subprocess.PIPE, text=True)
            processes.append(other_lane)
            old_d, old_brain = AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG
            AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG = str(control), str(control / "brain.log")
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if ((root / "sandbox-pid").exists() and
                            Path(f"/proc/{rpc.pid}/wchan").read_text().strip() == "do_wait"):
                        break
                    time.sleep(0.02)
                else:
                    self.fail("RPC did not enter wait on its stopped sandbox")
                env = {"OPENSWARM_HOME": str(home), "AD_PROC_ROOT": "/proc",
                       "OPENSWARM_FLEET_GUARD_CONFIG": str(root / "disabled.json")}
                with mock.patch.dict(os.environ, env):
                    loader = importlib.machinery.SourceFileLoader(
                        "stuck_test_swrm", str(Path(__file__).resolve().parents[2] / "swrm/bin/swrm"))
                    engine_spec = importlib.util.spec_from_loader(loader.name, loader)
                    engine = importlib.util.module_from_spec(engine_spec)
                    loader.exec_module(engine)
                    lane = {"name": "M19", "host": "local", "backend": "pi",
                            "model": "test", "repo": str(repo)}

                    class Transport:
                        boundary = None

                        def __init__(self):
                            self.pid = rpc.pid
                            self.close_attempted = False
                            self.sent = []

                        def alive(self):
                            return rpc.poll() is None

                        def send(self, message):
                            self.sent.append(message["type"])

                        def wait_response(self, *args):
                            return {"success": True}

                        def next_event(self, timeout=0):
                            if index >= 5:
                                return None  # RPC stream failure
                            if (root / "terminated").exists():
                                return json.loads(rpc.stdout.readline())
                            clock[0] += 31  # advance through budget and interrupt grace
                            return False

                        def close(self):
                            self.close_attempted = True  # reproduce failed child shutdown

                    def local_ssh(argv, timeout=60, inp=None):
                        # Run as the RPC's ancestor so Linux ptrace restrictions
                        # permit /proc/<pid>/syscall; execute the unmodified probe.
                        output = io.StringIO()
                        with mock.patch.object(sys, "argv", ["-", *argv[argv.index("-")+1:]]), \
                             contextlib.redirect_stdout(output):
                            try:
                                exec(inp, {})
                            except SystemExit as error:
                                self.assertEqual(error.code, 0)
                        return 0, output.getvalue(), ""

                    with mock.patch.object(AUTODISPATCH, "sh", side_effect=local_ssh), \
                         mock.patch.object(AUTODISPATCH, "audit_switches"), \
                         mock.patch.object(AUTODISPATCH, "pause_store_dir", return_value=str(control / "pauses")), \
                         mock.patch.object(AUTODISPATCH, "host_ok", return_value=True), \
                         mock.patch.object(AUTODISPATCH, "label_lane_failed"), \
                         mock.patch.object(AUTODISPATCH, "queue", return_value=[]), \
                         mock.patch.object(AUTODISPATCH, "replenish", return_value=[]), \
                         mock.patch.object(AUTODISPATCH, "status_snapshot", return_value={}):
                        state = {}
                        for index in range(1, 8):
                            if index == 4:
                                rpc.wait(timeout=5)
                                rpc = start_rpc()  # resident supervisor's restart boundary
                            engine.lane_write_state("M19", state="idle", child_pid=rpc.pid)
                            run_id = f"run-{index}"
                            spec = {"run_id": run_id, "task": "crash" if index >= 5 else "test", "started": time.time(),
                                    "budget_minutes": 0.002, "read_only": True}
                            clock = [time.time()]
                            child = Transport()
                            with mock.patch.object(engine, "NOW", side_effect=lambda: clock[0]):
                                result = engine.lane_do_run(lane, spec, child, None, {})
                            self.assertEqual(result["status"], "budget_exceeded" if index < 4 else
                                             "done" if index == 4 else "failed")
                            status = json.loads((home / "runs" / run_id / "status.json").read_text())
                            self.assertTrue(status["execution_started"])
                            events = [json.loads(line) for line in
                                      (home / "runs" / run_id / "events.1.pi.jsonl").read_text().splitlines()]
                            self.assertEqual(events[0], {"type": "lane_start", "lane": "M19"})
                            if index < 4:
                                self.assertEqual([event["type"] for event in events], ["lane_start", "error"])
                                self.assertEqual(events[1]["source"], "supervisor")
                                self.assertTrue(child.close_attempted)
                                self.assertIn("abort", child.sent)
                            state["M19"] = {"issue": 1825 + index, "run": run_id, "host": "local"}
                            AUTODISPATCH.tick({"M19": lane}, state, 0)
                            self.assertIsNone(other_lane.poll(), "cleanup must leave other lanes alone")
                            if index == 3:
                                rpc.wait(timeout=5)
                                sandbox_stat = Path(f"/proc/{(root / 'sandbox-pid').read_text()}/stat")
                                if sandbox_stat.exists():
                                    self.assertEqual(sandbox_stat.read_text().split(") ", 1)[1][0], "Z",
                                                     "cleaned must mean the stopped sandbox also exited")
                            audit = home / "lane-stuck-audit.jsonl"
                            self.assertEqual(len(audit.read_text().splitlines()) if audit.exists() else 0,
                                             1 if index >= 3 else 0)
                            self.assertEqual((control / "PAUSE-M19").exists(), index == 7)
                        self.assertIn("run-5,run-6,run-7", (control / "PAUSE-M19").read_text())
                        self.assertIn("backend recovered", (home / "runs/run-4/events.1.pi.jsonl").read_text())
                        self.assertEqual((control / "health.alert").read_text().count("auto:stuck"), 1)
                        self.assertIn("auto:stuck", (control / "brain.log").read_text())
            finally:
                AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG = old_d, old_brain
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    process.stdout.close()
                if (root / "sandbox-pid").exists():
                    try:
                        pid = int((root / "sandbox-pid").read_text())
                        if Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z":
                            os.kill(pid, signal.SIGKILL)  # failure-only fixture cleanup
                    except FileNotFoundError:
                        pass
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
