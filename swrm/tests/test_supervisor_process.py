"""The generated process supervisor really supervises: it restarts, it stops, it cleans up.

Every test runs the generated POSIX script for real against a fake command in a
temporary home, so the assertions cover the shell behaviour itself and not only
the rendered text. Nothing here touches the real ~/.claude/swrm.
"""
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = ROOT / "bin" / "swrm"
TMP = ROOT.parent / ".swrm-tmp"
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()


class ProcessSupervisorTests(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TMP)
        self.home = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    # -- helpers ---------------------------------------------------------

    def script(self, command, **kwargs):
        with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)}):
            return xs.generate_process_supervisor("fake-lane", command, **kwargs)

    def write_script(self, text, name="supervisor.sh"):
        script = self.home / name
        script.write_text(text)
        script.chmod(0o700)
        return script

    def launch(self, command, env=None, **kwargs):
        """Write the script, check its syntax and run the supervisor in the foreground."""
        script = self.write_script(self.script(command, **kwargs))
        syntax = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        process = subprocess.Popen(["sh", str(script), "run"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=dict(os.environ, **(env or {})))
        return script, process

    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail("condition did not become true before timeout")

    @staticmethod
    def pid_running(pid):
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, ValueError, TypeError):
            return False
        except PermissionError:
            return True
        try:
            out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                                 capture_output=True, text=True).stdout.strip()
        except OSError:
            return True
        if out:
            return not any(char in out for char in ("Z", "z"))
        return True

    def supervisor_pids(self, script):
        """The pids of the supervisors of `script` (the process group of the command is not one)."""
        if shutil.which("pgrep"):
            result = subprocess.run(["pgrep", "-f", str(script)], capture_output=True, text=True)
            return [int(x) for x in result.stdout.split() if x.isdigit()]
        result = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,args="], capture_output=True, text=True)
        pids = []
        for line in result.stdout.splitlines():
            pid, _, args = line.strip().partition(" ")
            if pid.isdigit() and str(script) in args:
                pids.append(int(pid))
        return pids

    def stop_supervisor(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    # -- tests ------------------------------------------------------------

    def test_generated_scripts_pass_the_shell_syntax_check(self):
        lane = self.script([sys.executable, "lane", "serve", "C1"], lane_name="C1")
        broker = self.script(["node", "broker.mjs"],
                             broker_socket='"$HOME/Library/Caches/swrm-pi-broker/broker.sock"')
        for text in (lane, broker):
            with self.subTest(script=text.splitlines()[0]):
                script = self.write_script(text, "syntax.sh")
                result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_tree_walk_does_not_depend_on_proc(self):
        """macOS has no /proc: the tree walk must use pgrep or ps."""
        text = self.script([sys.executable, "-c", "pass"])
        self.assertNotIn("/proc", text)
        self.assertIn("pgrep -P", text)
        self.assertIn("ps -A -o pid=,ppid=", text)

    def test_restart_backoff_stop_and_syntax(self):
        cmd = [sys.executable, "-c", "import time; print(time.time(), flush=True)"]
        script, process = self.launch(cmd)
        log = self.home / "logs" / "fake-lane.log"
        try:
            self.wait_for(lambda: log.exists() and len(log.read_text().splitlines()) >= 3)
            starts = [float(x) for x in log.read_text().splitlines()]
            gaps = [b - a for a, b in zip(starts, starts[1:])]
            self.assertGreater(gaps[1], gaps[0] + 0.7)
            subprocess.run(["sh", str(script), "stop"], check=True, timeout=30)
            self.assertFalse((self.home / "supervise" / "fake-lane.pid").exists())
            self.assertFalse((self.home / "supervise" / "fake-lane.child.pid").exists())
            self.wait_for(lambda: process.poll() is not None)
        finally:
            self.stop_supervisor(process)

    def test_stop_kills_forked_descendant(self):
        """The executor of a lane starts in its own session: `stop` must reach it anyway."""
        grandchild_file = self.home / "grandchild.pid"
        code = ("import os,subprocess,sys,time; ")
        code += "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],start_new_session=True); "
        code += f"open({str(grandchild_file)!r},'w').write(str(p.pid)); time.sleep(120)"
        script, process = self.launch([sys.executable, "-c", code])
        try:
            self.wait_for(grandchild_file.exists)
            grandchild = int(grandchild_file.read_text())
            subprocess.run(["sh", str(script), "stop"], check=True, timeout=30)
            self.wait_for(lambda: not self.pid_running(grandchild))
            self.assertFalse((self.home / "supervise" / "fake-lane.pid").exists())
            self.assertFalse((self.home / "supervise" / "fake-lane.child.pid").exists())
        finally:
            self.stop_supervisor(process)

    def test_stop_cleans_up_after_supervisor_was_killed(self):
        """A supervisor killed with SIGKILL leaves its command behind; stop must still clear it."""
        child_file = self.home / "child.pid"
        code = (f"import pathlib,time; pathlib.Path({str(child_file)!r}).write_text(str(1)); "
                "time.sleep(120)")
        script, process = self.launch([sys.executable, "-c", code])
        try:
            self.wait_for(child_file.exists)
            supervisor = int((self.home / "supervise" / "fake-lane.pid").read_text())
            os.kill(supervisor, 9)
            process.poll()
            subprocess.run(["sh", str(script), "stop"], check=True, timeout=30)
            self.assertFalse((self.home / "supervise" / "fake-lane.pid").exists())
            self.assertFalse((self.home / "supervise" / "fake-lane.child.pid").exists())
        finally:
            self.stop_supervisor(process)

    def test_stop_ends_the_supervisor_during_the_backoff_wait(self):
        """A crashed command waits in the backoff: `stop` must not wait for that window."""
        runs = self.home / "runs"
        code = (f"import os,pathlib; d=pathlib.Path({str(runs)!r}); d.mkdir(parents=True, exist_ok=True); "
                f"(d/str(os.getpid())).write_text('x')")
        script, process = self.launch([sys.executable, "-c", code],
                                       env={"OPENSWARM_SUPERVISE_BACKOFF": "45"})
        try:
            self.wait_for(lambda: runs.is_dir() and any(runs.iterdir()))
            supervisor = int((self.home / "supervise" / "fake-lane.pid").read_text())
            # The command crashed, so the supervisor waits in a 45 second backoff.
            time.sleep(3)
            self.assertEqual(len(list(runs.iterdir())), 1, "the backoff window was not honoured")
            began = time.monotonic()
            subprocess.run(["sh", str(script), "stop"], check=True, timeout=30)
            self.assertLess(time.monotonic() - began, 15, "stop waited for the backoff window")
            self.wait_for(lambda: process.poll() is not None)
            self.assertFalse(self.pid_running(supervisor), "the supervisor survived the stop")
            self.assertFalse((self.home / "supervise" / "fake-lane.pid").exists())
            # A second supervisor must not start while the first one is still alive.
            subprocess.run(["sh", str(script), "start"], check=True, timeout=30)
            self.wait_for(lambda: len(list(runs.iterdir())) == 2)
            time.sleep(2)
            self.assertEqual(len(self.supervisor_pids(script)), 1, "more than one supervisor is running")
        finally:
            subprocess.run(["sh", str(script), "stop"], check=False, timeout=30)
            self.stop_supervisor(process)

    def test_broker_makes_socket_directory(self):
        socket_path = self.home / "run" / "broker.sock"
        marker = self.home / "broker-started"
        code = (f"import pathlib,sys,time; assert pathlib.Path(sys.argv[1]).parent.is_dir(); "
                f"pathlib.Path({str(marker)!r}).write_text(sys.argv[1]); time.sleep(120)")
        with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)}):
            text = xs.generate_process_supervisor("broker", [sys.executable, "-c", code],
                                                  str(socket_path))
        script = self.write_script(text, "broker.sh")
        syntax = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        env = dict(os.environ, OPENSWARM_PI_BROKER_SOCKET=str(socket_path))
        proc = subprocess.Popen(["sh", str(script), "run"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            self.wait_for(marker.exists)
            self.assertEqual(marker.read_text(), str(socket_path))
            self.assertTrue(socket_path.parent.is_dir())
            subprocess.run(["sh", str(script), "stop"], check=True, timeout=30)
            self.assertFalse((self.home / "supervise" / "broker.pid").exists())
        finally:
            self.stop_supervisor(proc)

    def test_broker_keeps_the_socket_of_a_running_broker(self):
        """A live broker socket must survive: only a refused connection means a stale socket."""
        socket_path = self.home / "run" / "broker.sock"
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(socket_path))
        # A backlog of one that is already taken makes the next connect() fail with
        # EAGAIN instead of returning ECONNREFUSED: the broker is busy, not gone.
        listener.listen(0)
        held = socket.socket(socket.AF_UNIX)
        held.connect(str(socket_path))
        marker = self.home / "broker-started"
        code = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('x')"
        with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)}):
            text = xs.generate_process_supervisor("broker", [sys.executable, "-c", code],
                                                  str(socket_path))
        script = self.write_script(text, "broker.sh")
        env = dict(os.environ, OPENSWARM_PI_BROKER_SOCKET=str(socket_path))
        try:
            result = subprocess.run(["sh", str(script), "run"], capture_output=True, text=True,
                                    env=env, timeout=30)
            self.assertNotEqual(result.returncode, 0, "the broker started on a live socket")
            self.assertTrue(socket_path.exists(), "the socket of the running broker was removed")
            self.assertFalse(marker.exists(), "the broker command ran on a live socket")
        finally:
            held.close()
            listener.close()

    def test_broker_auth_module_follows_the_pi_runtime(self):
        socket_path = self.home / "run" / "broker.sock"
        runtime = self.home / "runtime"
        auth = runtime / "node_modules" / "@mariozechner" / "pi-coding-agent" / "dist" / "core" / "auth-storage.js"
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text("// fake auth module\n")
        marker = self.home / "broker-argv"
        code = f"import pathlib,sys; pathlib.Path({str(marker)!r}).write_text('\\n'.join(sys.argv[1:]))"
        with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)}):
            text = xs.generate_process_supervisor("broker", [sys.executable, "-c", code],
                                                  str(socket_path))
        script = self.write_script(text, "broker.sh")
        env = dict(os.environ, OPENSWARM_PI_BROKER_SOCKET=str(socket_path),
                   OPENSWARM_PI_RUNTIME=str(runtime))
        proc = subprocess.Popen(["sh", str(script), "run"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            self.wait_for(marker.exists)
            argv = marker.read_text().splitlines()
            self.assertEqual(argv[0], str(socket_path))
            self.assertEqual(argv[1], str(auth))
            self.assertEqual(argv[2], "MiniMax-M3.1-Flash")
        finally:
            subprocess.run(["sh", str(script), "stop"], check=False, timeout=30)
            self.stop_supervisor(proc)

    def test_stop_bounds_the_lane_down_call(self):
        """`lane down` must not block `stop` for a minute when the lane does not answer."""
        marker = self.home / "fake-cli.pid"
        fake_cli = self.home / "fake-cli.sh"
        fake_cli.write_text("#!/bin/sh\necho $$ > " + str(marker) + "\nexec sleep 300\n")
        fake_cli.chmod(0o700)
        script, process = self.launch([sys.executable, "-c", "import time; time.sleep(120)"],
                                       env={"OPENSWARM_SUPERVISE_CLI": str(fake_cli),
                                            "OPENSWARM_SUPERVISE_DOWN_TIMEOUT": "2"},
                                       lane_name="C1")
        env = dict(os.environ, OPENSWARM_SUPERVISE_CLI=str(fake_cli),
                   OPENSWARM_SUPERVISE_DOWN_TIMEOUT="2")
        try:
            began = time.monotonic()
            subprocess.run(["sh", str(script), "stop"], check=True, timeout=30, env=env)
            self.assertLess(time.monotonic() - began, 20, "stop waited for the lane down call")
            self.assertTrue(marker.exists(), "the lane down call never ran")
            fake_pid = int(marker.read_text().strip())
            self.wait_for(lambda: not self.pid_running(fake_pid))
        finally:
            self.stop_supervisor(process)

    def test_script_exports_the_openswarm_home(self):
        marker = self.home / "home.txt"
        code = (f"import os,pathlib; pathlib.Path({str(marker)!r}).write_text("
                "os.environ.get('OPENSWARM_HOME', '')); import time; time.sleep(120)")
        script, process = self.launch([sys.executable, "-c", code])
        try:
            self.wait_for(marker.exists)
            self.assertEqual(marker.read_text(), str(self.home))
        finally:
            subprocess.run(["sh", str(script), "stop"], check=False, timeout=30)
            self.stop_supervisor(process)

    def test_start_detaches_the_supervisor_from_the_terminal(self):
        text = self.script([sys.executable, "-c", "pass"])
        self.assertIn("setsid", text)
        self.assertIn("nohup", text)
        self.assertIn("</dev/null", text)
        self.assertIn("trap '' HUP", text)

    def test_start_runs_the_generated_script(self):
        script = self.write_script(self.script([sys.executable, "-c", "import time; time.sleep(120)"]))
        pidfile = self.home / "supervise" / "fake-lane.pid"
        try:
            result = subprocess.run(["sh", str(script), "start"], check=True, capture_output=True,
                                    text=True, timeout=30)
            self.wait_for(pidfile.exists)
            self.assertEqual(str(pidfile.read_text().strip()), result.stdout.strip())
            self.assertEqual(subprocess.run(["sh", str(script), "status"]).returncode, 0)
        finally:
            subprocess.run(["sh", str(script), "stop"], check=False, timeout=30)
        self.assertFalse(pidfile.exists())

    def test_cli_prints_broker_supervisor(self):
        result = subprocess.run([sys.executable, str(CLI), "lane", "supervise", "--broker", "--print"],
                                env=dict(os.environ, OPENSWARM_HOME=str(self.home)),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("#!/bin/sh", result.stdout)
        self.assertIn("swrm-pi-broker/broker.sock", result.stdout)

    def test_cli_needs_a_lane_name_or_the_broker(self):
        for arguments in (["lane", "supervise", "--print"], ["lane", "supervise", "C1", "--broker"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(CLI), *arguments],
                                        env=dict(os.environ, OPENSWARM_HOME=str(self.home)),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("lane name or --broker", result.stderr)


if __name__ == "__main__":
    unittest.main()
