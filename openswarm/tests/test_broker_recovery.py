"""The Pi broker socket recovery: reconnect, validation, alert and dispatch (Refs #1714).

The tests cover the recovery module (probe, capped backoff, budget-free minimal
session, brain log lines, the interrupted marker) and the executor side: a dead
broker socket has to travel the reconnect path instead of failing the dispatch
directly, and the health output has to report the interrupted Pi channel.
"""
import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = ROOT / "bin" / "openswarm"

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

spec = importlib.util.spec_from_file_location(
    "broker_recovery", ROOT / "lib" / "transport" / "broker_recovery.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class FakeBroker:
    """A tiny AF_UNIX server that answers like the MiniMax broker."""

    def __init__(self, path, status=200):
        self.path = str(path)
        self.status = status
        self.requests = 0
        self.lock = threading.Lock()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                head, _, rest = data.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                while len(rest) < length:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    rest += chunk
                with self.lock:
                    self.requests += 1
                body = b'{"id":"msg_1","type":"message"}'
                conn.sendall(("HTTP/1.1 %d OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                              "Connection: close\r\n\r\n" % (self.status, len(body))).encode() + body)
            except OSError:
                # A probe connects and closes right away: that is not a session.
                return

    def close(self):
        try:
            self.server.close()
        except OSError:
            pass


BROKER_SCRIPT = """
import os, socket, sys, threading, time
path, status, pidfile = sys.argv[1], int(sys.argv[2]), sys.argv[3]
if os.fork() != 0:
    os._exit(0)
os.setsid()
if os.fork() != 0:
    os._exit(0)
for fd in (0, 1, 2):
    os.close(fd)
    os.open(os.devnull, os.O_RDWR)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(8)
with open(pidfile, 'w') as stream:
    stream.write(str(os.getpid()))
def serve():
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        try:
            conn.recv(65536)
            body = b'{"id":"msg_1","type":"message"}'
            conn.sendall(b"HTTP/1.1 " + str(status).encode() +
                         b" OK\\r\\nContent-Type: application/json\\r\\nContent-Length: " +
                         str(len(body)).encode() + b"\\r\\nConnection: close\\r\\n\\r\\n" + body)
            conn.close()
        except OSError:
            # A probe connects and closes right away: that is not a session.
            pass
threading.Thread(target=serve, daemon=True).start()
time.sleep(120)
"""


class BrokerRecoveryTests(unittest.TestCase):
    """The recovery module: probe, reconnect with a capped backoff, validate."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openswarm-broker-"))
        self.addCleanup(self._cleanup)
        self.socket_path = self.tmp / "broker.sock"
        self.state_path = self.tmp / "state.json"
        self.brain_log = self.tmp / "brain.log"
        self.slept = []

    def _cleanup(self):
        for path in (self.socket_path,):
            try:
                os.unlink(path)
            except OSError:
                pass
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sleep(self, seconds):
        self.slept.append(seconds)

    def broker(self, status=200):
        server = FakeBroker(self.socket_path, status=status)
        self.addCleanup(server.close)
        return server

    def recovery(self, **kwargs):
        options = {"socket_path": str(self.socket_path), "host": "gocc", "attempts": 3,
                   "backoff_base_s": 1, "backoff_cap_s": 8, "state_path": str(self.state_path),
                   "brain_log": str(self.brain_log), "sleep": self.sleep, "socket_wait_s": 0.5}
        options.update(kwargs)
        return recovery.BrokerRecovery(**options)

    def brain_lines(self):
        if not self.brain_log.exists():
            return []
        return [line for line in self.brain_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    # ---------------------------------------------------------------- probes

    def test_a_missing_socket_is_not_reachable(self):
        self.assertFalse(recovery.probe_socket(str(self.tmp / "absent.sock")))

    def test_a_live_socket_is_reachable(self):
        self.broker()
        self.assertTrue(recovery.probe_socket(str(self.socket_path)))

    def test_the_minimal_session_answers_on_a_live_broker(self):
        broker = self.broker()
        ok, detail = recovery.minimal_session(str(self.socket_path), model="MiniMax-M3.1-Flash")
        self.assertTrue(ok, detail)
        self.assertIn("200", detail)
        self.assertEqual(broker.requests, 1)

    def test_the_minimal_session_reports_a_refused_request(self):
        self.broker(status=403)
        ok, detail = recovery.minimal_session(str(self.socket_path), model="MiniMax-M3.1-Flash")
        self.assertFalse(ok)
        self.assertIn("403", detail)

    def test_the_minimal_session_needs_a_model(self):
        self.broker()
        ok, detail = recovery.minimal_session(str(self.socket_path), model=None)
        self.assertFalse(ok)
        self.assertIn("model", detail)

    # ---------------------------------------------------------------- recovery

    def test_a_dead_socket_is_restarted_and_verified(self):
        broker = self.broker()
        status = self.recovery(restart=lambda: None).ensure_ready()
        self.assertTrue(status["ok"])
        self.assertEqual(status["result"], recovery.RESULT_HEALTHY)
        self.assertFalse(status["interrupted"])
        self.assertEqual(broker.requests, 0, "a healthy broker needs no session")

    def test_a_dead_broker_is_restarted_and_the_dispatch_continues(self):
        started, validated = [], []

        def restart():
            started.append(1)
            self.broker()

        def validate():
            validated.append(1)
            return True, "minimal session answered with 200"

        status = self.recovery(restart=restart, validate=validate).ensure_ready()
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["result"], recovery.RESULT_RECOVERED)
        self.assertFalse(status["interrupted"])
        self.assertEqual(len(started), 1)
        self.assertEqual(len(validated), 1, "a restarted broker is verified with a minimal session")
        self.assertEqual(self.slept, [], "a broker that answers again needs no backoff")

    def test_a_broker_that_stays_dead_keeps_the_host_interrupted_and_alerts(self):
        status = self.recovery(restart=lambda: None).ensure_ready()
        self.assertFalse(status["ok"])
        self.assertTrue(status["interrupted"])
        self.assertEqual(status["result"], recovery.RESULT_RECONNECT_FAILED)
        self.assertEqual(status["alert"], recovery.ALERT_INTERRUPTED)
        self.assertEqual(status["failures"], 3)
        self.assertEqual(self.slept, [1, 2], "the backoff doubles between the attempts")

    def test_a_failing_restart_is_reported_as_a_reconnect_failure(self):
        def restart():
            raise RuntimeError("broker service is not running")

        status = self.recovery(restart=restart).ensure_ready()
        self.assertFalse(status["ok"])
        self.assertEqual(status["result"], recovery.RESULT_RECONNECT_FAILED)
        self.assertIn("broker service is not running", status["detail"])

    def test_a_missing_restart_command_is_reported(self):
        status = self.recovery(restart=None).ensure_ready()
        self.assertFalse(status["ok"])
        self.assertEqual(status["result"], recovery.RESULT_RESTART_UNAVAILABLE)
        self.assertTrue(status["interrupted"])

    def test_a_failed_validation_keeps_the_interrupted_marker(self):
        def restart():
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
            self.broker()

        status = self.recovery(restart=restart,
                               validate=lambda: (False, "minimal session answered with 500")).ensure_ready()
        self.assertFalse(status["ok"])
        self.assertTrue(status["interrupted"])
        self.assertEqual(status["result"], recovery.RESULT_VALIDATION_FAILED)
        self.assertEqual(status["alert"], recovery.ALERT_INTERRUPTED)

    def test_an_interrupted_channel_is_only_released_by_a_validated_session(self):
        self.broker()
        recovery.write_state(str(self.state_path), {"hosts": {"gocc": {
            "host": "gocc", "socket": str(self.socket_path), "ok": False, "interrupted": True,
            "result": recovery.RESULT_VALIDATION_FAILED, "detail": "minimal session answered with 500",
            "attempts": 3, "reconnects": 3, "failures": 3, "alert": recovery.ALERT_INTERRUPTED}}})
        calls = []

        def validate():
            calls.append(1)
            return False, "minimal session answered with 500"

        status = self.recovery(restart=lambda: None, validate=validate).ensure_ready()
        self.assertTrue(calls, "an interrupted channel has to be validated again")
        self.assertFalse(status["ok"])
        self.assertTrue(status["interrupted"], "a failed validation keeps the interrupted marker")

        status = self.recovery(restart=lambda: None,
                               validate=lambda: (True, "minimal session answered with 200")).ensure_ready()
        self.assertTrue(status["ok"])
        self.assertFalse(status["interrupted"])
        self.assertEqual(status["result"], recovery.RESULT_RECOVERED)

    def test_the_state_file_keeps_the_host_record(self):
        self.recovery(restart=lambda: None).ensure_ready()
        state = recovery.read_state(str(self.state_path))
        record = recovery.host_state(state, "gocc")
        self.assertTrue(record["interrupted"])
        self.assertEqual(record["failures"], 3)
        self.assertEqual([row["host"] for row in recovery.interrupted_hosts(state)], ["gocc"])

    # ---------------------------------------------------------------- logging

    def test_the_brain_log_records_the_host_the_attempt_and_the_result(self):
        self.recovery(restart=lambda: None).ensure_ready()
        lines = self.brain_lines()
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("host=gocc", lines[0])
        self.assertIn("reconnect#1", lines[0])
        self.assertIn("result=%s" % recovery.RESULT_RECONNECT_FAILED, lines[0])
        self.assertIn("reconnect#3", lines[2])

    def test_a_healthy_broker_writes_no_brain_line(self):
        self.broker()
        self.recovery(restart=lambda: None).ensure_ready()
        self.assertEqual(self.brain_lines(), [])

    def test_the_brain_line_shape(self):
        line = recovery.brain_line("gocc", 2, recovery.RESULT_RECOVERED, "minimal session answered with 200")
        self.assertTrue(line.startswith("- "))
        self.assertIn("UTC+8 pi-broker: host=gocc reconnect#2 result=recovered", line)

    # ---------------------------------------------------------------- backoff

    def test_the_backoff_doubles_and_is_capped(self):
        self.assertEqual(recovery.backoff_delay(1, 1, 30), 1)
        self.assertEqual(recovery.backoff_delay(2, 1, 30), 2)
        self.assertEqual(recovery.backoff_delay(6, 1, 30), 30)


class ExecutorBrokerDispatchTests(unittest.TestCase):
    """The executor: a dead broker socket travels the reconnect path."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openswarm-broker-cli-"))
        self.addCleanup(self._cleanup)
        self.socket_path = self.tmp / "broker.sock"
        self.state_path = pathlib.Path(xs.DATA) / "broker-recovery.json"
        self.policy_path = pathlib.Path(xs.DATA) / "policy.local.json"
        self.previous_policy = self.policy_path.read_text(encoding="utf-8") if self.policy_path.exists() else None
        self.addCleanup(self._restore_policy)
        self.write_policy({})
        self.state_path.unlink(missing_ok=True)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restore_policy(self):
        if self.previous_policy is None:
            self.policy_path.unlink(missing_ok=True)
        else:
            self.policy_path.write_text(self.previous_policy, encoding="utf-8")

    def write_policy(self, broker):
        self.policy_path.write_text(json.dumps({"pi": {"broker": broker}}), encoding="utf-8")

    def start_broker_script(self, status=200):
        """A restart command that starts a broker in the background and returns at once."""
        script = self.tmp / "broker.py"
        script.write_text(BROKER_SCRIPT, encoding="utf-8")
        pidfile = self.tmp / "broker.pid"
        command = "%s %s %s %s %s" % (sys.executable, script, self.socket_path, status, pidfile)
        self.addCleanup(self._kill_broker, pidfile)
        return command

    def _kill_broker(self, pidfile):
        try:
            pid = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            return
        try:
            os.kill(pid, 15)
        except OSError:
            pass

    def lane(self):
        return {"backend": "pi", "broker_socket": str(self.socket_path), "model": "MiniMax-M3.1-Flash"}

    # ---------------------------------------------------------------- tests

    def test_a_dead_socket_is_restarted_and_the_dispatch_continues(self):
        self.write_policy({"restart_cmd": self.start_broker_script(), "attempts": 3,
                           "socket_wait_s": 5, "restart_grace_s": 5})
        status = xs.pi_broker_ensure_ready(self.lane(), host="gocc")
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["result"], "recovered")
        self.assertFalse(status["interrupted"])
        self.assertEqual(status["host"], "gocc")

    def test_a_broker_that_stays_dead_alerts_the_host(self):
        self.write_policy({"restart_cmd": "false", "attempts": 2, "socket_wait_s": 0.2,
                           "restart_grace_s": 0.5, "backoff_base_s": 0.01})
        status = xs.pi_broker_ensure_ready(self.lane(), host="gocc")
        self.assertFalse(status["ok"])
        self.assertTrue(status["interrupted"])
        self.assertEqual(status["alert"], "pi channel interrupted")
        record = xs.pi_broker_record(host="gocc")
        self.assertTrue(record["interrupted"])
        self.assertEqual(record["failures"], 2)

    def test_the_pi_dispatch_reports_the_broker_instead_of_a_vague_error(self):
        self.write_policy({"restart_cmd": "false", "attempts": 1, "socket_wait_s": 0.2,
                           "restart_grace_s": 0.5, "backoff_base_s": 0.01})
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        events = str(run_dir / "events.jsonl")
        code, final, meta, error = xs.exec_pi({"model": "MiniMax-M3.1-Flash"}, {}, "task", str(run_dir),
                                              str(run_dir), 60, events)
        self.assertEqual(code, 1)
        self.assertIsNone(final)
        self.assertIn("pi broker unavailable", error)
        self.assertEqual(xs.executor_error_class(code, final, error, "pi"), "broker_unavailable")
        self.assertFalse((run_dir / "pi-sessions").exists(), "no Pi session is started without a broker")

    def test_the_health_output_reports_an_interrupted_pi_channel(self):
        self.write_policy({"restart_cmd": "false", "attempts": 1, "socket_wait_s": 0.2,
                           "restart_grace_s": 0.5, "backoff_base_s": 0.01})
        xs.pi_broker_ensure_ready(self.lane())
        server = FakeBroker(self.socket_path)
        self.addCleanup(server.close)
        check = xs.doctor_pi_broker(self.lane())
        self.assertEqual(check["status"], "fail")
        self.assertIn("pi channel interrupted", check["detail"])

    def test_the_brain_log_of_the_executor_records_the_reconnect(self):
        brain = self.tmp / "brain.log"
        self.write_policy({"restart_cmd": "false", "attempts": 1, "brain_log": str(brain),
                           "socket_wait_s": 0.2, "restart_grace_s": 0.5, "backoff_base_s": 0.01})
        xs.pi_broker_ensure_ready(self.lane(), host="gocc")
        lines = [line for line in brain.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("host=gocc", lines[0])
        self.assertIn("reconnect#1", lines[0])

    # ---------------------------------------------------------------- lane dispatch

    class FakeChild:
        """A lane child that refuses the turn: the test only watches the gate."""

        pid = None
        pgid = None

        def __init__(self):
            self.sent = []

        def send(self, message):
            self.sent.append(message)
            raise RuntimeError("fake lane child refuses the turn")

    def lane_turn(self, lane):
        run_dir = self.tmp / "lane-run"
        run_dir.mkdir(exist_ok=True)
        spec = {"run_id": "run-1", "started": time.time(), "budget_minutes": 5, "task": "task"}
        child = self.FakeChild()
        return xs.lane_run_turn(lane, child, spec, "prompt", {"path": str(self.tmp)}, str(run_dir),
                                None, {}, []), child

    def test_the_lane_dispatch_reports_the_broker_instead_of_a_vague_error(self):
        self.write_policy({"restart_cmd": "false", "attempts": 1, "socket_wait_s": 0.2,
                           "restart_grace_s": 0.5, "backoff_base_s": 0.01})
        lane = dict(self.lane(), name="P1")
        result, child = self.lane_turn(lane)
        final, usage, error_class, rc, event_file, detail = result
        self.assertEqual(error_class, "broker_unavailable")
        self.assertIn("pi broker unavailable", detail)
        self.assertEqual(child.sent, [], "no turn is sent to a lane without a broker")

    def test_the_lane_dispatch_continues_after_the_reconnect(self):
        self.write_policy({"restart_cmd": self.start_broker_script(), "attempts": 2,
                           "socket_wait_s": 5, "restart_grace_s": 5})
        lane = dict(self.lane(), name="P1")
        with self.assertRaises(RuntimeError):
            self.lane_turn(lane)
        record = xs.pi_broker_record()
        self.assertTrue(record["ok"], record)
        self.assertFalse(record["interrupted"])


class FleetHealthPiChannelTests(unittest.TestCase):
    """`openswarm fleet health` warns when the Pi channel of a host is broken."""

    DISK = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/root 1000 700 300 70% /\n")
    MEMORY = ("              total used free shared buff/cache available\n"
              "Mem: 8000 3000 2000 100 3000 5000\n")

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openswarm-fleet-pi-"))
        self.addCleanup(self._cleanup)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.lanes_file = self.home / "lanes.json"
        self.lanes_file.write_text(json.dumps({"lanes": [
            {"name": "P1", "backend": "pi", "model": "MiniMax-M3.1-Flash", "repo": str(self.repo),
             "host": "worker-a", "broker_socket": str(self.tmp / "broker.sock")},
        ]}), encoding="utf-8")
        self.previous = os.environ.get("OPENSWARM_LANES_FILE")
        os.environ["OPENSWARM_LANES_FILE"] = str(self.lanes_file)
        self.addCleanup(self._restore_lanes)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restore_lanes(self):
        if self.previous is None:
            os.environ.pop("OPENSWARM_LANES_FILE", None)
        else:
            os.environ["OPENSWARM_LANES_FILE"] = self.previous

    def runner(self, broker_reachable):
        def run(host, command):
            if "socket.AF_UNIX" in command:
                if broker_reachable:
                    return ""
                raise OSError("broker socket is not reachable")
            if command == "df -P /":
                return self.DISK
            if command == "df -Pi /":
                return ("Filesystem Inodes IUsed IFree IUse% Mounted on\n"
                        "/dev/root 1000 950 50 10% /\n")
            if command == "free -m":
                return self.MEMORY
            raise OSError("unexpected probe: " + command)
        return run

    def health(self, broker_reachable):
        import argparse
        import contextlib
        import io
        args = argparse.Namespace(hosts=None, json=True, apply=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            return xs.cmd_fleet_health(args, runner=self.runner(broker_reachable),
                                       hosts={"worker-a": {"name": "worker-a", "kind": "local"}},
                                       now=1700000000.0, home=self.home)

    def test_a_broken_pi_channel_is_an_alert_not_a_pause(self):
        report = self.health(broker_reachable=False)
        row = report["hosts"][0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["decision"], "none")
        self.assertEqual(len(row["alerts"]), 1, row)
        self.assertIn("pi channel interrupted", row["alerts"][0])
        self.assertFalse(report["ok"])

    def test_a_reachable_pi_channel_has_no_alert(self):
        report = self.health(broker_reachable=True)
        row = report["hosts"][0]
        self.assertNotIn("alerts", row)
        self.assertTrue(report["ok"])



if __name__ == "__main__":
    unittest.main()
