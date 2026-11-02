"""The Pi broker doctor check uses only local socket and request-log state."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = ROOT / "bin" / "openswarm"
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()


class DoctorPiBrokerTests(unittest.TestCase):
    """The broker check never sends a provider request: it connects and reads the audit log."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="openswarm-doctor-pi-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.socket_path = self.tmp / "broker.sock"
        self.log_path = self.tmp / "requests.jsonl"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def check(self, path=None, lane=None):
        lane = lane if lane is not None else {"backend": "pi", "broker_socket": str(self.socket_path)}
        with mock.patch.dict(os.environ, {"PI_BROKER_LOG": str(path or self.log_path)}, clear=False), \
                mock.patch.object(xs.socket, "socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.connect.return_value = None
            return xs.doctor_pi_broker(lane)

    def write_log(self, rows):
        self.log_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def listen(self, path=None):
        """A real AF_UNIX listener: the doctor connects to it without any provider request."""
        path = path or self.socket_path
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        self.addCleanup(server.close)
        return server

    def test_connected_broker_with_missing_log_is_idle(self):
        result = self.check()
        self.assertEqual((result["status"], result["detail"]),
                         ("ok", "broker connected; request log idle"))

    def test_connected_broker_with_empty_log_is_idle(self):
        self.log_path.write_text("", encoding="utf-8")
        result = self.check()
        self.assertEqual((result["status"], result["detail"]),
                         ("ok", "broker connected; request log idle"))

    def test_requests_outside_the_ten_minute_window_are_ignored(self):
        old = time.time() - 3600
        self.write_log([{"ts": old, "status": 429} for _ in range(6)])
        result = self.check()
        self.assertEqual((result["status"], result["detail"]),
                         ("ok", "broker connected; request log idle"))

    def test_healthy_requests_are_ok(self):
        self.write_log([{"ts": time.time() - n, "status": 429 if n == 0 else 200} for n in range(6)])
        result = self.check()
        self.assertEqual(result["status"], "ok")
        self.assertIn("6 recent request(s), 1 returned 429", result["detail"])

    def test_recent_429_ratio_warns_and_reports_first_time(self):
        first = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
        rows = [{"ts": first, "status": 429}] + [
            {"ts": time.time() - n, "status": 429 if n < 5 else 200} for n in range(5)
        ]
        self.write_log(rows)
        result = self.check()
        self.assertEqual(result["status"], "warn")
        self.assertIn("provider quota or rate limit", result["detail"])
        self.assertIn(first, result["detail"])

    def test_unwritable_log_location_warns(self):
        result = self.check(self.tmp / "missing" / "requests.jsonl")
        self.assertEqual(result["status"], "warn")
        self.assertIn("cannot be written", result["detail"])

    def test_unreachable_socket_fails(self):
        lane = {"backend": "pi", "broker_socket": str(self.tmp / "absent.sock")}
        with mock.patch.dict(os.environ, {"PI_BROKER_LOG": str(self.log_path)}, clear=False), \
                mock.patch.object(xs.socket, "socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.connect.side_effect = OSError("missing")
            result = xs.doctor_pi_broker(lane)
        self.assertEqual(result["status"], "fail")
        self.assertIn("socket is not reachable", result["detail"])

    def test_env_socket_path_is_used_without_a_lane_field(self):
        """The documented OPENSWARM_PI_BROKER_SOCKET must reach the socket connect call."""
        lane = {"backend": "pi"}
        with mock.patch.dict(os.environ, {"OPENSWARM_PI_BROKER_SOCKET": str(self.socket_path),
                                          "PI_BROKER_LOG": str(self.log_path)}, clear=False), \
                mock.patch.object(xs.socket, "socket") as socket_factory:
            os.environ.pop("XSWARM_PI_BROKER_SOCKET", None)
            socket_factory.return_value.__enter__.return_value.connect.return_value = None
            result = xs.doctor_pi_broker(lane)
        connect = socket_factory.return_value.__enter__.return_value.connect
        connect.assert_called_once_with(str(self.socket_path))
        self.assertEqual(result["status"], "ok")

    def test_the_lane_socket_wins_over_the_environment(self):
        lane_socket = self.tmp / "lane.sock"
        with mock.patch.dict(os.environ, {"OPENSWARM_PI_BROKER_SOCKET": str(self.tmp / "env.sock"),
                                          "PI_BROKER_LOG": str(self.log_path)}, clear=False), \
                mock.patch.object(xs.socket, "socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.connect.return_value = None
            xs.doctor_pi_broker({"backend": "pi", "broker_socket": str(lane_socket)})
        connect = socket_factory.return_value.__enter__.return_value.connect
        connect.assert_called_once_with(str(lane_socket))

    def test_a_real_broker_socket_reports_the_provider_quota(self):
        """The documented variable reaches a real broker socket, and its 429 rows warn."""
        self.listen()
        first = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
        self.write_log([{"ts": first, "status": 429}] +
                       [{"ts": time.time() - n, "status": 429} for n in range(5)])
        with mock.patch.dict(os.environ, {"OPENSWARM_PI_BROKER_SOCKET": str(self.socket_path),
                                          "PI_BROKER_LOG": str(self.log_path)}, clear=False):
            result = xs.doctor_pi_broker({"backend": "pi"})
        self.assertEqual(result["status"], "warn")
        self.assertIn("provider quota or rate limit", result["detail"])
        self.assertIn(first, result["detail"])

    def test_the_json_report_carries_the_broker_check_for_pi_lanes_only(self):
        """The doctor CLI reports the broker check with the stable doctor_check fields."""
        self.listen()
        self.write_log([{"ts": time.time() - n, "status": 200} for n in range(6)])
        lanes_file = self.tmp / "lanes.json"
        lanes_file.write_text(json.dumps({"lanes": [
            {"name": "P1", "backend": "pi", "repo": str(self.repo),
             "binary": str(ROOT / "bin" / "pi-isolated"), "broker_socket": str(self.socket_path)},
            {"name": "C1", "backend": "codex", "model": "gpt-6-luna", "effort": "high",
             "repo": str(self.repo)},
        ]}), encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp),
               "OPENSWARM_HOME": str(self.tmp), "OPENSWARM_LANES_FILE": str(lanes_file),
               "PI_BROKER_LOG": str(self.log_path)}

        result = subprocess.run([sys.executable, str(CLI), "doctor", "--json"],
                                env=env, text=True, capture_output=True, timeout=60)
        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        report = json.loads(result.stdout)
        checks = {row["name"]: {check["name"]: check for check in row["checks"]}
                  for row in report["lanes"]}
        broker = checks["P1"]["pi_broker"]
        self.assertEqual(set(broker), {"name", "status", "detail", "fix"})
        self.assertEqual(broker["status"], "ok")
        self.assertIn("broker connected", broker["detail"])
        self.assertNotIn("pi_broker", checks["C1"])


if __name__ == "__main__":
    unittest.main()
