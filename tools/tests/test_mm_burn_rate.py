import contextlib
import importlib.util
import io
import json
import os
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "mm-burn-rate.py"
spec = importlib.util.spec_from_file_location("mm_burn_rate", SCRIPT)
mmbr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mmbr)

END = "2026-11-04T01:05:00+08:00"
NOW = mmbr.parse_ts("2026-11-02T06:40:00+08:00")   # 2026-11-01T22:40:00Z

# The real row shape of the MiniMax provider request log
# (openswarm/bin/pi-minimax-broker.mjs): the token counters MiniMax reported
# for every request.  The broker numbers the requests of a run ("id"), so the
# ids of one log are contiguous.
BROKER_ROWS = [
    {"ts": "2026-11-01T17:45:00Z", "id": 1, "lane": "M0", "req_bytes": 1200,
     "resp_bytes": 900, "status": 200, "err": "", "ms": 1100, "in_tok": 4000, "out_tok": 1000},
    {"ts": "2026-11-01T22:10:00Z", "id": 2, "lane": "M1", "req_bytes": 900,
     "resp_bytes": 400, "status": 200, "err": "", "ms": 1200, "in_tok": 12, "out_tok": 34},
    {"ts": "2026-11-01T22:30:00Z", "id": 3, "lane": "M2", "req_bytes": 800,
     "resp_bytes": 200, "status": 429, "err": "quota exceeded", "ms": 900,
     "in_tok": 0, "out_tok": 0},
]


def row(ts, pct, end=END):
    return {"ts": ts, "minimax": {"weekly_pct": pct, "weekly_end": end}}


def point(ts, pct, end=END):
    return {"ts": mmbr.parse_ts(ts), "weekly_pct": pct, "weekly_end": mmbr.parse_ts(end)}


def broker_log(tmp, rows=None, name="requests.jsonl"):
    """Write a MiniMax request log and return its path."""
    path = Path(tmp) / name
    path.write_text("".join(json.dumps(row) + "\n" for row in (rows or BROKER_ROWS)),
                    encoding="utf-8")
    return path


def live_row(minutes_ago, rid=1, in_tok=100, out_tok=20, status=200):
    """A provider request row of the real clock, ``minutes_ago`` old."""
    now = datetime.now(timezone.utc)
    return {"ts": (now - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z"),
            "id": rid, "lane": "M1", "req_bytes": 900, "resp_bytes": 400, "status": status,
            "err": "", "ms": 900, "in_tok": in_tok, "out_tok": out_tok}


def live_broker_rows():
    """Provider request rows of the last hour, relative to the real clock."""
    return [live_row(30, rid=1, in_tok=12, out_tok=34),
            live_row(10, rid=2, in_tok=100, out_tok=20)]


class MiniMaxBurnRateTests(unittest.TestCase):
    def test_two_samples_estimate_burn_and_5h_usage(self):
        points = [point("2026-11-02T00:16:00+08:00", 67), point("2026-11-02T06:28:00+08:00", 52)]
        result = mmbr.analyze(points, now=NOW)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["burn_pct_per_hour"], 15 / (6 + 12 / 60), places=4)
        self.assertAlmostEqual(result["five_hour_pct"], result["burn_pct_per_hour"] * 5, places=3)
        self.assertAlmostEqual(result["window_hours"], 6 + 12 / 60, places=2)
        self.assertEqual(result["newest_ts"], "2026-11-02T06:28:00+08:00")
        self.assertEqual(result["sample_age_s"], 720)
        self.assertTrue(result["alert"])
        self.assertTrue(result["quota_alert"])
        self.assertIsNotNone(result["exhaustion_at"])
        self.assertNotIn(".", result["exhaustion_at"])

    def test_week_reset_is_insufficient(self):
        points = [
            point("2026-11-02T05:00:00+08:00", 2, end="2026-11-02T06:00:00+08:00"),
            point("2026-11-02T06:00:00+08:00", 99, end="2026-11-09T06:00:00+08:00"),
        ]
        now = mmbr.parse_ts("2026-11-02T06:05:00+08:00")
        self.assertEqual(mmbr.analyze(points, now=now)["status"], "insufficient")

    def test_append_deduplicates_same_minute(self):
        with tempfile.TemporaryDirectory() as tmp:
            history, health = Path(tmp) / "history.jsonl", Path(tmp) / "health.json"
            health.write_text(json.dumps({"minimax": row("ignored", 52)["minimax"]}), encoding="utf-8")
            self.assertTrue(mmbr.append_health(history, health, now=NOW))
            self.assertFalse(mmbr.append_health(history, health, now=NOW + timedelta(seconds=30)))
            self.assertTrue(mmbr.append_health(history, health, now=NOW + timedelta(minutes=1)))
            self.assertEqual(len(history.read_text(encoding="utf-8").splitlines()), 2)

    def test_bad_lines_are_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "history.jsonl"
            valid = row("2026-11-02T05:00:00+08:00", 52)
            string_pct = row("2026-11-02T05:01:00+08:00", "52%")
            bool_pct = row("2026-11-02T05:02:00+08:00", True)
            history.write_text("not json\n[]\n\n" + json.dumps(string_pct) + "\n"
                               + json.dumps(bool_pct) + "\n" + json.dumps(valid) + "\n",
                               encoding="utf-8")
            points, skipped = mmbr.read_history(history)
            self.assertEqual(len(points), 1)
            self.assertEqual(skipped, 4)
            now = mmbr.parse_ts("2026-11-02T05:05:00+08:00")
            self.assertEqual(mmbr.analyze(points, now=now)["status"], "insufficient")

    def test_sparse_history_does_not_fake_six_hour_rate(self):
        points = [point("2026-11-01T10:00:00+08:00", 70), point("2026-11-02T05:00:00+08:00", 52)]
        result = mmbr.analyze(points, now=mmbr.parse_ts("2026-11-02T05:05:00+08:00"))
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["points"], 1)

    def test_stale_sample_never_alerts(self):
        points = [point("2026-10-31T04:00:00+08:00", 60), point("2026-10-31T05:00:00+08:00", 52)]
        result = mmbr.analyze(points, now=NOW)
        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["alert"])
        self.assertIsNone(result["exhaustion_at"])
        self.assertEqual(result["newest_ts"], "2026-10-31T05:00:00+08:00")

    def test_missing_or_empty_history_is_insufficient(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.jsonl"
            empty = Path(tmp) / "empty.jsonl"
            empty.write_text("", encoding="utf-8")
            for path in (missing, empty):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = mmbr.main(["--history", str(path),
                                      "--broker-log", str(Path(tmp) / "none.jsonl")])
                self.assertEqual(code, 0)
                self.assertEqual(out.getvalue().strip().splitlines()[0],
                                 "insufficient (points=0, skipped=0)")

    def test_append_rejects_unreadable_minimax(self):
        with tempfile.TemporaryDirectory() as tmp:
            history, health = Path(tmp) / "history.jsonl", Path(tmp) / "health.json"
            health.write_text(json.dumps({"minimax": {"weekly_pct": "52%", "weekly_end": END}}),
                              encoding="utf-8")
            with self.assertRaises(ValueError):
                mmbr.append_health(history, health, now=NOW)
            self.assertFalse(history.exists())

    def test_health_top_level_not_object_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            history, health = Path(tmp) / "history.jsonl", Path(tmp) / "health.json"
            health.write_text("[]", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = mmbr.main(["--history", str(history), "--append", str(health)])
            self.assertEqual(code, 2)
            self.assertIn("health snapshot was not appended", err.getvalue())
            self.assertFalse(history.exists())


class MiniMaxTokenReadTests(unittest.TestCase):
    """The real MiniMax token reading comes from the provider request log."""

    def test_hourly_tokens_are_summed_from_the_provider_request_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, skipped, files = mmbr.read_broker_log(str(broker_log(tmp)))
        self.assertEqual(skipped, 0)
        self.assertEqual(len(files), 1)
        self.assertEqual(len(rows), 3)
        reading = mmbr.token_reading(rows, now=NOW)
        # Only the requests of the last hour count: 12+34 tokens, the 429 of
        # the exhausted window reported no tokens at all.
        self.assertEqual(reading["hourly_tokens"], 46)
        self.assertEqual(reading["hourly_requests"], 2)
        self.assertEqual(reading["token_sample_at"], "2026-11-01T22:30:00+00:00")
        self.assertEqual(reading["token_sample_age_s"], 600)
        self.assertEqual(reading["token_state"], "ok")

    def test_broken_request_rows_are_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requests.jsonl"
            path.write_text("not json\n[]\n" + json.dumps({"ts": 42, "status": 200}) + "\n"
                            + json.dumps({"ts": "2026-11-01T22:10:00Z", "status": "200"}) + "\n"
                            + json.dumps(BROKER_ROWS[1]) + "\n", encoding="utf-8")
            rows, skipped, files = mmbr.read_broker_log(str(path))
        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped, 4)
        self.assertEqual(len(files), 1)

    def test_missing_request_log_is_a_collection_failure(self):
        rows, skipped, files = mmbr.read_broker_log("/nonexistent/requests.jsonl")
        self.assertEqual((rows, skipped, files), ([], 0, []))
        reading = mmbr.token_reading(rows, now=NOW, collected=bool(files))
        # Nothing is known about the burn: there is no reading and no alert.
        self.assertIsNone(reading["hourly_tokens"])
        self.assertFalse(reading["token_alert"])
        self.assertEqual(reading["token_state"], "unavailable")

    def test_low_token_burn_raises_the_watchdog_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, _, _ = mmbr.read_broker_log(str(broker_log(tmp)))
        reading = mmbr.token_reading(rows, now=NOW, low_tokens=1000)
        self.assertTrue(reading["token_alert"])
        result = mmbr.analyze([point("2026-11-02T00:16:00+08:00", 67),
                                point("2026-11-02T06:28:00+08:00", 52)], now=NOW, tokens=reading)
        # The low burn reaches the alert of the watchdog, not only its own flag.
        self.assertTrue(result["alert"])
        self.assertTrue(result["token_alert"])

    def test_a_healthy_token_burn_raises_no_alert(self):
        rows = [mmbr.broker_row(json.dumps({"ts": "2026-11-01T22:30:00Z", "id": "r1", "lane": "M1",
                                            "status": 200, "err": "", "ms": 1200,
                                            "in_tok": 5000, "out_tok": 5000}))]
        reading = mmbr.token_reading(rows, now=NOW, low_tokens=1000)
        self.assertEqual(reading["hourly_tokens"], 10000)
        self.assertFalse(reading["token_alert"])

    def test_a_sustained_zero_burn_alerts_with_zero_tokens(self):
        # The log is there but holds no request of the last hour: the fleet
        # burned nothing, which is the burn collapse of #1671.  The collector
        # answers, so the quiet hour is a real zero burn.
        rows = [mmbr.broker_row(json.dumps({"ts": "2026-11-01T20:00:00Z", "id": "r1", "lane": "M1",
                                            "status": 200, "err": "", "ms": 1200,
                                            "in_tok": 5000, "out_tok": 5000}))]
        reading = mmbr.token_reading(rows, now=NOW, collector={"collector_state": "alive"})
        self.assertEqual(reading["hourly_tokens"], 0)
        self.assertEqual(reading["hourly_requests"], 0)
        self.assertEqual(reading["token_state"], "idle")
        self.assertTrue(reading["collector_verified"])
        self.assertIsNone(reading["token_sample_at"])
        self.assertTrue(reading["token_alert"])
        # The collector is silent since then: the age tells it apart from an
        # idle fleet.
        self.assertEqual(reading["token_last_request_age_s"], 9600)
        result = mmbr.analyze([], now=NOW, tokens=reading)
        self.assertTrue(result["alert"])

    def test_a_quiet_hour_of_an_unverified_collector_is_not_a_zero_burn(self):
        # The collector cannot be verified (no socket, an explicitly
        # configured log): a fleet that burned nothing and a collection that
        # is broken look the same, so the burn is unknown (Refs #1671).
        rows = [mmbr.broker_row(json.dumps({"ts": "2026-11-01T20:00:00Z", "id": "r1", "lane": "M1",
                                            "status": 200, "err": "", "ms": 1200,
                                            "in_tok": 5000, "out_tok": 5000}))]
        reading = mmbr.token_reading(rows, now=NOW)
        self.assertIsNone(reading["hourly_tokens"])
        self.assertEqual(reading["token_state"], "unverified")
        self.assertFalse(reading["collector_verified"])
        self.assertFalse(reading["token_alert"])
        # The age of the last request says how long the collector is silent.
        self.assertEqual(reading["token_last_request_age_s"], 9600)
        result = mmbr.analyze([], now=NOW, tokens=reading)
        self.assertFalse(result["alert"])

    def test_the_token_alert_does_not_depend_on_the_quota_history(self):
        # A missing, stale or too short history must not silence the watchdog.
        reading = {"hourly_tokens": 0, "token_alert": True, "token_state": "idle"}
        for points in ([], [point("2026-10-31T05:00:00+08:00", 52)],
                       [point("2026-11-02T06:28:00+08:00", 52)]):
            result = mmbr.analyze(points, now=NOW, tokens=reading)
            self.assertTrue(result["alert"])
            self.assertFalse(result["quota_alert"])

    def test_the_rotated_broker_log_is_read(self):
        # The broker rotates its log by renaming it to "<log>.1": the rows of
        # the hour before the rotation must still be counted.
        with tempfile.TemporaryDirectory() as tmp:
            current = broker_log(tmp, [BROKER_ROWS[2]])
            (Path(tmp) / "requests.jsonl.1").write_text(
                json.dumps(BROKER_ROWS[0]) + "\n" + json.dumps(BROKER_ROWS[1]) + "\n",
                encoding="utf-8")
            rows, _, files = mmbr.read_broker_log(str(current))
        self.assertEqual(len(files), 2)
        self.assertEqual(len(rows), 3)
        reading = mmbr.token_reading(rows, now=NOW)
        self.assertEqual(reading["hourly_tokens"], 46)
        self.assertEqual(reading["hourly_requests"], 2)

    def test_the_broker_log_is_found_without_configuration(self):
        # The broker writes "<socket>.requests.jsonl" when PI_BROKER_LOG is
        # not set (openswarm/bin/pi-minimax-broker.mjs).
        env = {"PI_BROKER_SOCKET": "/run/openswarm-pi/broker.sock"}
        self.assertEqual(mmbr.broker_log_candidates(env)[0],
                         "/run/openswarm-pi/broker.sock.requests.jsonl")
        env = {"PI_BROKER_LOG": "/var/log/mm.jsonl"}
        self.assertEqual(mmbr.broker_log_candidates(env), ["/var/log/mm.jsonl"])

    def test_the_broker_log_of_the_running_broker_is_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket = Path(tmp) / "broker.sock"
            socket.write_text("", encoding="utf-8")
            log = Path(tmp) / "broker.sock.requests.jsonl"
            log.write_text(json.dumps(BROKER_ROWS[1]) + "\n", encoding="utf-8")
            env = {"PI_BROKER_SOCKET": str(socket)}
            self.assertEqual(mmbr.default_broker_log(env), str(log))

    def test_the_interval_window_starts_at_the_token_plan_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, _, _ = mmbr.read_broker_log(str(broker_log(tmp)))
            # The coding-plan payload: the Token Plan window ends at 23:00Z.
            quota = Path(tmp) / "quota.json"
            quota.write_text(json.dumps({"base_resp": {"status_code": 0},
                                          "model_remains": [{"model_name": "MiniMax-M3.1-Flash",
                                                             "current_interval_remaining_percent": 80,
                                                             "current_weekly_remaining_percent": 60,
                                                             "end_time": 1793574000000,
                                                             "weekly_end_time": 1794178800000}]}),
                             encoding="utf-8")
            resets_at = mmbr.interval_reset_at(str(quota))
        self.assertEqual(resets_at.isoformat(), "2026-11-01T23:00:00+00:00")
        reading = mmbr.token_reading(rows, now=NOW, resets_at=resets_at)
        # The request of 17:45Z belongs to the previous window: the interval
        # total of the current window does not mix the two intervals.
        self.assertEqual(reading["interval_tokens"], 46)
        self.assertEqual(reading["interval_requests"], 2)
        self.assertEqual(reading["interval_reset_at"], "2026-11-01T23:00:00+00:00")
        self.assertEqual(reading["interval_start"], "2026-11-01T18:00:00+00:00")
        # Without the provider window the interval is anchored on the reading,
        # so the request of the previous window is counted in it.
        anchored = mmbr.token_reading(rows, now=NOW)
        self.assertEqual(anchored["interval_tokens"], 5046)
        self.assertIsNone(anchored["interval_reset_at"])

    def test_hourly_buckets_are_the_console_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, _, _ = mmbr.read_broker_log(str(broker_log(tmp)))
        reading = mmbr.token_reading(rows, now=NOW)
        buckets = {bucket["hour"]: bucket for bucket in reading["hourly_buckets"]}
        # The buckets are the Beijing clock hours of the MiniMax console.
        self.assertEqual(buckets["2026-11-02T06:00:00+08:00"],
                         {"hour": "2026-11-02T06:00:00+08:00", "tokens": 46, "requests": 2})
        self.assertEqual(buckets["2026-11-02T01:00:00+08:00"]["tokens"], 5000)


class MiniMaxBurnSnapshotTests(unittest.TestCase):
    """The snapshot the lane status reads is written by the watchdog itself."""

    def _history(self, tmp):
        """A health history of the last two hours that predicts no exhaustion."""
        now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
        history = Path(tmp) / "history.jsonl"
        rows = [{"ts": (now - timedelta(hours=2)).isoformat(),
                 "minimax": {"weekly_pct": 52, "weekly_end": (now + timedelta(days=2)).isoformat()}},
                {"ts": now.isoformat(),
                 "minimax": {"weekly_pct": 51, "weekly_end": (now + timedelta(days=2)).isoformat()}}]
        history.write_text("".join(json.dumps(entry) + "\n" for entry in rows), encoding="utf-8")
        return history

    def test_the_snapshot_is_written_next_to_the_history_without_an_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = mmbr.main(["--history", str(history), "--json"])
            snapshot = json.loads((Path(tmp) / "minimax-burn.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(snapshot["status"], "ok")
        self.assertIn("written_at", snapshot)
        # The stdout is one JSON object, so a JSON consumer can read it.
        self.assertEqual(json.loads(out.getvalue())["status"], "ok")

    def test_the_snapshot_carries_the_token_reading_of_the_request_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(tmp)
            log_path = broker_log(tmp, live_broker_rows())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = mmbr.main(["--history", str(history), "--broker-log", str(log_path), "--json"])
            snapshot = json.loads((Path(tmp) / "minimax-burn.json").read_text(encoding="utf-8"))
        # The fleet burned 166 tokens in the hour: that is a collapse, so the
        # watchdog alerts and exits non-zero.
        self.assertEqual(code, 1)
        self.assertEqual(snapshot["hourly_tokens"], 166)
        self.assertTrue(snapshot["token_alert"])
        self.assertTrue(snapshot["alert"])
        self.assertIn("token_sample_at", snapshot)
        self.assertIn("token_observed_at", snapshot)

    def test_json_stdout_stays_a_single_object_with_a_token_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(tmp)
            log_path = broker_log(tmp, live_broker_rows())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                mmbr.main(["--history", str(history), "--broker-log", str(log_path), "--json"])
            lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["hourly_tokens"], 166)

    def test_a_missing_request_log_leaves_the_snapshot_without_a_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = mmbr.main(["--history", str(history),
                                  "--broker-log", str(Path(tmp) / "none.jsonl"), "--json"])
            snapshot = json.loads((Path(tmp) / "minimax-burn.json").read_text(encoding="utf-8"))
        # A collection failure is not a burn: the watchdog stays quiet.
        self.assertEqual(code, 0)
        self.assertIsNone(snapshot["hourly_tokens"])
        self.assertFalse(snapshot["token_alert"])
        self.assertEqual(snapshot["token_state"], "unavailable")

    def test_a_sustained_zero_burn_makes_the_watchdog_exit_with_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(tmp)
            # The broker of the box answers on its socket and its log holds
            # no request of the last hour: the fleet burned nothing.
            broker_socket = Path(tmp) / "broker.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(broker_socket))
            server.listen(1)
            self.addCleanup(server.close)
            log_path = Path(tmp) / "broker.sock.requests.jsonl"
            log_path.write_text(json.dumps({"ts": "2026-11-01T00:00:00Z", "id": 1, "lane": "M0",
                                            "status": 200, "err": "", "ms": 1100,
                                            "in_tok": 4000, "out_tok": 1000}) + "\n",
                                encoding="utf-8")
            previous = os.environ.get("PI_BROKER_SOCKET")
            os.environ["PI_BROKER_SOCKET"] = str(broker_socket)
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = mmbr.main(["--history", str(history), "--json"])
            finally:
                if previous is None:
                    os.environ.pop("PI_BROKER_SOCKET", None)
                else:
                    os.environ["PI_BROKER_SOCKET"] = previous
            snapshot = json.loads((Path(tmp) / "minimax-burn.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(snapshot["hourly_tokens"], 0)
        self.assertTrue(snapshot["token_alert"])
        self.assertTrue(snapshot["alert"])
        self.assertEqual(snapshot["token_state"], "idle")
        self.assertEqual(snapshot["collector_state"], "alive")

    def test_the_watchdog_reads_the_broker_log_without_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(tmp)
            socket = Path(tmp) / "broker.sock"
            socket.write_text("", encoding="utf-8")
            log = Path(tmp) / "broker.sock.requests.jsonl"
            log.write_text("".join(json.dumps(entry) + "\n" for entry in live_broker_rows()),
                           encoding="utf-8")
            previous = os.environ.get("PI_BROKER_SOCKET")
            os.environ["PI_BROKER_SOCKET"] = str(socket)
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = mmbr.main(["--history", str(history), "--json"])
                snapshot = json.loads((Path(tmp) / "minimax-burn.json").read_text(encoding="utf-8"))
            finally:
                if previous is None:
                    os.environ.pop("PI_BROKER_SOCKET", None)
                else:
                    os.environ["PI_BROKER_SOCKET"] = previous
        # The reading exists without any configured path: the watchdog finds
        # the log of the running broker itself.
        self.assertEqual(code, 1)
        self.assertEqual(snapshot["hourly_tokens"], 166)
        self.assertEqual(snapshot["broker_log"], str(log))


class MiniMaxCollectorTests(unittest.TestCase):
    """The reading needs a collector that is alive and a log that is complete.

    A collector that stopped leaves a readable log behind, and a log that
    lost rows (a rotation that overwrote an older generation, an append that
    failed) counts less than the console shows.  Both are collection
    failures: they must not be reported as a fleet that burned nothing.
    """

    def _broker_socket(self, path, listen=True):
        """The unix socket of the MiniMax broker.

        Without a listener the broker stopped: the socket file is still
        there, but nobody answers on it.
        """
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        if listen:
            server.listen(1)
            self.addCleanup(server.close)
        else:
            server.close()
        return server

    def _watchdog(self, tmp, broker_socket=None, extra=None):
        """Run the watchdog in the box setup; return (code, snapshot)."""
        env = {"PI_BROKER_SOCKET": str(broker_socket)} if broker_socket else {}
        argv = ["--history", str(Path(tmp) / "history.jsonl"), "--json"] + list(extra or [])
        out = io.StringIO()
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(out):
                code = mmbr.main(argv)
        snapshot = json.loads((Path(tmp) / "minimax-burn.json").read_text(encoding="utf-8"))
        return code, snapshot

    def _broker_log(self, tmp, rows, name="broker.sock.requests.jsonl"):
        path = Path(tmp) / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_a_stopped_collector_is_not_reported_as_a_zero_burn(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker_socket = Path(tmp) / "broker.sock"
            self._broker_socket(broker_socket, listen=False)
            # The last request of the collector is three hours old: without
            # the liveness check the hour would read as a zero burn.
            self._broker_log(tmp, [live_row(180, rid=1)])
            code, snapshot = self._watchdog(tmp, broker_socket)
        self.assertEqual(snapshot["collector_state"], "stopped")
        self.assertEqual(snapshot["token_state"], "collector_down")
        self.assertIsNone(snapshot["hourly_tokens"])
        self.assertFalse(snapshot["token_alert"])
        self.assertEqual(code, 0)

    def test_a_live_collector_without_a_request_is_a_genuine_zero_burn(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker_socket = Path(tmp) / "broker.sock"
            self._broker_socket(broker_socket, listen=True)
            self._broker_log(tmp, [live_row(180, rid=1)])
            code, snapshot = self._watchdog(tmp, broker_socket)
        # The collector collects and the fleet did nothing: that is the burn
        # collapse of #1671 and the watchdog alerts on it.
        self.assertEqual(snapshot["collector_state"], "alive")
        self.assertEqual(snapshot["token_state"], "idle")
        self.assertEqual(snapshot["hourly_tokens"], 0)
        self.assertTrue(snapshot["token_alert"])
        self.assertEqual(code, 1)

    def test_a_live_collector_reads_the_tokens_of_the_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker_socket = Path(tmp) / "broker.sock"
            self._broker_socket(broker_socket, listen=True)
            self._broker_log(tmp, [live_row(30, rid=1, in_tok=4000, out_tok=1000),
                                   live_row(10, rid=2, in_tok=12, out_tok=34)])
            code, snapshot = self._watchdog(tmp, broker_socket)
        self.assertEqual(snapshot["token_state"], "ok")
        self.assertEqual(snapshot["hourly_tokens"], 5046)
        self.assertFalse(snapshot["token_alert"])
        self.assertEqual(code, 0)

    def test_a_log_that_lost_rows_is_not_a_reliable_reading(self):
        # The broker failed to append a row: the numbering of the requests
        # has a hole, so the hour is not complete and the sum is not the
        # figure of the console.
        with tempfile.TemporaryDirectory() as tmp:
            broker_socket = Path(tmp) / "broker.sock"
            self._broker_socket(broker_socket, listen=True)
            self._broker_log(tmp, [live_row(50, rid=1), live_row(20, rid=3)])
            code, snapshot = self._watchdog(tmp, broker_socket)
        self.assertEqual(snapshot["token_state"], "incomplete")
        self.assertEqual(snapshot["token_missing_rows"], 1)
        self.assertIsNone(snapshot["hourly_tokens"])
        self.assertFalse(snapshot["token_alert"])
        self.assertEqual(code, 0)

    def test_a_second_rotation_in_the_hour_loses_rows(self):
        # The broker overwrites "<log>.1" on every rotation: the rows of the
        # first generation of the hour (1..3) and of the second one (6..7)
        # are gone.
        with tempfile.TemporaryDirectory() as tmp:
            broker_socket = Path(tmp) / "broker.sock"
            self._broker_socket(broker_socket, listen=True)
            log = self._broker_log(tmp, [live_row(10, rid=8), live_row(5, rid=9)])
            (Path(str(log) + ".1")).write_text(
                "".join(json.dumps(row) + "\n" for row in (live_row(50, rid=4),
                                                            live_row(45, rid=5))),
                encoding="utf-8")
            code, snapshot = self._watchdog(tmp, broker_socket)
        # The rows of the hour are not complete: the reading is not the figure
        # of the console.
        self.assertEqual(snapshot["token_state"], "incomplete")
        self.assertEqual(snapshot["token_missing_rows"], 5)
        self.assertIsNone(snapshot["hourly_tokens"])
        self.assertFalse(snapshot["token_alert"])
        self.assertEqual(code, 0)

    def test_one_rotation_keeps_the_hour_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker_socket = Path(tmp) / "broker.sock"
            self._broker_socket(broker_socket, listen=True)
            log = self._broker_log(tmp, [live_row(10, rid=2), live_row(5, rid=3)])
            (Path(str(log) + ".1")).write_text(json.dumps(live_row(50, rid=1)) + "\n",
                                                encoding="utf-8")
            code, snapshot = self._watchdog(tmp, broker_socket)
        # The rows of the hour are complete: the reading is the figure of the
        # console and can be reconciled with it.
        self.assertEqual(snapshot["token_state"], "ok")
        self.assertEqual(snapshot["token_missing_rows"], 0)
        self.assertEqual(snapshot["hourly_tokens"], 360)
        self.assertEqual(snapshot["hourly_requests"], 3)

    def test_every_rotated_generation_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker_socket = Path(tmp) / "broker.sock"
            self._broker_socket(broker_socket, listen=True)
            log = self._broker_log(tmp, [live_row(10, rid=3)])
            (Path(str(log) + ".1")).write_text(json.dumps(live_row(40, rid=2)) + "\n",
                                                encoding="utf-8")
            (Path(str(log) + ".2")).write_text(json.dumps(live_row(50, rid=1)) + "\n",
                                                encoding="utf-8")
            code, snapshot = self._watchdog(tmp, broker_socket)
        # All three generations are read: the hour is complete.
        self.assertEqual(snapshot["token_state"], "ok")
        self.assertEqual(snapshot["hourly_tokens"], 360)
        self.assertEqual(snapshot["hourly_requests"], 3)

    def test_a_collector_that_cannot_write_its_log_is_a_collection_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = broker_log(tmp, [live_row(10, rid=1)])
            with mock.patch("os.access", return_value=False):
                code, snapshot = self._watchdog(tmp, extra=["--broker-log", str(log)])
        # The collector cannot write the log: nothing is collected, so nothing
        # is known about the burn.
        self.assertEqual(snapshot["collector_state"], "unwritable")
        self.assertEqual(snapshot["token_state"], "collector_down")
        self.assertIsNone(snapshot["hourly_tokens"])
        self.assertFalse(snapshot["token_alert"])
        self.assertEqual(code, 0)

    def test_a_write_failure_is_a_failure_even_when_the_collector_answers(self):
        # The broker answers on its socket but cannot append to its log: the
        # readable log of an hour ago would look like a zero burn, so the
        # write failure has to be seen before the liveness (Refs #1671).
        with tempfile.TemporaryDirectory() as tmp:
            broker_socket = Path(tmp) / "broker.sock"
            self._broker_socket(broker_socket, listen=True)
            self._broker_log(tmp, [live_row(180, rid=1)])
            with mock.patch("os.access", return_value=False):
                code, snapshot = self._watchdog(tmp, broker_socket)
        self.assertEqual(snapshot["collector_state"], "unwritable")
        self.assertEqual(snapshot["token_state"], "collector_down")
        self.assertIsNone(snapshot["hourly_tokens"])
        self.assertFalse(snapshot["token_alert"])
        self.assertEqual(code, 0)

    def test_an_unverified_collector_with_a_quiet_log_is_not_a_zero_burn(self):
        # An explicitly configured log without a known socket: the collector
        # cannot be verified, so a quiet hour is not a zero burn (it may be a
        # collection that is broken).
        with tempfile.TemporaryDirectory() as tmp:
            log = self._broker_log(tmp, [live_row(180, rid=1)], name="requests.jsonl")
            code, snapshot = self._watchdog(tmp, extra=["--broker-log", str(log)])
        self.assertEqual(snapshot["collector_state"], "unknown")
        self.assertEqual(snapshot["token_state"], "unverified")
        self.assertIsNone(snapshot["hourly_tokens"])
        self.assertFalse(snapshot["token_alert"])
        # The age of the last request says how long the collector is silent.
        self.assertEqual(snapshot["token_last_request_age_s"], 180 * 60)
        self.assertEqual(code, 0)

    def test_a_request_of_the_hour_verifies_an_unverified_collector(self):
        # The log carries a request of the hour: the collector demonstrably
        # wrote inside the hour, so the reading is a real one.
        with tempfile.TemporaryDirectory() as tmp:
            log = self._broker_log(tmp, [live_row(10, rid=1, in_tok=4000, out_tok=1000)],
                                   name="requests.jsonl")
            code, snapshot = self._watchdog(tmp, extra=["--broker-log", str(log)])
        self.assertEqual(snapshot["collector_state"], "unknown")
        self.assertEqual(snapshot["token_state"], "ok")
        self.assertTrue(snapshot["collector_verified"])
        self.assertEqual(snapshot["hourly_tokens"], 5000)
        self.assertFalse(snapshot["token_alert"])
        self.assertEqual(code, 0)

    def test_a_missing_health_snapshot_does_not_block_the_token_alert(self):
        # The quota collection is broken (there is no health snapshot at all):
        # the token reading is its own, so the burn is still judged (Refs #1671).
        with tempfile.TemporaryDirectory() as tmp:
            log = broker_log(tmp, [live_row(10, rid=1)])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, snapshot = self._watchdog(tmp, extra=["--broker-log", str(log),
                                                            "--append", str(Path(tmp) / "none.json")])
        self.assertEqual(code, 1)
        self.assertTrue(snapshot["token_alert"])
        self.assertTrue(snapshot["alert"])
        self.assertEqual(snapshot["hourly_tokens"], 120)
        self.assertIn("health snapshot was not appended", snapshot["health_error"])

    def test_a_broken_health_snapshot_does_not_block_the_token_alert(self):
        # The quota collection is broken, but the token reading is its own:
        # the low burn still alerts and the snapshot is still written.
        with tempfile.TemporaryDirectory() as tmp:
            health = Path(tmp) / "health.json"
            health.write_text("[]", encoding="utf-8")
            log = broker_log(tmp, [live_row(10, rid=1)])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, snapshot = self._watchdog(tmp, extra=["--broker-log", str(log),
                                                            "--append", str(health)])
        self.assertEqual(code, 1)
        self.assertTrue(snapshot["token_alert"])
        self.assertTrue(snapshot["alert"])
        self.assertEqual(snapshot["hourly_tokens"], 120)
        self.assertIn("health snapshot was not appended", snapshot["health_error"])
        self.assertIn("health snapshot was not appended", err.getvalue())

    def test_the_reading_matches_the_console_hours_of_the_provider_data(self):
        # The clock hours of the MiniMax console are the hours of the
        # provider data: the buckets of the reading sum up to the tokens the
        # console shows for the same hour.
        now = datetime.now(timezone.utc)
        rows = [live_row(10, rid=1, in_tok=4000, out_tok=1000),
                live_row(5, rid=2, in_tok=12, out_tok=34)]
        reading = mmbr.token_reading([mmbr.broker_row(json.dumps(row)) for row in rows], now=now)
        buckets = reading["hourly_buckets"]
        self.assertEqual(sum(bucket["tokens"] for bucket in buckets), reading["hourly_tokens"])
        self.assertEqual(sum(bucket["requests"] for bucket in buckets), reading["hourly_requests"])
        hour = now.astimezone(mmbr.CONSOLE_TZ).replace(minute=0, second=0, microsecond=0)
        self.assertEqual(buckets[-1]["hour"], hour.isoformat(timespec="seconds"))


if __name__ == "__main__":
    unittest.main()
