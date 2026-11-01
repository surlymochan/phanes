"""The Pi broker doctor check uses only local socket and request-log state."""
import json
import os
import shutil
import sys
import time
import uuid
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()


class DoctorPiBrokerTests(unittest.TestCase):
    def setUp(self):
        # Keep the AF_UNIX socket path below the platform's short path limit.
        self.tmp = HERE.parent / (".doctor-pi-" + uuid.uuid4().hex[:8])
        self.tmp.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.socket_path = self.tmp / "broker.sock"
        self.log_path = self.tmp / "requests.jsonl"

    def check(self, path=None):
        lane = {"backend": "pi", "broker_socket": str(self.socket_path)}
        with mock.patch.dict(os.environ, {"PI_BROKER_LOG": str(path or self.log_path)}, clear=False), \
                mock.patch.object(xs.socket, "socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.connect.return_value = None
            return xs.doctor_pi_broker(lane)

    def test_connected_broker_with_missing_log_is_idle(self):
        result = self.check()
        self.assertEqual((result["status"], result["detail"]),
                         ("ok", "broker connected; request log idle"))

    def test_recent_429_ratio_warns_and_reports_first_time(self):
        first = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
        rows = [{"ts": first, "status": 429}] + [
            {"ts": time.time() - n, "status": 429 if n < 5 else 200} for n in range(5)
        ]
        self.log_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
