#!/usr/bin/env python3
"""Reproduction of #1671: the MiniMax hourly burn reading (real token volume).

The fleet side had no token reading at all: the burn watchdog of the batch
base only knew the quota percentages of ``health.json``, so the 2026-11-02 burn
collapse was only visible in the MiniMax console.  These tests pin the
contract the issue asks for:

* the hourly burn is the *real* MiniMax token consumption of the provider
  request log -- the rows carry the token counters MiniMax reported per
  request -- and not a dispatch count proxy and not the remaining quota
  percentage;
* a log that rotated inside the hour still covers the whole hour;
* a fleet that burned nothing in the hour is a zero reading that alerts,
  while a collector that stopped is a collection failure that must not be
  passed off as a zero burn;
* the reading reaches the status surface (the snapshot) without configuration;
* ``--json`` stays a single JSON object.

Every test of this module fails on the batch base (``1f0346dc6``): the
watchdog of the base has no token collection at all.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "mm-burn-rate.py"


def request_row(ts, in_tok=0, out_tok=0, status=200, lane="M1"):
    """One row of the MiniMax provider request log.

    The shape is the row the broker writes (the same one
    ``tools/broker-log-summary.py`` reads): time, lane, status and the token
    counters MiniMax reported for the request.
    """
    return json.dumps({"ts": ts, "id": 7, "lane": lane, "status": status, "err": "",
                       "ms": 1200, "in_tok": in_tok, "out_tok": out_tok})


def hourly_tokens(payload):
    """The hourly token reading of a payload, whatever the field is named."""
    for key, value in sorted(payload.items()):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        name = key.lower()
        if "hour" in name and "token" in name:
            return value
    return None


def low_burn_alert(payload):
    """True when the payload raises the low burn (token) alert.

    The dedicated token flag is preferred; the generic alert flag is accepted
    as well, because the watchdog merges the token alert into it.
    """
    for key, value in sorted(payload.items()):
        if isinstance(value, bool) and "token" in key.lower() and "alert" in key.lower():
            return value
    for key in ("alert", "burn_alert", "low_burn_alert"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    return None


def token_state(payload):
    """The state the watchdog reports for the token collection."""
    for key, value in sorted(payload.items()):
        if isinstance(value, str) and "state" in key.lower():
            return value
    return None


def genuine_zero(payload):
    """True when the payload reports a real zero burn of the last hour."""
    value = hourly_tokens(payload)
    if value is None or value != 0:
        return False
    return token_state(payload) in (None, "", "ok", "idle")


class MiniMaxHourlyBurnReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.history = self.dir / "history.jsonl"
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.socket_path = self.dir / "broker.sock"
        self._listen()

    # -- helpers ---------------------------------------------------------
    def _listen(self):
        """A broker socket, so a liveness check that looks at it is satisfied."""
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.socket_path))
            server.listen(1)
        except OSError:  # a path that is too long for a unix socket
            self.socket_path = None
            return
        self.addCleanup(server.close)

    def _ago(self, seconds):
        return (self.now - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    def _env(self, **extra):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("OPENSWARM_", "XSWARM_", "MM_", "PI_BROKER_"))}
        env["HOME"] = str(self.dir / "home")
        env["TMPDIR"] = str(self.dir)
        if self.socket_path is not None:
            env["OPENSWARM_PI_BROKER_SOCKET"] = str(self.socket_path)
            env["PI_BROKER_SOCKET"] = str(self.socket_path)
        env.update(extra)
        return env

    def _run(self, args, env=None):
        proc = subprocess.run([sys.executable, str(SCRIPT)] + args, capture_output=True,
                              text=True, env=env or self._env(), cwd=str(self.dir))
        return proc.returncode, proc.stdout, proc.stderr

    def _write_log(self, name="broker.sock.requests.jsonl", rows=(), mtime=None):
        log = self.dir / name
        log.write_text("".join(row + "\n" for row in rows), encoding="utf-8")
        if mtime is not None:
            os.utime(log, (mtime, mtime))
        return log

    def _write_history(self, ts=None, weekly_pct=95.0):
        stamp = ts or self.now.isoformat().replace("+00:00", "Z")
        end = (self.now + timedelta(days=3)).isoformat().replace("+00:00", "Z")
        row = {"ts": stamp, "minimax": {"weekly_pct": weekly_pct, "weekly_end": end}}
        self.history.write_text(json.dumps(row) + "\n", encoding="utf-8")
        return self.history

    def _snapshot_payload(self):
        """The burn snapshot of a run, wherever the watchdog wrote it."""
        state = self.dir / "home" / "phanes-ctl" / "autodispatch"
        for path in list(self.dir.glob("*.json")) + list(state.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and hourly_tokens(payload) is not None:
                return payload
        return None

    # -- the reading -----------------------------------------------------
    def test_hourly_reading_is_the_real_token_volume_of_the_provider_log(self):
        """The hourly burn is the sum of the token counters MiniMax reported."""
        log = self._write_log(rows=[
            request_row(self._ago(0), in_tok=120000, out_tok=34000),
            request_row(self._ago(7200), in_tok=5000000, out_tok=5000000),  # older than the hour
            request_row(self._ago(60), status=429, in_tok=0, out_tok=0),  # a refused request
            "not json at all",  # a broken line must not stop the reading
        ])
        rc, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        payload = json.loads(out)
        self.assertEqual(hourly_tokens(payload), 154000)

    def test_json_output_stays_a_single_json_object(self):
        """``--json`` must not append a text line to the JSON object."""
        log = self._write_log(rows=[request_row(self._ago(0), in_tok=120000, out_tok=34000)])
        rc, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        payload = json.loads(out)  # raises when the output is not one JSON object
        self.assertIsInstance(payload, dict)
        self.assertEqual(hourly_tokens(payload), 154000)

    def test_the_reading_is_based_on_the_token_sample_not_on_the_health_sample(self):
        """The freshness of the reading is the token sample, not the health sample."""
        sample = self._ago(1200)
        log = self._write_log(rows=[request_row(sample, in_tok=120000, out_tok=34000)])
        self._write_history(ts=self.now.isoformat().replace("+00:00", "Z"))
        rc, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        payload = json.loads(out)
        stamps = []
        for value in payload.values():
            if not isinstance(value, str):
                continue
            try:
                stamps.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                continue
        expected = datetime.fromisoformat(sample.replace("Z", "+00:00"))
        self.assertTrue(any(abs((stamp - expected).total_seconds()) <= 5 for stamp in stamps),
                        f"the token sample time {sample} is not reported: {payload}")

    # -- rotation --------------------------------------------------------
    def test_a_rotated_broker_log_still_covers_the_whole_hour(self):
        """A rotation inside the hour must not drop the earlier half of it."""
        rotated = self._write_log(name="broker.sock.requests.jsonl.1",
                                  rows=[request_row(self._ago(1500), in_tok=800000, out_tok=200000)])
        current = self._write_log(rows=[request_row(self._ago(0), in_tok=900000, out_tok=100000)])
        self.assertTrue(rotated.exists())
        rc, out, err = self._run(["--history", str(self.history), "--broker-log", str(current), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        payload = json.loads(out)
        self.assertEqual(hourly_tokens(payload), 2000000)

    # -- zero burn vs. broken collector ----------------------------------
    def test_a_live_collector_with_a_quiet_hour_is_a_zero_burn_that_alerts(self):
        """The broker heartbeat keeps the collector verifiable.

        The broker writes a heartbeat next to its request log, so a fleet that
        burned nothing in the hour is a zero burn that alerts -- and not a
        collection failure (Refs #1671).
        """
        log = self._write_log(rows=[request_row(self._ago(7200), in_tok=10, out_tok=10)])
        stale = (self.now - timedelta(hours=2)).timestamp()
        os.utime(log, (stale, stale))  # the log itself was not written for two hours
        Path(str(log) + ".heartbeat").write_text(self.now.isoformat() + "\n", encoding="utf-8")
        self._write_history()
        rc, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        payload = json.loads(out)
        self.assertEqual(hourly_tokens(payload), 0)
        self.assertEqual(token_state(payload), "idle")
        self.assertTrue(low_burn_alert(payload), f"a zero burn must alert: {payload}")

    def test_a_fleet_that_burned_nothing_in_the_hour_is_a_zero_reading_that_alerts(self):
        """A sustained zero burn is the collapse of #1671: it must alert."""
        log = self._write_log(rows=[request_row(self._ago(7200), in_tok=10, out_tok=10)])
        os.utime(log, (self.now.timestamp(), self.now.timestamp()))  # the collector is alive
        self._write_history()
        rc, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        payload = json.loads(out)
        self.assertEqual(hourly_tokens(payload), 0)
        self.assertTrue(low_burn_alert(payload), f"a zero burn must alert: {payload}")

    def test_a_broken_collector_is_not_reported_as_a_zero_burn(self):
        """A stopped collector is a collection failure, not a fleet that did nothing."""
        live = self._write_log(name="live.requests.jsonl",
                               rows=[request_row(self._ago(0), in_tok=120000, out_tok=34000)])
        os.utime(live, (self.now.timestamp(), self.now.timestamp()))
        self._write_history()
        rc, out, err = self._run(["--history", str(self.history), "--broker-log", str(live), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        self.assertGreater(hourly_tokens(json.loads(out)) or 0, 0,
                           "a live collector must yield a token reading")

        # The collector stopped hours ago: the log is readable but stale, and the
        # quota health snapshot of the box is stale as well.
        stopped = self._write_log(name="stopped.requests.jsonl",
                                  rows=[request_row(self._ago(7200), in_tok=10, out_tok=10)])
        stale = (self.now - timedelta(hours=2)).timestamp()
        os.utime(stopped, (stale, stale))
        self._write_history(ts=self._ago(7200))
        rc, out, err = self._run(["--history", str(self.history), "--broker-log", str(stopped), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        payload = json.loads(out)
        self.assertFalse(low_burn_alert(payload), f"a stopped collector must not alert: {payload}")
        self.assertFalse(genuine_zero(payload), f"a stopped collector is not a zero burn: {payload}")

        # No log at all: nothing is known about the burn.
        rc, out, err = self._run(["--history", str(self.history), "--broker-log",
                                  str(self.dir / "missing.requests.jsonl"), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        payload = json.loads(out)
        self.assertFalse(low_burn_alert(payload), f"a missing log must not alert: {payload}")
        self.assertFalse(genuine_zero(payload), f"a missing log is not a zero burn: {payload}")

    # -- the watchdog stays independent of the quota history --------------
    def test_a_broken_health_snapshot_does_not_block_the_token_reading(self):
        """The quota history is broken: the token reading must still be collected."""
        log = self._write_log(rows=[request_row(self._ago(0), in_tok=120000, out_tok=34000)])
        health = self.dir / "health.json"
        health.write_text("[]", encoding="utf-8")
        _, out, err = self._run(["--history", str(self.history), "--append", str(health),
                                  "--broker-log", str(log), "--json"])
        payload = None
        if out.strip():
            try:
                payload = json.loads(out)
            except ValueError:
                payload = None
        if payload is None:
            payload = self._snapshot_payload()
        self.assertIsNotNone(payload,
                             f"a broken health snapshot must not stop the token collection: {err}")
        self.assertEqual(hourly_tokens(payload), 154000)

    # -- the Token Plan window (the console figure) -----------------------
    def test_the_interval_total_covers_the_current_token_plan_window_only(self):
        """After a reset the reading must not mix two intervals.

        The MiniMax console counts inside the Token Plan window; the tokens of
        the window before the reset belong to the plan before it (Refs #1671).
        """
        end = self.now + timedelta(minutes=30)  # the current window ends in half an hour
        stamp = lambda moment: moment.isoformat().replace("+00:00", "Z")
        self.history.write_text(json.dumps({
            "ts": stamp(self.now),
            "minimax": {"weekly_pct": 95.0, "weekly_end": stamp(self.now + timedelta(days=3)),
                        "interval_pct": 80.0, "interval_end": stamp(end)}}) + "\n", encoding="utf-8")
        log = self._write_log(rows=[
            request_row(self._ago(60), in_tok=1000, out_tok=0),          # the current window
            request_row(self._ago(5 * 3600), in_tok=500000, out_tok=0),    # the window before it
        ])
        rc, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        payload = json.loads(out)
        self.assertEqual(payload["interval_tokens"], 1000)
        self.assertEqual(datetime.fromisoformat(payload["interval_reset_at"]), end)

    # -- the reading reaches the status surface ---------------------------
    def test_the_broker_log_next_to_the_socket_is_read_without_a_log_override(self):
        """The broker writes ``<socket>.requests.jsonl``: it must be found."""
        socket_path = self.dir / "broker.sock"
        log = Path(str(socket_path) + ".requests.jsonl")
        log.write_text(request_row(self._ago(0), in_tok=120000, out_tok=34000) + "\n", encoding="utf-8")
        rc, out, err = self._run(["--history", str(self.history), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        payload = json.loads(out)
        self.assertEqual(hourly_tokens(payload), 154000)

    def test_the_reading_reaches_the_status_surface_without_a_status_file_argument(self):
        """A normal run must leave the snapshot the lane status reads."""
        log = self._write_log(rows=[request_row(self._ago(0), in_tok=120000, out_tok=34000)])
        rc, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(rc, (0, 1), f"the watchdog failed: {err}")
        state = self.dir / "home" / "phanes-ctl" / "autodispatch"
        state.mkdir(parents=True, exist_ok=True)
        candidates = list(self.dir.glob("*.json")) + list(state.glob("*.json"))
        snapshots = []
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and hourly_tokens(payload) is not None:
                snapshots.append(path)
        self.assertTrue(snapshots,
                        "the watchdog must write the burn snapshot the lane status reads "
                        f"(looked in {self.dir} and {state})")


if __name__ == "__main__":
    unittest.main()
