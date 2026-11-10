"""The Pi broker socket recovery: reconnect, validation, alert and dispatch (Refs #1714).

The tests cover the recovery module (probe, capped backoff, budget-free minimal
session, brain log lines, the interrupted marker) and the executor side: a dead
broker socket has to travel the reconnect path instead of failing the dispatch
directly, and the fleet status has to report the interrupted Pi channel.

BLOCKER mapping (lane review of PR #1778, round 2).  The line numbers are the
ones of the delivered files; the results are the ones of the local run.

* B1 — a dead broker is really restarted and the dispatch continues:
  ``swrm/policy.json:238`` names the restart command of the deployment
  (``pi.broker.restart_cmd`` = ``bin/pi-broker-restart``),
  ``swrm/bin/pi-broker-restart:90`` starts the broker again on the probed
  socket, and ``swrm/bin/swrm:3671`` (``pi_broker_settings``),
  ``swrm/bin/swrm:3707`` (``pi_broker_restart_argv``) and
  ``swrm/bin/swrm:3747`` (``pi_broker_restart``) run it detached, hand it the
  socket and report the reason of a failed restart.  The recovery itself runs
  before every dispatch (``swrm/bin/swrm:3804`` ``pi_broker_ensure_ready``,
  ``swrm/bin/swrm:5282`` ``exec_pi``, ``swrm/bin/swrm:8302`` ``lane_run_turn``)
  and the interrupted host reaches the health output
  (``swrm/bin/swrm:3846`` ``pi_broker_channel_status``,
  ``swrm/bin/swrm:10033`` ``fleet_pi_channel_alert``).  Tests:
  ``DefaultBrokerRestartTests.test_the_default_restart_command_brings_the_broker_back``,
  ``DefaultBrokerRestartTests.test_the_default_restart_command_is_the_one_of_the_deployment``,
  ``DefaultBrokerRestartTests.test_the_restart_command_reports_why_it_failed``,
  ``BrokerRestartScriptTests.test_the_script_execs_the_broker_on_the_given_socket``,
  ``ExecutorBrokerDispatchTests.test_a_dead_socket_is_restarted_and_the_dispatch_continues``,
  ``BrokerDispatchIntegrationTests.test_the_dispatch_continues_after_the_broker_was_restarted``,
  ``BrokerRecoveryTests.test_a_broker_that_stays_dead_keeps_the_host_interrupted_and_alerts``,
  ``ExecutorBrokerDispatchTests.test_a_broker_that_stays_dead_alerts_the_host``,
  ``ExecutorBrokerDispatchTests.test_the_health_output_reports_an_interrupted_pi_channel``,
  ``FleetPiChannelTests.test_a_broken_pi_channel_is_an_alert_not_a_pause``.
* B2 — the acceptance coverage at dispatch level: the lane is really dispatched
  (``swrm lane up`` + ``swrm lane assign``) after the broker was restarted, and
  a failed validation keeps the dispatch blocked.  Tests:
  ``BrokerDispatchIntegrationTests.test_the_dispatch_continues_after_the_broker_was_restarted``,
  ``BrokerDispatchIntegrationTests.test_a_failed_validation_keeps_the_dispatch_blocked``,
  ``BrokerDispatchIntegrationTests.test_a_dead_broker_without_a_restart_keeps_the_dispatch_blocked``,
  ``BrokerRecoveryTests.test_a_failed_validation_keeps_the_interrupted_marker``,
  ``BrokerRecoveryTests.test_an_interrupted_channel_is_only_released_by_a_validated_session``,
  ``ExecutorBrokerDispatchTests.test_the_pi_dispatch_reports_the_broker_instead_of_a_vague_error``,
  ``ExecutorBrokerDispatchTests.test_the_lane_dispatch_reports_the_broker_instead_of_a_vague_error``.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURES = HERE / "fixtures"
CLI = ROOT / "bin" / "swrm"

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

spec = importlib.util.spec_from_file_location(
    "broker_recovery", ROOT / "bin" / "broker_recovery.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def short_dir(prefix="swrm-broker-", reserve=48):
    """A short temporary directory: an AF_UNIX socket path is limited to about 108 bytes.

    The temporary directory of a run can be long (the CI puts it below the
    checkout), so a short one is used when the default would not fit.
    """
    for base in (tempfile.gettempdir(), "/tmp"):
        try:
            path = pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        except OSError:
            continue
        if len(os.fsencode(str(path))) + reserve <= 100:
            return path
        shutil.rmtree(path, ignore_errors=True)
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix))


def broker_socket_dir(prefix="swrm-broker-"):
    """A short directory for a broker socket: an AF_UNIX path is limited to about 104 bytes."""
    return short_dir(prefix, reserve=12)


FAKE_NODE = '''#!/usr/bin/env python3
"""Stand-in for the node runtime of the broker: it serves the socket like the broker does."""
import socket
import sys
import threading

path = sys.argv[2]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(8)


def handle(conn):
    with conn:
        try:
            data = b""
            while b"\\r\\n\\r\\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            body = b'{"id":"msg_1","type":"message"}'
            conn.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\nContent-Length: "
                         + str(len(body)).encode() + b"\\r\\nConnection: close\\r\\n\\r\\n" + body)
        except OSError:
            return


def serve():
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


serve()
'''


FAKE_NODE_ARGV = '''#!/usr/bin/env python3
"""Stand-in for the node runtime: it records the arguments of the broker."""
import os
import sys

with open(os.environ["FAKE_NODE_ARGV"], "w", encoding="utf-8") as handle:
    handle.write("\\n".join(sys.argv))
'''


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
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="swrm-broker-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.socket_dir = broker_socket_dir()
        self.addCleanup(shutil.rmtree, self.socket_dir, ignore_errors=True)
        self.socket_path = self.socket_dir / "broker.sock"
        self.state_path = self.tmp / "state.json"
        self.brain_log = self.tmp / "brain.log"
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)

    def broker(self, status=200):
        server = FakeBroker(self.socket_path, status=status)
        self.addCleanup(server.close)
        return server

    def recovery(self, **kwargs):
        options = {"socket_path": str(self.socket_path), "host": "hostA", "attempts": 3,
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
        recovery.write_state(str(self.state_path), {"hosts": {"hostA": {
            "host": "hostA", "socket": str(self.socket_path), "ok": False, "interrupted": True,
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
        record = recovery.host_state(state, "hostA")
        self.assertTrue(record["interrupted"])
        self.assertEqual(record["failures"], 3)
        self.assertEqual([row["host"] for row in recovery.interrupted_hosts(state)], ["hostA"])

    # ---------------------------------------------------------------- logging

    def test_the_brain_log_records_the_host_the_attempt_and_the_result(self):
        self.recovery(restart=lambda: None).ensure_ready()
        lines = self.brain_lines()
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("host=hostA", lines[0])
        self.assertIn("reconnect#1", lines[0])
        self.assertIn("result=%s" % recovery.RESULT_RECONNECT_FAILED, lines[0])
        self.assertIn("reconnect#3", lines[2])

    def test_a_healthy_broker_writes_no_brain_line(self):
        self.broker()
        self.recovery(restart=lambda: None).ensure_ready()
        self.assertEqual(self.brain_lines(), [])

    def test_the_brain_line_shape(self):
        line = recovery.brain_line("hostA", 2, recovery.RESULT_RECOVERED, "minimal session answered with 200")
        self.assertTrue(line.startswith("- "))
        self.assertIn("UTC+8 pi-broker: host=hostA reconnect#2 result=recovered", line)

    # ---------------------------------------------------------------- backoff

    def test_the_backoff_doubles_and_is_capped(self):
        self.assertEqual(recovery.backoff_delay(1, 1, 30), 1)
        self.assertEqual(recovery.backoff_delay(2, 1, 30), 2)
        self.assertEqual(recovery.backoff_delay(6, 1, 30), 30)


class ExecutorBrokerDispatchTests(unittest.TestCase):
    """The executor: a dead broker socket travels the reconnect path."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="swrm-broker-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.socket_dir = broker_socket_dir()
        self.addCleanup(shutil.rmtree, self.socket_dir, ignore_errors=True)
        self.socket_path = self.socket_dir / "broker.sock"
        self.state_path = self.home / "broker-recovery.json"
        self.policy_path = self.home / "policy.local.json"
        self.previous_home = os.environ.get("OPENSWARM_HOME")
        self.previous_socket = os.environ.get("OPENSWARM_PI_BROKER_SOCKET")
        self.previous_bin = os.environ.get("OPENSWARM_PI_BIN")
        os.environ["OPENSWARM_HOME"] = str(self.home)
        os.environ["OPENSWARM_PI_BROKER_SOCKET"] = str(self.socket_path)
        os.environ["OPENSWARM_PI_BIN"] = str(self.fake_isolated_binary())
        self.addCleanup(self._restore_env)
        # The executor keeps its state and its policy in the swrm home: point both at the
        # scratch home of this test, so the recovery never touches the real one.
        patcher = mock.patch.object(xs, "DATA", str(self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_policy({})
        self.state_path.unlink(missing_ok=True)

    def _restore_env(self):
        for name, value in (("OPENSWARM_HOME", self.previous_home),
                            ("OPENSWARM_PI_BROKER_SOCKET", self.previous_socket),
                            ("OPENSWARM_PI_BIN", self.previous_bin)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def fake_isolated_binary(self):
        """A stand-in for bin/pi-isolated: the run goes through the broker socket."""
        path = self.tmp / "pi-isolated"
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

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
        return {"name": "P1", "backend": "pi", "broker_socket": str(self.socket_path),
                "model": "MiniMax-M3.1-Flash"}

    # ---------------------------------------------------------------- tests

    def test_a_dead_socket_is_restarted_and_the_dispatch_continues(self):
        self.write_policy({"restart_cmd": self.start_broker_script(), "attempts": 3,
                           "socket_wait_s": 5, "restart_grace_s": 5})
        status = xs.pi_broker_ensure_ready(self.lane(), host="hostA")
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["result"], "recovered")
        self.assertFalse(status["interrupted"])
        self.assertEqual(status["host"], "hostA")

    def test_a_broker_that_stays_dead_alerts_the_host(self):
        self.write_policy({"restart_cmd": "false", "attempts": 2, "socket_wait_s": 0.2,
                           "restart_grace_s": 0.5, "backoff_base_s": 0.01})
        status = xs.pi_broker_ensure_ready(self.lane(), host="hostA")
        self.assertFalse(status["ok"])
        self.assertTrue(status["interrupted"])
        self.assertEqual(status["alert"], "pi channel interrupted")
        record = xs.pi_broker_record(host="hostA")
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

    def test_a_live_broker_lets_the_pi_dispatch_run(self):
        # The isolated Pi talks to the broker socket: a live socket must not block the run.
        broker = FakeBroker(self.socket_path)
        self.addCleanup(broker.close)
        self.write_policy({"attempts": 1, "socket_wait_s": 0.2})
        run_dir = self.tmp / "run-live"
        run_dir.mkdir()
        events = str(run_dir / "events.jsonl")
        code, final, meta, error = xs.exec_pi({"model": "MiniMax-M3.1-Flash"}, {}, "task", str(run_dir),
                                              str(run_dir), 60, events)
        self.assertNotIn("pi broker unavailable", error or "")
        self.assertTrue((run_dir / "pi-sessions").exists(), "a live broker lets the Pi run start")

    def test_the_health_output_reports_an_interrupted_pi_channel(self):
        self.write_policy({"restart_cmd": "false", "attempts": 1, "socket_wait_s": 0.2,
                           "restart_grace_s": 0.5, "backoff_base_s": 0.01})
        xs.pi_broker_ensure_ready(self.lane())
        server = FakeBroker(self.socket_path)
        self.addCleanup(server.close)
        status = xs.pi_broker_channel_status([self.lane()])
        self.assertIsNotNone(status)
        self.assertFalse(status["ok"])
        self.assertIn("pi channel interrupted", xs.fleet_pi_channel_alert(status))

    def test_the_brain_log_of_the_executor_records_the_reconnect(self):
        brain = self.tmp / "brain.log"
        self.write_policy({"restart_cmd": "false", "attempts": 1, "brain_log": str(brain),
                           "socket_wait_s": 0.2, "restart_grace_s": 0.5, "backoff_base_s": 0.01})
        xs.pi_broker_ensure_ready(self.lane(), host="hostA")
        lines = [line for line in brain.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("host=hostA", lines[0])
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


class FleetPiChannelTests(unittest.TestCase):
    """`swrm fleet status` warns when the Pi channel of a host is broken."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="swrm-fleet-pi-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.socket_dir = broker_socket_dir()
        self.addCleanup(shutil.rmtree, self.socket_dir, ignore_errors=True)
        self.socket_path = self.socket_dir / "broker.sock"
        self.previous_home = os.environ.get("OPENSWARM_HOME")
        os.environ["OPENSWARM_HOME"] = str(self.home)
        self.addCleanup(self._restore_env)
        patcher = mock.patch.object(xs, "DATA", str(self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.home / "lanes.json").write_text(json.dumps({"lanes": [
            {"name": "P1", "backend": "pi", "model": "MiniMax-M3.1-Flash", "repo": str(self.repo),
             "broker_socket": str(self.socket_path)},
        ]}), encoding="utf-8")
        (self.home / "policy.local.json").write_text(json.dumps({"pi": {"broker": {
            "restart_cmd": "false", "attempts": 1, "socket_wait_s": 0.2, "restart_grace_s": 0.5,
            "backoff_base_s": 0.01}}}), encoding="utf-8")

    def _restore_env(self):
        if self.previous_home is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = self.previous_home

    def test_a_broken_pi_channel_is_an_alert_not_a_pause(self):
        status = xs.fleet_snapshot()
        channel = status["pi_channel"]
        self.assertIsNotNone(channel)
        self.assertFalse(channel["ok"])
        alert = xs.fleet_pi_channel_alert(channel)
        self.assertIn("pi channel interrupted", alert)

    def test_a_healthy_pi_channel_has_no_alert(self):
        server = FakeBroker(self.socket_path)
        self.addCleanup(server.close)
        status = xs.fleet_snapshot()
        channel = status["pi_channel"]
        self.assertTrue(channel["ok"], channel)
        self.assertIsNone(xs.fleet_pi_channel_alert(channel))

    def test_the_fleet_status_prints_the_alert(self):
        with mock.patch.object(xs, "host_json", return_value={
                "lanes": [], "active_runs": [], "providers": {}, "backends": {},
                "lane_config_valid": True,
                "pi_channel": {"ok": False, "detail": "broker socket /run/swrm-pi/broker.sock does not answer"}}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=False))
            self.assertIn("alert: pi channel interrupted", output.getvalue())


class DefaultBrokerRestartTests(unittest.TestCase):
    """The shipped default restart command brings a dead broker back (Refs #1714).

    The default of policy.json is ``bin/pi-broker-restart``: the script that
    starts the broker of this deployment again on the socket the executor
    probes.  A fake node stands in for the real runtime, so the default path is
    covered without a MiniMax credential.
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="swrm-broker-default-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.socket_dir = broker_socket_dir()
        self.addCleanup(shutil.rmtree, self.socket_dir, ignore_errors=True)
        self.socket_path = self.socket_dir / "broker.sock"
        self.node = self.tmp / "node"
        self.node.write_text(FAKE_NODE, encoding="utf-8")
        self.node.chmod(0o755)
        self.auth = self.tmp / "auth-storage.js"
        self.auth.write_text("export class AuthStorage {}\n", encoding="utf-8")
        self.previous = {name: os.environ.get(name) for name in
                         ("OPENSWARM_HOME", "OPENSWARM_PI_NODE", "OPENSWARM_PI_AUTH_MODULE")}
        os.environ["OPENSWARM_HOME"] = str(self.home)
        os.environ["OPENSWARM_PI_NODE"] = str(self.node)
        os.environ["OPENSWARM_PI_AUTH_MODULE"] = str(self.auth)
        self.addCleanup(self._restore_env)
        patcher = mock.patch.object(xs, "DATA", str(self.home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_env(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def lane(self):
        return {"name": "P1", "backend": "pi", "broker_socket": str(self.socket_path),
                "model": "MiniMax-M3.1-Flash"}

    def test_the_default_restart_command_is_the_one_of_the_deployment(self):
        # The policy of the deployment has to name a restart command that exists:
        # without it a dead broker can only be reported, never recovered.
        settings = xs.pi_broker_settings()
        self.assertEqual(settings["restart_cmd"], "bin/pi-broker-restart")
        argv = xs.pi_broker_restart_argv(settings["restart_cmd"])
        self.assertEqual(argv, [str(ROOT / "bin" / "pi-broker-restart")])
        self.assertTrue(os.access(argv[0], os.X_OK))

    def test_the_default_restart_command_brings_the_broker_back(self):
        # No restart_cmd of its own: the default of the policy has to recover the host.
        status = xs.pi_broker_ensure_ready(self.lane(), host="hostA")
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["result"], "recovered")
        self.assertFalse(status["interrupted"])
        self.assertTrue(self.socket_path.exists(), "the default command started the broker")

    def test_the_restart_command_reports_why_it_failed(self):
        # A restart that dies right away is a failed reconnect, with its reason.
        (self.home / "policy.local.json").write_text(json.dumps(
            {"pi": {"broker": {"restart_cmd": "false", "attempts": 1}}}), encoding="utf-8")
        status = xs.pi_broker_ensure_ready(self.lane(), host="hostA")
        self.assertFalse(status["ok"])
        self.assertEqual(status["result"], "reconnect_failed")
        self.assertIn("broker restart failed", status["detail"])


class BrokerDispatchIntegrationTests(unittest.TestCase):
    """The dispatch itself: a dead broker is recovered, a broken one blocks the run.

    These tests drive the real dispatch (``swrm lane up`` + ``swrm lane assign``)
    instead of calling the turn helper, so they prove what the Pi lane of a host
    does when its broker socket dies.
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="swrm-broker-lane-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # The lane home holds an AF_UNIX control socket: it has to stay short.
        self.root = short_dir("swrm-broker-lane-", reserve=64)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.socket_dir = broker_socket_dir()
        self.addCleanup(shutil.rmtree, self.socket_dir, ignore_errors=True)
        self.socket_path = self.socket_dir / "broker.sock"
        self.rpc_log = self.tmp / "fake-pi-rpc.jsonl"
        self.previous_home = os.environ.get("OPENSWARM_HOME")
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.home), FAKE_PI_RPC_LOG=str(self.rpc_log))
        self.env.pop("OPENSWARM_PI_BROKER_SOCKET", None)
        self.git("init", "-q")
        self.git("config", "user.name", "Broker Test")
        self.git("config", "user.email", "broker-test@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        (self.home / "lanes.json").write_text(json.dumps({"lanes": [{
            "name": "P1", "backend": "pi", "model": "fake-model", "provider": "fake-provider",
            "repo": str(self.repo), "binary": str(FIXTURES / "fake_pi_rpc.py"),
            "broker_socket": str(self.socket_path)}]}), encoding="utf-8")
        self.write_policy({})
        self.addCleanup(self._restore_home)
        self.addCleanup(self._lane_down)

    def _restore_home(self):
        if self.previous_home is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = self.previous_home

    def _lane_down(self):
        try:
            subprocess.run([sys.executable, str(CLI), "lane", "down"], env=self.env,
                           capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write_policy(self, broker):
        # The lane uses a fake executor, so the isolation of a Pi lane is off; the
        # broker socket of the lane is what the recovery is about.
        (self.home / "policy.local.json").write_text(json.dumps(
            {"pi": {"require_isolation": False, "broker": broker}}), encoding="utf-8")

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

    def cli(self, *args, timeout=60):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], env=self.env, text=True,
                              capture_output=True, timeout=timeout)

    def start_lane(self):
        result = self.cli("lane", "up", "P1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assign(self, task="recover the broker"):
        result = self.cli("lane", "assign", "P1", "--prompt", task)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)["id"]

    def wait_run(self, run_id, timeout=90):
        deadline = time.monotonic() + timeout
        path = self.home / "runs" / run_id / "status.json"
        while time.monotonic() < deadline:
            try:
                state = json.loads(path.read_text(encoding="utf-8")).get("state")
            except (OSError, ValueError):
                state = None
            if state == "finished":
                return json.loads((self.home / "runs" / run_id / "result.json").read_text(encoding="utf-8"))
            time.sleep(0.1)
        self.fail("the run %s did not finish" % run_id)

    def prompts(self):
        """The prompts the fake executor received."""
        if not self.rpc_log.exists():
            return []
        rows = [json.loads(line) for line in self.rpc_log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [row.get("message") for row in rows if row.get("type") == "prompt"]

    def test_the_dispatch_continues_after_the_broker_was_restarted(self):
        # The broker socket of the lane is dead: the dispatch has to restart it and
        # really run the lane, not only report the broker.
        self.write_policy({"restart_cmd": self.start_broker_script(200), "attempts": 2,
                           "socket_wait_s": 5, "restart_grace_s": 5})
        self.start_lane()
        run_id = self.assign()
        result = self.wait_run(run_id)
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(result["error_class"], None, result)
        self.assertTrue(self.prompts(), "the lane really got the prompt after the recovery")

    def test_a_failed_validation_keeps_the_dispatch_blocked(self):
        # The broker answers the connection but not the minimal session: the channel
        # stays interrupted and the dispatch is blocked instead of failing the run
        # with a vague error.
        self.write_policy({"restart_cmd": self.start_broker_script(500), "attempts": 2,
                           "socket_wait_s": 5, "restart_grace_s": 5, "backoff_base_s": 0.01})
        self.start_lane()
        run_id = self.assign()
        result = self.wait_run(run_id)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["error_class"], "broker_unavailable")
        self.assertEqual(self.prompts(), [], "no prompt reached the executor")

    def test_a_dead_broker_without_a_restart_keeps_the_dispatch_blocked(self):
        # No restart command at all: the host stays interrupted and every dispatch of
        # the lane is blocked with the error class of the broker.
        self.write_policy({"restart_cmd": "false", "attempts": 1, "socket_wait_s": 0.2,
                           "restart_grace_s": 0.5, "backoff_base_s": 0.01})
        self.start_lane()
        run_id = self.assign()
        result = self.wait_run(run_id)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["error_class"], "broker_unavailable")
        self.assertEqual(self.prompts(), [], "no prompt reached the executor")


class BrokerRestartScriptTests(unittest.TestCase):
    """bin/pi-broker-restart: the restart command of the deployment (Refs #1714)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="swrm-broker-restart-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.socket_dir = broker_socket_dir(prefix="swrm-restart-")
        self.addCleanup(shutil.rmtree, self.socket_dir, ignore_errors=True)
        self.socket_path = self.socket_dir / "broker.sock"
        self.node = self.tmp / "node"
        self.node.write_text(FAKE_NODE_ARGV, encoding="utf-8")
        self.node.chmod(0o755)
        self.auth = self.tmp / "auth-storage.js"
        self.auth.write_text("export class AuthStorage {}\n", encoding="utf-8")
        self.argv_path = self.tmp / "node-argv.txt"

    def run_script(self, *args, env=None):
        environment = dict(os.environ, HOME=str(self.home), OPENSWARM_PI_NODE=str(self.node),
                           OPENSWARM_PI_AUTH_MODULE=str(self.auth), FAKE_NODE_ARGV=str(self.argv_path))
        environment.pop("OPENSWARM_PI_BROKER_SOCKET", None)
        environment.pop("XSWARM_PI_BROKER_SOCKET", None)
        if env:
            environment.update(env)
        return subprocess.run([sys.executable, str(ROOT / "bin" / "pi-broker-restart"), *args],
                              env=environment, capture_output=True, text=True, timeout=15)

    def test_the_script_execs_the_broker_on_the_given_socket(self):
        result = self.run_script(str(self.socket_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.argv_path.read_text(encoding="utf-8").split("\n"), [
            str(self.node), str(ROOT / "bin" / "pi-minimax-broker.mjs"), str(self.socket_path),
            str(self.auth), "MiniMax-M3.1-Flash"])

    def test_the_script_takes_the_socket_from_the_environment(self):
        result = self.run_script(env={"OPENSWARM_PI_BROKER_SOCKET": str(self.socket_path)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.socket_path), self.argv_path.read_text(encoding="utf-8"))

    def test_the_script_removes_a_stale_socket(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.socket_path))
        stale.listen(1)
        stale.close()
        result = self.run_script(str(self.socket_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.socket_path.exists())

    def test_the_script_refuses_a_path_that_is_not_a_socket(self):
        self.socket_path.write_text("not a socket")
        result = self.run_script(str(self.socket_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid broker socket", result.stderr)
        self.assertFalse(self.argv_path.exists())

    def test_the_script_needs_a_node_runtime(self):
        result = self.run_script(str(self.socket_path), env={"OPENSWARM_PI_NODE": "",
                                                            "PATH": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no node binary", result.stderr)

    def test_the_script_needs_the_credential_store(self):
        result = self.run_script(str(self.socket_path), env={"OPENSWARM_PI_AUTH_MODULE": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("credential store unavailable", result.stderr)


class SandboxRecoveryTests(unittest.TestCase):
    def test_concurrent_dispatches_wait_for_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = os.path.join(directory, "state.json")
            entered, release = threading.Event(), threading.Event()
            results = []
            def validate():
                entered.set()
                release.wait(5)
                return False, "remote down"
            options = dict(socket_path="/a", host="host", state_path=state, attempts=1,
                           probe=lambda: True, validate=validate)
            recovery.write_state(state, {"hosts": {"host": {"socket": "/a", "interrupted": True}}})
            threads = [threading.Thread(target=lambda: results.append(recovery.BrokerRecovery(**options).ensure_ready()))
                       for _ in range(2)]
            threads[0].start()
            self.assertTrue(entered.wait(5))
            threads[1].start()
            self.assertEqual(results, [])
            release.set()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(len(results), 2)
            self.assertTrue(all(not result["ok"] for result in results))

    def test_lane_child_recovers_before_launch_and_passes_socket(self):
        calls = []
        lane = {"name": "P1", "backend": "pi", "model": "model", "binary": "/bin/pi", "broker_socket": "/broker.sock"}
        policy = {"pi": {"require_isolation": False}}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(xs, "lane_root", return_value=directory), \
             mock.patch.object(xs, "configure_executor_environment", return_value=(directory, {})), \
             mock.patch.object(xs, "pi_broker_ensure_ready", side_effect=lambda *a: calls.append("recover") or {"ok": True}), \
             mock.patch.object(xs.subprocess, "Popen", side_effect=lambda *a, **kw: calls.append(kw["env"]["OPENSWARM_PI_BROKER_SOCKET"]) or mock.MagicMock()), \
             mock.patch.object(xs.threading.Thread, "start"), \
             mock.patch.object(xs.procboundary.CgroupBoundary, "create", return_value=None):
            xs.LaneChild(lane, directory, policy)
        self.assertEqual(calls, ["recover", "/broker.sock"])

    def test_lane_child_does_not_launch_when_recovery_fails(self):
        lane = {"name": "P1", "backend": "pi", "model": "model", "binary": "/bin/pi", "broker_socket": "/missing"}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(xs, "lane_root", return_value=directory), \
             mock.patch.object(xs, "configure_executor_environment", return_value=(directory, {})), \
             mock.patch.object(xs, "pi_broker_ensure_ready", return_value={"ok": False}), \
             mock.patch.object(xs.subprocess, "Popen") as launch:
            with self.assertRaises(RuntimeError):
                xs.LaneChild(lane, directory, {"pi": {"require_isolation": False}})
            launch.assert_not_called()

    def test_proxy_session_failure_is_unhealthy(self):
        lane = {"name": "P1", "backend": "pi", "model": "model", "broker_socket": "/proxy"}
        with mock.patch.object(xs, "pi_broker_record", return_value={}), \
             mock.patch.object(xs.broker_recovery_module(), "minimal_session", return_value=(False, "remote down")) as probe:
            self.assertFalse(xs.pi_broker_channel_status([lane])["ok"])
            probe.assert_called_once()

    def test_marker_exists_before_restart_and_other_socket_failure_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            state = os.path.join(directory, "state.json")
            def restart():
                self.assertTrue(recovery.host_state(recovery.read_state(state), "host", "/a")["interrupted"])
            first = recovery.BrokerRecovery("/a", host="host", state_path=state, attempts=1,
                                            probe=lambda: False, restart=restart, socket_wait_s=0)
            self.assertFalse(first.ensure_ready()["ok"])
            second = recovery.BrokerRecovery("/b", host="host", state_path=state, probe=lambda: True)
            self.assertTrue(second.ensure_ready()["ok"])
            self.assertTrue(recovery.host_state(recovery.read_state(state), "host")["interrupted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
