#!/usr/bin/env python3
"""Reproduction of #1671: the MiniMax burn reading on the lane status surface.

The lane status (``lanes-status.json``) is the surface an operator reads when
the fleet burns through the MiniMax quota: on the batch base it carries no
token reading at all, so a collapsed burn was only visible in the MiniMax
console.  These tests pin the contract:

* the status shows the hourly token reading of the burn watchdog;
* a reading that is no longer fresh is not shown as the current one (a
  watchdog that stopped must not pass the last known burn off as current);
* the dispatcher runs the burn watchdog of the repository
  (``tools/mm-burn-rate.py``) by default -- the watchdog lives one directory
  above the dispatcher, so a path inside ``tools/lane-autodispatch/`` does not
  exist.

Every test of this module fails on the batch base (``1f0346dc6``): the
dispatcher of the base neither runs the burn watchdog nor reads a burn
snapshot.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


AD_PATH = Path(__file__).resolve().parents[1] / "lane-autodispatch/autodispatch.py"
WATCHDOG = AD_PATH.parents[1] / "mm-burn-rate.py"

import importlib.util

SPEC = importlib.util.spec_from_file_location("autodispatch_1671", AD_PATH)
AD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AD)


def hourly_tokens(payload):
    """The hourly token reading of a payload, whatever the field is named."""
    for key, value in sorted(payload.items()):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        name = key.lower()
        if "hour" in name and "token" in name:
            return value
    return None


def snapshot_paths(ad, state_dir):
    """Where the dispatcher may read the burn snapshot from.

    The dispatcher owns the path, so it is asked first; the documented default
    names of the state directory are the fallback.
    """
    for name in ("minimax_burn_path", "burn_snapshot_path", "minimax_burn_snapshot_path",
                 "minimax_burn_status_path", "burn_status_path"):
        getter = getattr(ad, name, None)
        if callable(getter):
            try:
                return [Path(getter())]
            except Exception:  # a getter that needs an argument is not the path
                continue
    return [state_dir / name for name in
            ("minimax-burn.json", "minimax-burn-status.json", "minimax_burn.json")]


def burn_snapshot(tokens, observed_at):
    """The snapshot the burn watchdog writes."""
    return {"status": "ok", "hourly_tokens": tokens, "hourly_requests": 3,
            "token_alert": False, "token_state": "ok",
            "token_observed_at": observed_at, "token_sample_at": observed_at,
            "token_last_request_at": observed_at}


class MiniMaxBurnStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.state = self.home / "phanes-ctl" / "autodispatch"
        self.state.mkdir(parents=True)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def _broker_log(self, tokens, coverage_hours=6):
        """The MiniMax provider request log of a running broker."""
        socket_path = self.state / "broker.sock"
        log = Path(str(socket_path) + ".requests.jsonl")
        stamp = self.now.isoformat().replace("+00:00", "Z")
        log.write_text(json.dumps({"ts": stamp, "id": 1, "lane": "M1", "status": 200,
                                   "err": "", "ms": 900, "in_tok": tokens, "out_tok": 0}) + "\n",
                       encoding="utf-8")
        Path(str(log) + ".heartbeat").write_text(json.dumps({
            "ts": stamp, "coverage_start": (self.now - timedelta(hours=coverage_hours)).isoformat()}))
        return socket_path

    def _status_row(self, tokens, observed_at, broker=True, extra=None):
        """The lane status row the dispatcher builds with a burn snapshot.

        The broker request log is only there when the reading is fresh: a
        dispatcher that refreshes the reading itself must not be able to
        refresh a stale snapshot into a current one.
        """
        socket_path = None
        if broker:
            socket_path = self._broker_log(tokens)
        snapshot = burn_snapshot(tokens, observed_at)
        snapshot.update(extra or {})
        with mock.patch.object(AD, "D", str(self.state)):
            paths = snapshot_paths(AD, self.state)
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(snapshot), encoding="utf-8")
        env = {"MM_BURN_STATUS": str(paths[0])}
        if socket_path is not None:
            env["OPENSWARM_PI_BROKER_SOCKET"] = str(socket_path)
            env["PI_BROKER_SOCKET"] = str(socket_path)
        with mock.patch.object(AD, "HOME", str(self.home)), \
                mock.patch.object(AD, "D", str(self.state)), \
                mock.patch.dict(os.environ, env):
            return AD.status_snapshot([], {}, {}, 0, 0)

    def test_the_lane_status_shows_the_hourly_token_reading(self):
        """The status carries the hourly burn the watchdog measured."""
        row = self._status_row(12345, self.now.isoformat())
        self.assertEqual(hourly_tokens(row), 12345,
                         f"the lane status must show the hourly token reading: {row}")

    def test_a_stale_reading_is_not_shown_as_the_current_one(self):
        """A watchdog that stopped must not pass the last known burn off as current."""
        fresh = self._status_row(12345, self.now.isoformat())
        self.assertEqual(hourly_tokens(fresh), 12345,
                         f"a fresh burn reading must be shown: {fresh}")
        stale = (self.now - timedelta(hours=2)).isoformat()
        row = self._status_row(12345, stale, broker=False)
        self.assertIsNone(hourly_tokens(row),
                          f"a stale burn reading must not be shown: {row}")

    def test_the_dispatcher_refreshes_the_reading_and_shows_it(self):
        """The end-to-end chain: the dispatcher runs the watchdog, the status shows it.

        The burn watchdog of the repository reads the provider request log of the
        running broker and writes the snapshot the lane status shows (Refs #1671).
        """
        socket_path = self._broker_log(54321)
        env = {"OPENSWARM_PI_BROKER_SOCKET": str(socket_path),
               "PI_BROKER_SOCKET": str(socket_path)}
        with mock.patch.object(AD, "HOME", str(self.home)), \
                mock.patch.object(AD, "D", str(self.state)), \
                mock.patch.dict(os.environ, env):
            AD.refresh_minimax_burn()
            row = AD.status_snapshot([], {}, {}, 0, 0)
        self.assertEqual(hourly_tokens(row), 54321,
                         f"the lane status must show the hourly token reading: {row}")

    def test_new_collector_coverage_is_incomplete_on_the_status_surface(self):
        socket_path = self._broker_log(20, coverage_hours=0)
        env = {"OPENSWARM_PI_BROKER_SOCKET": str(socket_path),
               "PI_BROKER_SOCKET": str(socket_path)}
        with mock.patch.object(AD, "D", str(self.state)), mock.patch.dict(os.environ, env):
            AD.refresh_minimax_burn()
            row = AD.status_snapshot([], {}, {}, 0, 0)
        self.assertEqual(row.get("_minimax_token_state"), "incomplete")
        self.assertIsNone(hourly_tokens(row))
        self.assertNotIn("_minimax_hourly_buckets", row)
        self.assertFalse(row.get("_minimax_token_alert", False))

    def test_the_status_shows_the_console_figures_of_the_burn(self):
        """The Token Plan window and the clock hours are on the status.

        They are the figures the MiniMax console shows, so an operator samples
        the console and compares them with the status (Refs #1671).
        """
        reset = (self.now + timedelta(minutes=30)).isoformat()
        row = self._status_row(12345, self.now.isoformat(), extra={
            "interval_tokens": 654321, "interval_requests": 12, "interval_reset_at": reset,
            "hourly_buckets": [{"hour": self.now.isoformat(), "tokens": 12345, "requests": 3}]})
        self.assertEqual(row.get("_minimax_interval_tokens"), 654321)
        self.assertEqual(row.get("_minimax_interval_requests"), 12)
        self.assertEqual(row.get("_minimax_interval_reset_at"), reset)
        self.assertEqual(row.get("_minimax_hourly_buckets"),
                         [{"hour": self.now.isoformat(), "tokens": 12345}])

    def test_the_dispatcher_runs_the_burn_watchdog_of_the_repository(self):
        """The default watchdog path must be the existing ``tools/mm-burn-rate.py``."""
        command = None
        for name in ("minimax_burn_command", "burn_command", "minimax_burn_cmd",
                     "minimax_burn_tool", "burn_tool"):
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
            self.assertTrue(scripts, f"the default burn command must run the watchdog: {command}")
            self.assertTrue(Path(scripts[0]).exists(),
                            f"the default burn watchdog does not exist: {scripts[0]}")
            self.assertEqual(Path(scripts[0]).resolve(), WATCHDOG.resolve())
            return
        source = AD_PATH.read_text(encoding="utf-8")
        self.assertIn("mm-burn-rate.py", source,
                      "the dispatcher must run the MiniMax burn watchdog by default")


if __name__ == "__main__":
    unittest.main()
