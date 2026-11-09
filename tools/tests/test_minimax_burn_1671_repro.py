"""Reproduction of #1671: the MiniMax hourly burn reading on the status surface.

The 2026-11-02 burn collapse was only visible in the MiniMax console: the
fleet side had a dispatch count proxy and no hour level token reading at all.
The issue asks for the real hourly token consumption of the provider on the
status surface, so an operator can sample the console and compare it with the
status.

These tests pin that contract:

* the hourly burn is the *token volume* MiniMax reported for the requests of
  the provider request log (``<socket>.requests.jsonl``, the shape
  ``tools/broker-log-summary.py`` reads) and not a dispatch count proxy;
* the reading reaches the status surface (the lane status of the dispatcher)
  without any configuration, and the clock hours of the console are part of
  it, so one hour of the status can be compared with one hour of the console;
* a fleet that burned nothing in the hour is a zero reading that alerts,
  while a collector that stopped is a collection failure that must never be
  passed off as a zero burn;
* a rotated request log still covers the whole hour;
* ``--json`` stays a single JSON object, also when a reading is there.

On the batch base every test of this module fails: the burn watchdog of the
base knows the quota percentages of ``health.json`` and nothing else, and the
dispatcher neither runs the watchdog nor reads a burn snapshot.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
WATCHDOG = ROOT / "tools" / "mm-burn-rate.py"
AD_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
# The MiniMax console counts in Beijing time.
CONSOLE_TZ = timezone(timedelta(hours=8))

_spec = importlib.util.spec_from_file_location("autodispatch_1671", AD_PATH)
AD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AD)


def request_row(ts, in_tok=0, out_tok=0, status=200, lane="M1"):
    """One row of the MiniMax provider request log.

    The shape is the row the broker writes (the one
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


def token_alert(payload):
    """True when the reading raises the low burn alert."""
    for key, value in sorted(payload.items()):
        if isinstance(value, bool) and "token" in key.lower() and "alert" in key.lower():
            return value
    for key in ("alert", "burn_alert", "low_burn_alert"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    return None


def collection_state(payload):
    """The state the reading reports for the token collection."""
    for key, value in sorted(payload.items()):
        if not isinstance(value, str):
            continue
        name = key.lower()
        if "collector" in name and ("state" in name or "status" in name):
            return value
    for key, value in sorted(payload.items()):
        if isinstance(value, str) and "state" in key.lower() and "token" in key.lower():
            return value
    return None


def console_buckets(payload):
    """The clock hours of the console the reading carries."""
    for value in payload.values():
        if not isinstance(value, list) or not value:
            continue
        if not all(isinstance(item, dict) for item in value):
            continue
        if all(any("hour" in str(name).lower() for name in item) for item in value):
            return value
    return None


def bucket_tokens(bucket):
    """The token total of one clock hour."""
    for key, value in sorted(bucket.items()):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if "token" in key.lower():
            return value
    return None


def burn_snapshot(tokens, observed_at, **extra):
    """The burn snapshot the watchdog publishes for the lane status."""
    snapshot = {"status": "ok", "hourly_tokens": tokens, "hourly_requests": 3,
                "token_alert": False, "token_state": "ok", "token_collector": "alive",
                "token_observed_at": observed_at, "observed_at": observed_at,
                "token_sample_at": observed_at, "token_last_request_at": observed_at}
    snapshot.update(extra)
    return snapshot


class MiniMaxBurnReadingReproTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.history = self.dir / "history.jsonl"
        self.history.write_text("", encoding="utf-8")
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    # -- helpers ---------------------------------------------------------
    def _ago(self, seconds):
        return (self.now - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    def _env(self, **extra):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("OPENSWARM_", "XSWARM_", "MM_", "PI_BROKER_"))}
        env["HOME"] = str(self.dir / "home")
        env["TMPDIR"] = str(self.dir)
        env.update(extra)
        return env

    def _run(self, args, env=None):
        proc = subprocess.run([sys.executable, str(WATCHDOG)] + args, capture_output=True,
                              text=True, timeout=60, env=env or self._env(), cwd=str(self.dir))
        return proc.returncode, proc.stdout, proc.stderr

    def _reading(self, out, err):
        """The reading of a watchdog run."""
        if not out.strip():
            self.fail("the burn watchdog printed no reading: %s" % err.strip())
        try:
            payload = json.loads(out)
        except ValueError:
            self.fail("the burn watchdog did not print one JSON object: %r / %s" % (out, err.strip()))
        return payload

    def _write_log(self, name="requests.jsonl", rows=(), heartbeat=True, coverage_hours=6):
        """A provider request log with the heartbeat of a live collector."""
        log = self.dir / name
        log.write_text("".join(row + "\n" for row in rows), encoding="utf-8")
        if heartbeat:
            beat = Path(str(log) + ".heartbeat")
            beat.write_text(json.dumps({"ts": self.now.isoformat().replace("+00:00", "Z"),
                                        "coverage_start": (self.now - timedelta(hours=coverage_hours))
                                        .isoformat().replace("+00:00", "Z")}), encoding="utf-8")
        return log

    # -- the reading -----------------------------------------------------
    def test_the_hourly_burn_is_the_real_token_volume_of_the_provider_log(self):
        """The hourly burn is the token volume MiniMax reported, not a count."""
        log = self._write_log(rows=[
            request_row(self._ago(60), in_tok=120000, out_tok=34000),
            request_row(self._ago(120), in_tok=500, out_tok=500),
            request_row(self._ago(7200), in_tok=5000000, out_tok=5000000),  # older than the hour
        ])
        code, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(code, (0, 1),
                      "the burn watchdog failed (the base knows no token collection): %s" % err.strip())
        payload = self._reading(out, err)
        self.assertEqual(hourly_tokens(payload), 155000,
                         "the hourly burn must be the token volume of the provider: %r" % payload)

    def test_the_json_output_is_one_object_that_carries_the_reading(self):
        """``--json`` must stay a single JSON object, also with a reading."""
        log = self._write_log(rows=[request_row(self._ago(60), in_tok=120000, out_tok=34000)])
        code, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(code, (0, 1),
                      "the burn watchdog failed (the base knows no token collection): %s" % err.strip())
        payload = self._reading(out, err)  # one JSON object, nothing else
        self.assertIsInstance(payload, dict)
        self.assertEqual(hourly_tokens(payload), 154000,
                         "the JSON output must carry the hourly reading: %r" % payload)

    def test_a_quiet_hour_of_a_live_collector_is_a_zero_burn_that_alerts(self):
        """A fleet that burned nothing in the hour is a zero reading that alerts.

        The heartbeat of the broker proves that the collector is alive: a quiet
        hour of a live collector is the collapse of #1671 and not a broken
        collection.
        """
        log = self._write_log(rows=[request_row(self._ago(7200), in_tok=10, out_tok=10)])
        code, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(code, (0, 1),
                      "the burn watchdog failed (the base knows no token collection): %s" % err.strip())
        payload = self._reading(out, err)
        self.assertEqual(hourly_tokens(payload), 0,
                         "a quiet hour of a live collector is a zero reading: %r" % payload)
        self.assertTrue(token_alert(payload), "a zero burn must alert: %r" % payload)

    def test_a_stopped_collector_is_never_reported_as_a_zero_burn(self):
        """A collector that stopped is a collection failure, not a zero burn."""
        log = self._write_log(rows=[request_row(self._ago(7200), in_tok=10, out_tok=10)])
        beat = Path(str(log) + ".heartbeat")
        stale = (self.now - timedelta(hours=2)).timestamp()
        os.utime(beat, (stale, stale))  # the collector stopped two hours ago
        code, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(code, (0, 1),
                      "the burn watchdog failed (the base knows no token collection): %s" % err.strip())
        payload = self._reading(out, err)
        self.assertFalse(token_alert(payload), "a stopped collector must not alert: %r" % payload)
        self.assertNotEqual(hourly_tokens(payload), 0,
                            "a stopped collector is not a fleet that burned nothing: %r" % payload)
        state = collection_state(payload)
        self.assertIsNotNone(state, "the reading must report the state of the collector: %r" % payload)
        self.assertNotIn(state, ("alive", "ok", "idle"),
                         "a stopped collector must not be reported as a live one: %r" % payload)

    def test_a_rotated_broker_log_still_covers_the_whole_hour(self):
        """A rotation inside the hour must not drop the earlier half of it."""
        self._write_log(name="requests.jsonl.1",
                        rows=[request_row(self._ago(1500), in_tok=800000, out_tok=200000)],
                        heartbeat=False)
        log = self._write_log(rows=[request_row(self._ago(0), in_tok=900000, out_tok=100000)])
        code, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(code, (0, 1),
                      "the burn watchdog failed (the base knows no token collection): %s" % err.strip())
        payload = self._reading(out, err)
        self.assertEqual(hourly_tokens(payload), 2000000,
                         "a rotated log must still cover the whole hour: %r" % payload)

    def test_the_reading_carries_the_clock_hours_of_the_console(self):
        """The status must carry the hours the MiniMax console shows.

        An operator samples the console and compares one hour of the console
        with one hour of the reading (Refs #1671).
        """
        hour = self.now.astimezone(CONSOLE_TZ).replace(minute=0, second=0, microsecond=0)
        log = self._write_log(rows=[
            request_row(hour.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                        in_tok=120000, out_tok=34000),
            request_row(self._ago(7200), in_tok=7, out_tok=7),  # an older hour
        ])
        code, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(code, (0, 1),
                      "the burn watchdog failed (the base knows no token collection): %s" % err.strip())
        payload = self._reading(out, err)
        buckets = console_buckets(payload)
        self.assertIsNotNone(buckets, "the reading must carry the clock hours of the console: %r" % payload)
        current = None
        for bucket in buckets:
            stamp = bucket.get("hour")
            if not isinstance(stamp, str):
                continue
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            self.assertEqual(moment.utcoffset(), timedelta(hours=8),
                             "the clock hours of the reading must be the hours of the console: %r" % bucket)
            if moment == hour:
                current = bucket
        self.assertIsNotNone(current, "the current hour of the console is missing: %r" % buckets)
        self.assertEqual(bucket_tokens(current), 154000,
                         "the hour of the console must carry the tokens of that hour: %r" % current)

    def test_the_reading_is_published_to_the_status_surface_without_configuration(self):
        """A normal run must leave the snapshot the lane status reads."""
        log = self._write_log(rows=[request_row(self._ago(60), in_tok=120000, out_tok=34000)])
        code, out, err = self._run(["--history", str(self.history), "--broker-log", str(log), "--json"])
        self.assertIn(code, (0, 1),
                      "the burn watchdog failed (the base knows no token collection): %s" % err.strip())
        state = self.dir / "home" / "phanes-ctl" / "autodispatch"
        snapshots = []
        for path in list(state.glob("*.json")) + list(self.dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and hourly_tokens(payload) is not None:
                snapshots.append(payload)
        self.assertTrue(snapshots,
                        "the watchdog must publish the burn snapshot the lane status reads "
                        "(looked in %s and %s)" % (state, self.dir))

    # -- the lane status -------------------------------------------------
    def test_the_lane_status_shows_the_hourly_token_reading(self):
        """The lane status carries the hourly burn the watchdog measured."""
        home = self.dir / "home"
        state = home / "phanes-ctl" / "autodispatch"
        state.mkdir(parents=True)
        observed = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        snapshot = burn_snapshot(12345, observed)
        (state / "minimax-burn.json").write_text(json.dumps(snapshot), encoding="utf-8")
        with mock.patch.object(AD, "HOME", str(home)), \
                mock.patch.object(AD, "D", str(state)), \
                mock.patch.dict(os.environ, {"MM_BURN_STATUS": str(state / "minimax-burn.json")}):
            row = AD.status_snapshot([], {}, {}, 0, 0)
        self.assertEqual(hourly_tokens(row), 12345,
                         "the lane status must show the hourly token reading: %r" % row)

    def test_a_stale_reading_is_not_shown_as_the_current_one(self):
        """A watchdog that stopped must not pass the last known burn off as current."""
        home = self.dir / "home"
        state = home / "phanes-ctl" / "autodispatch"
        state.mkdir(parents=True)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for observed, shown in ((now.isoformat(), True),
                                ((now - timedelta(hours=2)).isoformat(), False)):
            with self.subTest(observed=observed):
                snapshot = burn_snapshot(12345, observed)
                (state / "minimax-burn.json").write_text(json.dumps(snapshot), encoding="utf-8")
                with mock.patch.object(AD, "HOME", str(home)), \
                        mock.patch.object(AD, "D", str(state)), \
                        mock.patch.dict(os.environ,
                                        {"MM_BURN_STATUS": str(state / "minimax-burn.json")}):
                    row = AD.status_snapshot([], {}, {}, 0, 0)
                if shown:
                    self.assertEqual(hourly_tokens(row), 12345,
                                     "a fresh burn reading must be shown: %r" % row)
                else:
                    self.assertIsNone(hourly_tokens(row),
                                      "a stale burn reading must not be shown: %r" % row)

    def test_the_dispatcher_runs_the_watchdog_of_the_repository(self):
        """The default burn command must run ``tools/mm-burn-rate.py``.

        The watchdog of the repository lives one directory above the
        dispatcher, so a path inside ``tools/lane-autodispatch/`` does not
        exist.
        """
        command = None
        for name in ("minimax_burn_command", "burn_command", "minimax_burn_cmd",
                     "burn_watchdog_command", "minimax_burn_tool", "burn_tool"):
            getter = getattr(AD, name, None)
            if not callable(getter):
                continue
            try:
                value = getter()
            except TypeError:
                continue
            if isinstance(value, (list, tuple)):
                command = [str(part) for part in value]
            elif isinstance(value, str):
                command = [value]
            if command:
                break
        if command:
            scripts = [part for part in command if part.endswith("mm-burn-rate.py")]
            self.assertTrue(scripts, "the default burn command must run the watchdog: %r" % command)
            self.assertTrue(Path(scripts[0]).exists(),
                            "the default burn watchdog does not exist: %s" % scripts[0])
            self.assertEqual(Path(scripts[0]).resolve(), WATCHDOG.resolve())
            return
        self.assertIn("mm-burn-rate.py", AD_PATH.read_text(encoding="utf-8"),
                      "the dispatcher must run the MiniMax burn watchdog by default")


if __name__ == "__main__":
    unittest.main()
