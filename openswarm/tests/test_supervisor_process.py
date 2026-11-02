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
CLI = ROOT / "bin" / "openswarm"
TMP = ROOT.parent / ".openswarm-tmp"
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()
xs.__path__ = [str(ROOT)]
# Other suites patch the CLI through ``sys.modules["openswarm"]``; a second
# registration here would point those patches at this module instead.
if not hasattr(sys.modules.get("openswarm"), "lane_config"):
    sys.modules["openswarm"] = xs


class ProcessSupervisorTests(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TMP)
        self.home = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def script(self, command):
        with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)}):
            return xs.generate_process_supervisor("fake-lane", command)

    def launch(self, command):
        script = self.home / "supervisor.sh"
        script.write_text(self.script(command))
        script.chmod(0o700)
        syntax = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        process = subprocess.Popen(["sh", str(script), "run"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return script, process

    def wait_for(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail("condition did not become true before timeout")

    def test_restart_backoff_stop_and_syntax(self):
        cmd = [sys.executable, "-c", "import time; print(time.time(), flush=True)"]
        script, process = self.launch(cmd)
        log = self.home / "logs" / "fake-lane.log"
        try:
            self.wait_for(lambda: log.exists() and len(log.read_text().splitlines()) >= 3)
            starts = [float(x) for x in log.read_text().splitlines()]
            gaps = [b - a for a, b in zip(starts, starts[1:])]
            self.assertGreater(gaps[1], gaps[0] + 0.7)
            subprocess.run(["sh", str(script), "stop"], check=True, timeout=15)
            self.assertFalse((self.home / "supervise" / "fake-lane.pid").exists())
            self.assertFalse((self.home / "supervise" / "fake-lane.child.pid").exists())
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def test_stop_kills_forked_descendant(self):
        grandchild_file = self.home / "grandchild.pid"
        code = ("import os,subprocess,sys,time; ")
        code += "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],start_new_session=True); "
        code += f"open({str(grandchild_file)!r},'w').write(str(p.pid)); time.sleep(120)"
        script, process = self.launch([sys.executable, "-c", code])
        try:
            self.wait_for(grandchild_file.exists)
            grandchild = int(grandchild_file.read_text())
            subprocess.run(["sh", str(script), "stop"], check=True, timeout=15)
            self.wait_for(lambda: not self.pid_running(grandchild))
            self.assertFalse((self.home / "supervise" / "fake-lane.pid").exists())
            self.assertFalse((self.home / "supervise" / "fake-lane.child.pid").exists())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

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
            subprocess.run(["sh", str(script), "stop"], check=True, timeout=15)
            self.assertFalse((self.home / "supervise" / "fake-lane.pid").exists())
            self.assertFalse((self.home / "supervise" / "fake-lane.child.pid").exists())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    @staticmethod
    def pid_running(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        try:
            stat_text = pathlib.Path(f"/proc/{pid}/stat").read_text()
            state = stat_text.rsplit(")", 1)[1].split()[0]
            return state not in ("Z", "X")
        except (OSError, IndexError):
            pass
        try:
            out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                                 capture_output=True, text=True).stdout.strip()
            if out:
                return not any(char in out for char in ("Z", "z"))
        except Exception:
            pass
        return True

    def test_broker_makes_socket_directory(self):
        socket_path = self.home / "run" / "broker.sock"
        marker = self.home / "broker-started"
        code = (f"import pathlib,sys,time; assert pathlib.Path(sys.argv[1]).parent.is_dir(); "
                f"pathlib.Path({str(marker)!r}).write_text(sys.argv[1]); time.sleep(120)")
        with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)}):
            text = xs.generate_process_supervisor(
                "broker", [sys.executable, "-c", code, "{socket}"], str(socket_path))
        script = self.home / "broker.sh"
        script.write_text(text)
        script.chmod(0o700)
        syntax = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        env = dict(os.environ, OPENSWARM_PI_BROKER_SOCKET=str(socket_path))
        proc = subprocess.Popen(["sh", str(script), "run"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            self.wait_for(marker.exists)
            self.assertEqual(marker.read_text(), str(socket_path))
            self.assertTrue(socket_path.parent.is_dir())
            subprocess.run(["sh", str(script), "stop"], check=True, timeout=15)
            self.assertFalse((self.home / "supervise" / "broker.pid").exists())
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)

    def test_broker_supervisor_passes_socket_auth_module_and_model(self):
        socket_path = self.home / "run" / "broker.sock"
        with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)}):
            text = xs.generate_process_supervisor("pi-broker", ["node", "broker.mjs"], str(socket_path))
        self.assertIn('"$BROKER_SOCKET"', text)
        self.assertIn("OPENSWARM_PI_AUTH_MODULE", text)
        self.assertIn("node-v24.14.1/bin/node", text)
        self.assertIn("MiniMax-M3.1-Flash", text)
        self.assertIn('mkdir -p "$(dirname "$BROKER_SOCKET")"', text)

    def test_start_detaches_the_supervisor_from_the_terminal(self):
        text = self.script([sys.executable, "-c", "pass"])
        self.assertIn("setsid", text)
        self.assertIn("nohup", text)
        self.assertIn("</dev/null", text)
        self.assertIn("trap '' HUP", text)

    def test_start_runs_the_generated_script(self):
        script = self.home / "supervisor.sh"
        script.write_text(self.script([sys.executable, "-c", "import time; time.sleep(120)"]))
        script.chmod(0o700)
        subprocess.run(["sh", str(script), "start"], check=True, capture_output=True, text=True)
        pidfile = self.home / "supervise" / "fake-lane.pid"
        try:
            self.wait_for(pidfile.exists)
            self.assertEqual(subprocess.run(["sh", str(script), "status"]).returncode, 0)
        finally:
            subprocess.run(["sh", str(script), "stop"], check=True, timeout=15)
        self.assertFalse(pidfile.exists())

    def test_cli_prints_broker_supervisor(self):
        result = subprocess.run([sys.executable, str(CLI), "lane", "supervise", "--broker", "--print"],
                                env=dict(os.environ, OPENSWARM_HOME=str(self.home)),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("#!/bin/sh", result.stdout)
        self.assertIn("Library/Caches/openswarm-pi-broker/broker.sock", result.stdout)


if __name__ == "__main__":
    unittest.main()
