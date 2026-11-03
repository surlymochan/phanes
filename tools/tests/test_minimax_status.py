"""The MiniMax burn reading of the box lane status (Refs #1671).

The chain under test: the burn watchdog (``tools/mm-burn-rate.py``) measures
the real MiniMax token burn from the provider request log of the broker and
writes the snapshot; the lane status of the dispatcher shows the hourly
reading while the reading is fresh.
"""
import importlib.util
import json
import os
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "lane-autodispatch" / "autodispatch.py"
spec = importlib.util.spec_from_file_location("autodispatch_minimax_status", SCRIPT)
ad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ad)

TOOL = Path(__file__).resolve().parents[1] / "mm-burn-rate.py"


def snapshot(tokens=420, age_s=30, alert=True, sample=None):
    """A burn snapshot of the MiniMax watchdog."""
    stamp = sample or (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return {"status": "ok", "hourly_tokens": tokens, "token_alert": alert,
            "token_observed_at": stamp, "token_sample_at": stamp, "newest_ts": stamp}


def interval_snapshot(tokens=420, interval=9000, age_s=30):
    """A burn snapshot with the reading of the current Token Plan interval."""
    body = snapshot(tokens=tokens, age_s=age_s)
    body.update({"interval_tokens": interval, "interval_requests": 7,
                 "interval_reset_at": "2026-11-03T18:00:00+00:00",
                 "interval_start": "2026-11-03T13:00:00+00:00"})
    return body


class MiniMaxStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "minimax-burn.json"
        self.previous = os.environ.get("MM_BURN_STATUS")
        os.environ["MM_BURN_STATUS"] = str(self.path)
        self.addCleanup(self._restore)

    def _restore(self):
        if self.previous is None:
            os.environ.pop("MM_BURN_STATUS", None)
        else:
            os.environ["MM_BURN_STATUS"] = self.previous

    def write(self, body):
        self.path.write_text(json.dumps(body), encoding="utf-8")

    def test_status_exposes_the_watchdog_hourly_tokens_and_alert(self):
        body = snapshot()
        self.write(body)
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertEqual(status["_minimax_hourly_tokens"], 420)
        self.assertTrue(status["_minimax_token_alert"])
        # The stamp of the token sample, not the one of the quota history.
        self.assertEqual(status["_minimax_usage_at"], body["token_sample_at"])
        self.assertLessEqual(status["_minimax_token_age_s"], 60)

    def test_an_expired_reading_is_not_shown(self):
        # The watchdog stopped: the last known burn is not the current one.
        self.write(snapshot(age_s=7200))
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertNotIn("_minimax_hourly_tokens", status)

    def test_the_interval_consumption_is_shown_with_the_hour(self):
        # The MiniMax console shows the consumption of the Token Plan
        # interval too: the status carries it next to the hour (Refs #1671).
        self.write(interval_snapshot())
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertEqual(status["_minimax_hourly_tokens"], 420)
        self.assertEqual(status["_minimax_interval_tokens"], 9000)
        self.assertEqual(status["_minimax_interval_requests"], 7)
        self.assertEqual(status["_minimax_interval_reset_at"], "2026-11-03T18:00:00+00:00")

    def test_an_incomplete_interval_is_not_shown(self):
        # The watchdog leaves the interval empty when the log lost rows of
        # it: an incomplete figure is not shown as the consumption.
        body = interval_snapshot()
        body["interval_tokens"] = None
        body["interval_missing_rows"] = 3
        self.write(body)
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertNotIn("_minimax_interval_tokens", status)
        self.assertEqual(status["_minimax_hourly_tokens"], 420)

    def test_a_zero_burn_is_shown_with_its_alert(self):
        # A sustained zero burn is a reading of this hour: it is the collapse
        # the operator has to see (Refs #1671).
        self.write(snapshot(tokens=0, alert=True, sample=None))
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertEqual(status["_minimax_hourly_tokens"], 0)
        self.assertTrue(status["_minimax_token_alert"])

    def test_a_snapshot_without_a_token_reading_is_not_shown(self):
        self.write({"status": "ok", "hourly_tokens": None, "newest_ts": "2026-11-02T06:30:00+08:00"})
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertNotIn("_minimax_hourly_tokens", status)

    def test_a_collection_failure_is_shown_without_a_token_count(self):
        # The collector is gone: there is no reading, but the status says so
        # instead of showing a zero burn (Refs #1671).
        stamp = datetime.now(timezone.utc).isoformat()
        self.write({"status": "ok", "hourly_tokens": None, "token_alert": False,
                    "token_state": "collector_down", "collector_state": "stopped",
                    "token_observed_at": stamp})
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertNotIn("_minimax_hourly_tokens", status)
        self.assertEqual(status["_minimax_token_state"], "collector_down")
        self.assertEqual(status["_minimax_collector_state"], "stopped")

    def test_an_unverified_collector_is_shown_without_a_token_count(self):
        # The collector cannot be verified: the burn of the hour is unknown,
        # so the status shows the state and not a zero burn (Refs #1671).
        stamp = datetime.now(timezone.utc).isoformat()
        self.write({"status": "ok", "hourly_tokens": None, "token_alert": False,
                    "token_state": "unverified", "collector_state": "unknown",
                    "token_observed_at": stamp})
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertNotIn("_minimax_hourly_tokens", status)
        self.assertEqual(status["_minimax_token_state"], "unverified")
        self.assertEqual(status["_minimax_collector_state"], "unknown")

    def test_missing_watchdog_snapshot_does_not_break_status(self):
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertNotIn("_minimax_hourly_tokens", status)

    def test_a_malformed_watchdog_snapshot_does_not_break_status(self):
        self.path.write_text("not json", encoding="utf-8")
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertNotIn("_minimax_hourly_tokens", status)

    def test_the_burn_watchdog_command_is_configurable(self):
        previous = os.environ.get("MM_BURN_CMD")
        os.environ["MM_BURN_CMD"] = "python3 /opt/mm-burn-rate.py --history /var/h.jsonl"
        try:
            command = ad.minimax_burn_command()
        finally:
            if previous is None:
                os.environ.pop("MM_BURN_CMD", None)
            else:
                os.environ["MM_BURN_CMD"] = previous
        self.assertEqual(command, ["python3", "/opt/mm-burn-rate.py", "--history", "/var/h.jsonl"])

    def test_the_default_burn_watchdog_command_runs_the_watchdog_of_the_repository(self):
        # The dispatcher lives in tools/lane-autodispatch/, the watchdog in
        # tools/: the default command must find it there (Refs #1671).
        previous = os.environ.get("MM_BURN_TOOL")
        os.environ.pop("MM_BURN_TOOL", None)
        try:
            command = ad.minimax_burn_command()
        finally:
            if previous is not None:
                os.environ["MM_BURN_TOOL"] = previous
        self.assertIn(str(TOOL), command)
        self.assertIn("--status-file", command)
        self.assertIn(str(self.path), command)
        self.assertIn("--history", command)

    def test_the_default_command_needs_no_broker_configuration(self):
        # The watchdog finds the log of the running broker itself.
        previous = os.environ.get("MM_BROKER_LOG")
        os.environ.pop("MM_BROKER_LOG", None)
        try:
            command = ad.minimax_burn_command()
        finally:
            if previous is not None:
                os.environ["MM_BROKER_LOG"] = previous
        self.assertNotIn("--broker-log", command)

    def test_without_a_watchdog_there_is_nothing_to_refresh(self):
        previous = os.environ.get("MM_BURN_TOOL")
        os.environ["MM_BURN_TOOL"] = "/nonexistent/mm-burn-rate.py"
        try:
            self.assertEqual(ad.minimax_burn_command(), [])
        finally:
            if previous is None:
                os.environ.pop("MM_BURN_TOOL", None)
            else:
                os.environ["MM_BURN_TOOL"] = previous


class MiniMaxStatusChainTests(unittest.TestCase):
    """The watchdog of the repository writes the snapshot the status shows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.previous_dir = ad.D
        ad.D = str(self.dir)
        self.addCleanup(self._restore_dir)
        self.previous_socket = os.environ.get("PI_BROKER_SOCKET")
        os.environ["PI_BROKER_SOCKET"] = str(self.dir / "broker.sock")
        (self.dir / "broker.sock").write_text("", encoding="utf-8")
        self.addCleanup(self._restore_socket)

    def _restore_dir(self):
        ad.D = self.previous_dir

    def _restore_socket(self):
        if self.previous_socket is None:
            os.environ.pop("PI_BROKER_SOCKET", None)
        else:
            os.environ["PI_BROKER_SOCKET"] = self.previous_socket

    def _request_log(self):
        now = datetime.now(timezone.utc)
        rows = [{"ts": (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                 "id": 1, "lane": "M1", "status": 200, "err": "", "ms": 1200,
                 "in_tok": 12, "out_tok": 34},
                {"ts": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                 "id": 2, "lane": "M2", "status": 200, "err": "", "ms": 900,
                 "in_tok": 100, "out_tok": 20}]
        path = self.dir / "broker.sock.requests.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_the_default_watchdog_writes_the_snapshot_the_status_shows(self):
        self._request_log()
        ad.refresh_minimax_burn()
        snapshot_path = self.dir / "minimax-burn.json"
        self.assertTrue(snapshot_path.exists())
        status = ad.status_snapshot([], {}, {}, 0, 0)
        # 46 + 120 tokens in the hour: a collapsed burn, so the watchdog alerts.
        self.assertEqual(status["_minimax_hourly_tokens"], 166)
        self.assertTrue(status["_minimax_token_alert"])
        self.assertEqual(status["_minimax_token_state"], "ok")
        # The consumption of the current Token Plan interval is shown with it.
        self.assertEqual(status["_minimax_interval_tokens"], 166)

    def test_a_stopped_watchdog_leaves_the_status_without_a_burn(self):
        self._request_log()
        ad.refresh_minimax_burn()
        # The watchdog stops: the snapshot of the last run is not a current
        # reading any more.
        stamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        body = json.loads((self.dir / "minimax-burn.json").read_text(encoding="utf-8"))
        body["token_observed_at"] = stamp
        (self.dir / "minimax-burn.json").write_text(json.dumps(body), encoding="utf-8")
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertNotIn("_minimax_hourly_tokens", status)

    def test_a_stopped_collector_is_not_a_zero_burn_in_the_status(self):
        # The broker stopped: its log is still readable, but the burn of the
        # hour is unknown.  The status says so instead of showing a zero.
        self._request_log()
        (self.dir / "broker.sock").unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.dir / "broker.sock"))
        server.close()  # the socket file is there, but nobody listens any more
        ad.refresh_minimax_burn()
        status = ad.status_snapshot([], {}, {}, 0, 0)
        self.assertNotIn("_minimax_hourly_tokens", status)
        self.assertEqual(status["_minimax_token_state"], "collector_down")
        self.assertEqual(status["_minimax_collector_state"], "stopped")


if __name__ == "__main__":
    unittest.main()
