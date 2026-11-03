"""The MiniMax burn reading the lane status shows (Refs #1671).

The chain under test: the burn watchdog (``tools/mm-burn-rate.py``) measures
the real MiniMax token burn from the provider request log and writes a
snapshot; the lane status of the dispatcher shows the hourly reading while
the reading is fresh.
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


usage = _load("minimax_usage", HERE.parent / "lib" / "dispatch" / "minimax_usage.py")
watchdog = _load("mm_burn_rate", ROOT / "tools" / "mm-burn-rate.py")
autodispatch = _load("autodispatch_minimax_burn", ROOT / "tools" / "lane-autodispatch" / "autodispatch.py")
engine = _load("dispatch_engine_minimax", HERE.parent / "lib" / "dispatch" / "engine.py")


def snapshot(tokens=420, age_s=30, alert=True):
    """A burn snapshot of the MiniMax watchdog."""
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return {"status": "ok", "hourly_tokens": tokens, "token_alert": alert,
            "token_observed_at": stamp, "token_sample_at": stamp, "newest_ts": stamp}


def interval_snapshot(tokens=420, interval=9000, age_s=30):
    """A burn snapshot with the reading of the current Token Plan interval."""
    body = snapshot(tokens=tokens, age_s=age_s)
    body.update({"interval_tokens": interval, "interval_requests": 7,
                 "interval_reset_at": "2026-11-03T18:00:00+00:00",
                 "interval_start": "2026-11-03T13:00:00+00:00"})
    return body


class SnapshotContractTests(unittest.TestCase):
    """A snapshot is only shown while its reading is fresh."""

    def test_a_fresh_snapshot_becomes_status_fields(self):
        body = snapshot()
        fields = usage.snapshot_fields(body)
        self.assertEqual(fields["_minimax_hourly_tokens"], 420)
        self.assertTrue(fields["_minimax_token_alert"])
        self.assertEqual(fields["_minimax_usage_at"], body["token_sample_at"])
        self.assertLessEqual(fields["_minimax_token_age_s"], 60)

    def test_an_expired_reading_is_dropped(self):
        # The watchdog stopped: the last known burn is not the current one.
        self.assertEqual(usage.snapshot_fields(snapshot(age_s=7200)), {})

    def test_a_snapshot_without_a_token_reading_is_dropped(self):
        self.assertEqual(usage.snapshot_fields({"status": "ok", "newest_ts": "2026-11-02T06:30:00+08:00"}), {})

    def test_a_collection_failure_is_shown_without_a_token_count(self):
        # The collector is gone: there is no reading, but the status says so
        # instead of showing a zero burn (Refs #1671).
        stamp = datetime.now(timezone.utc).isoformat()
        fields = usage.snapshot_fields({"status": "ok", "hourly_tokens": None,
                                        "token_state": "collector_down",
                                        "collector_state": "stopped",
                                        "token_observed_at": stamp})
        self.assertNotIn("_minimax_hourly_tokens", fields)
        self.assertEqual(fields["_minimax_token_state"], "collector_down")
        self.assertEqual(fields["_minimax_collector_state"], "stopped")

    def test_an_unverified_collector_is_shown_without_a_token_count(self):
        # The collector cannot be verified: the burn of the hour is unknown,
        # so the status shows the state and not a zero burn (Refs #1671).
        stamp = datetime.now(timezone.utc).isoformat()
        fields = usage.snapshot_fields({"status": "ok", "hourly_tokens": None,
                                        "token_state": "unverified",
                                        "collector_state": "unknown",
                                        "token_observed_at": stamp})
        self.assertNotIn("_minimax_hourly_tokens", fields)
        self.assertEqual(fields["_minimax_token_state"], "unverified")
        self.assertEqual(fields["_minimax_collector_state"], "unknown")

    def test_a_verified_reading_shows_the_state_of_the_collector(self):
        fields = usage.snapshot_fields(dict(snapshot(), collector_state="alive"))
        self.assertEqual(fields["_minimax_collector_state"], "alive")
        self.assertEqual(fields["_minimax_hourly_tokens"], 420)

    def test_the_interval_consumption_is_shown_with_the_hour(self):
        # The MiniMax console shows the consumption of the Token Plan
        # interval too: the status carries it next to the hour (Refs #1671).
        fields = usage.snapshot_fields(interval_snapshot())
        self.assertEqual(fields["_minimax_hourly_tokens"], 420)
        self.assertEqual(fields["_minimax_interval_tokens"], 9000)
        self.assertEqual(fields["_minimax_interval_requests"], 7)
        self.assertEqual(fields["_minimax_interval_reset_at"], "2026-11-03T18:00:00+00:00")

    def test_an_incomplete_interval_is_not_shown(self):
        # The watchdog leaves the interval empty when the log lost rows of
        # it: an incomplete figure is not shown as the consumption.
        body = interval_snapshot()
        body["interval_tokens"] = None
        body["interval_missing_rows"] = 3
        fields = usage.snapshot_fields(body)
        self.assertNotIn("_minimax_interval_tokens", fields)
        self.assertEqual(fields["_minimax_hourly_tokens"], 420)

    def test_a_future_reading_is_dropped(self):
        self.assertEqual(usage.snapshot_fields(snapshot(age_s=-3600)), {})

    def test_a_malformed_snapshot_is_dropped(self):
        self.assertEqual(usage.snapshot_fields("not json"), {})
        self.assertEqual(usage.snapshot_fields({"hourly_tokens": "many"}), {})

    def test_a_zero_burn_is_a_reading(self):
        # A sustained zero burn is the collapse the operator has to see.
        fields = usage.snapshot_fields(snapshot(tokens=0))
        self.assertEqual(fields["_minimax_hourly_tokens"], 0)
        self.assertTrue(fields["_minimax_token_alert"])

    def test_a_missing_snapshot_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "minimax-burn.json")
            self.assertEqual(usage.read_snapshot(path), {})
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("not json")
            self.assertEqual(usage.read_snapshot(path), {})
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(snapshot(), stream)
            self.assertEqual(usage.read_snapshot(path)["_minimax_hourly_tokens"], 420)


class BurnWatchdogChainTests(unittest.TestCase):
    """The watchdog writes the snapshot; the lane status shows its reading."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.previous = os.environ.get("MM_BURN_STATUS")
        os.environ["MM_BURN_STATUS"] = str(self.dir / "minimax-burn.json")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.previous is None:
            os.environ.pop("MM_BURN_STATUS", None)
        else:
            os.environ["MM_BURN_STATUS"] = self.previous

    def _history(self):
        now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
        path = self.dir / "minimax-burn-history.jsonl"
        rows = [{"ts": (now - timedelta(hours=2)).isoformat(),
                 "minimax": {"weekly_pct": 52, "weekly_end": (now + timedelta(days=2)).isoformat()}},
                {"ts": now.isoformat(),
                 "minimax": {"weekly_pct": 51, "weekly_end": (now + timedelta(days=2)).isoformat()}}]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def _request_log(self, minutes=(30, 10)):
        now = datetime.now(timezone.utc)
        path = self.dir / "requests.jsonl"
        rows = [{"ts": (now - timedelta(minutes=minutes[0])).isoformat().replace("+00:00", "Z"),
                 "id": 1, "lane": "M1", "status": 200, "err": "", "ms": 1200,
                 "in_tok": 12, "out_tok": 34},
                {"ts": (now - timedelta(minutes=minutes[1])).isoformat().replace("+00:00", "Z"),
                 "id": 2, "lane": "M2", "status": 200, "err": "", "ms": 900,
                 "in_tok": 100, "out_tok": 20}]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def _run_watchdog(self, history, log):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return watchdog.main(["--history", str(history), "--broker-log", str(log), "--json"])

    def test_the_watchdog_reading_reaches_the_lane_status(self):
        history, log = self._history(), self._request_log()
        code = self._run_watchdog(history, log)
        status = autodispatch.status_snapshot([], {}, {}, 0, 0)
        # 46 + 120 tokens in the hour: a collapsed burn, so the watchdog alerts.
        self.assertEqual(code, 1)
        self.assertEqual(status["_minimax_hourly_tokens"], 166)
        self.assertTrue(status["_minimax_token_alert"])
        self.assertIn("_minimax_usage_at", status)

    def test_a_stopped_watchdog_leaves_the_status_without_a_burn(self):
        history = self._history()
        log = self._request_log()
        self._run_watchdog(history, log)
        snapshot_path = self.dir / "minimax-burn.json"
        body = json.loads(snapshot_path.read_text(encoding="utf-8"))
        # The watchdog stopped: the reading of the last run is not current.
        body["token_observed_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        snapshot_path.write_text(json.dumps(body), encoding="utf-8")
        status = autodispatch.status_snapshot([], {}, {}, 0, 0)
        self.assertNotIn("_minimax_hourly_tokens", status)


class DispatcherBurnChainTests(unittest.TestCase):
    """The fleet dispatcher refreshes and shows the burn reading (Refs #1671)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.state_dir = self.dir / "state"
        self.state_dir.mkdir()
        self.policy = self.dir / "policy.json"
        self.policy.write_text(json.dumps({"claude": {"default_model": "claude-haiku-4.5"}}),
                               encoding="utf-8")
        self.config = self.dir / "dispatch.json"
        self.config.write_text(json.dumps({"repo": "a/b", "local_repo": str(self.dir),
                                           "state_dir": str(self.state_dir),
                                           "policy_file": str(self.policy),
                                           "ssh": {"control_dir": str(self.dir / "cm")}}),
                               encoding="utf-8")
        self.calls = []

    def _runner(self, cmd, timeout=120, inp=None, env=None):
        self.calls.append(cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd))
        return 0, "", ""

    def _dispatcher(self):
        cfg = engine.load_config(str(self.config))
        dispatcher = engine.Dispatcher(cfg, runner=self._runner, out=open(os.devnull, "w"))
        self.addCleanup(dispatcher.out.close)
        return dispatcher

    def test_the_default_burn_command_runs_the_watchdog_of_the_repository(self):
        # Without a configured burn_cmd the fleet status would carry no token
        # reading at all (Refs #1671).
        dispatcher = self._dispatcher()
        command = dispatcher.minimax_burn_command()
        self.assertTrue(command)
        self.assertTrue(command[1].endswith(os.path.join("tools", "mm-burn-rate.py")))
        self.assertIn("--status-file", command)
        self.assertIn(str(self.state_dir / "minimax-burn.json"), command)

    def test_the_dispatcher_refreshes_the_snapshot_and_shows_the_reading(self):
        dispatcher = self._dispatcher()
        now = datetime.now(timezone.utc)
        stamp = (now - timedelta(minutes=5)).isoformat()
        (self.state_dir / "minimax-burn.json").write_text(
            json.dumps({"status": "ok", "hourly_tokens": 4200, "token_alert": False,
                        "token_observed_at": stamp, "token_sample_at": stamp}), encoding="utf-8")
        dispatcher.refresh_minimax_burn()
        # The watchdog is run by the dispatcher, so the snapshot is refreshed
        # without any configured command.
        self.assertTrue(any("mm-burn-rate.py" in call for call in self.calls))
        fields = usage.read_snapshot(str(self.state_dir / "minimax-burn.json"))
        self.assertEqual(fields["_minimax_hourly_tokens"], 4200)


if __name__ == "__main__":
    unittest.main()
