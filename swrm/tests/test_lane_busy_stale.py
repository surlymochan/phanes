import concurrent.futures
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import _load


xs = _load.load()
spec = importlib.util.spec_from_file_location(
    "busy_stale_autodispatch", Path(__file__).resolve().parents[2] / "tools/lane-autodispatch/autodispatch.py")
ad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ad)


class LaneBusyStaleTests(unittest.TestCase):
    def test_status_and_supervisor_updates_are_serialized(self):
        # Pause the first transaction after reading state.json, then force the
        # other caller to attempt its lock. Exercise both orders, including the
        # status writes that initialize or normalize the activity clock.
        for activity in (100, 12_000, None):
            for changes in ({"state": "idle", "current_run": None},
                            {"state": "busy", "current_run": "run-2", "last_activity": 10_000},
                            {"last_activity": 10_000}):
                for status_first in (True, False):
                    with self.subTest(activity=activity, changes=changes, status_first=status_first):
                        self.check_concurrent_update(activity, changes, status_first)

    def check_concurrent_update(self, activity, changes, status_first):
        with tempfile.TemporaryDirectory(prefix="lane-busy-stale-") as root:
            lane = {"name": "L1", "backend": "codex", "model": "test-model"}
            state_path = os.path.join(root, "lanes", "L1", "state.json")
            xs.jsave(state_path, {"state": "busy", "current_run": "run-1",
                                 "supervisor_pid": 123, "last_activity": activity})
            read = threading.Event()
            release = threading.Event()
            contending = threading.Event()
            first_thread = []
            jload, flock = xs.jload, xs._flock

            def paused_read(path, default=None):
                value = jload(path, default)
                if path == state_path and not read.is_set():
                    first_thread.append(threading.get_ident())
                    read.set()
                    if not release.wait(5):
                        raise AssertionError("state transaction was not released")
                return value

            def observed_lock(target, op):
                if first_thread and threading.get_ident() != first_thread[0]:
                    contending.set()
                return flock(target, op)

            with mock.patch.object(xs, "DATA", root), \
                    mock.patch.object(xs, "lane_config", return_value=[lane]), \
                    mock.patch.object(xs, "pid_alive", return_value=True), \
                    mock.patch.object(xs, "NOW", return_value=10_000), \
                    mock.patch.object(xs, "jload", side_effect=paused_read), \
                    mock.patch.object(xs, "_flock", side_effect=observed_lock):
                status = lambda: xs.lane_status_rows()[0]
                update = lambda: xs.lane_write_state("L1", **changes)
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(status if status_first else update)
                    try:
                        self.assertTrue(read.wait(5), "first transaction never read state")
                        second = pool.submit(update if status_first else status)
                        self.assertTrue(contending.wait(5), "second transaction did not attempt the lock")
                        self.assertFalse(second.done(), "second transaction bypassed the state lock")
                    finally:
                        release.set()
                    first.result(timeout=5)
                    second.result(timeout=5)
                persisted = jload(state_path, {})
                for key, value in changes.items():
                    self.assertEqual(persisted[key], value)
                if "current_run" in changes:
                    self.assertNotIn("stale_run", persisted)
                    self.assertNotIn("stale_since", persisted)
                elif not status_first:
                    self.assertNotIn("stale_run", persisted)
                else:
                    # Activity after marking does not implicitly acknowledge it.
                    self.assertEqual(persisted.get("stale_run"), "run-1" if activity == 100 else None)

    def test_legacy_busy_without_activity_eventually_expires(self):
        with tempfile.TemporaryDirectory(prefix="lane-busy-stale-") as root:
            os.makedirs(os.path.join(root, "lanes", "L1"))
            lane = {"name": "L1", "backend": "codex", "model": "test-model"}
            with mock.patch.object(xs, "DATA", root), mock.patch.object(xs, "lane_config", return_value=[lane]), \
                    mock.patch.object(xs, "pid_alive", return_value=True), \
                    mock.patch.object(xs, "load_policy", return_value={"thresholds": {"busy_stale_minutes": 1}}):
                with mock.patch.object(xs, "NOW", return_value=100):
                    xs.lane_write_state("L1", state="busy", supervisor_pid=123, current_run="old-run")
                    self.assertEqual(xs.lane_status_rows()[0]["state"], "busy")
                with mock.patch.object(xs, "NOW", return_value=161):
                    xs.lane_write_state("L1", turn_state="running")
                    self.assertEqual(xs.lane_status_rows()[0]["state"], "stale")

    def test_expired_activity_is_stale_even_when_lane_is_alive_and_status_was_refreshed(self):
        with tempfile.TemporaryDirectory(prefix="lane-busy-stale-") as root:
            lane_dir = os.path.join(root, "lanes", "L1")
            os.makedirs(lane_dir)
            with open(os.path.join(lane_dir, "state.json"), "w", encoding="utf-8") as fh:
                json.dump({
                    "state": "busy",
                    "supervisor_pid": 123,
                    "current_run": "run-1",
                    "last_activity": 1_000,
                    "updated": 9_999,
                    "started": 900,
                }, fh)
            lane = {"name": "L1", "backend": "codex", "model": "test-model"}
            patches = (
                mock.patch.object(xs, "DATA", root),
                mock.patch.object(xs, "lane_config", return_value=[lane]),
                mock.patch.object(xs, "pid_alive", return_value=True),
                mock.patch.object(xs, "NOW", return_value=10_000),
                mock.patch.object(xs, "execution_provider", return_value="test-provider"),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                row = xs.lane_status_rows()[0]
                state_path = xs.lane_state_path("L1")
                persisted = xs.jload(state_path, {})

        self.assertEqual(row["current_run"], "run-1")
        self.assertEqual(row["state"], "stale")
        self.assertEqual(persisted["state"], "busy")
        self.assertEqual(persisted["stale_run"], "run-1")
        self.assertEqual(persisted["stale_since"], 10_000)

    def test_stale_marker_survives_new_activity_and_status_refresh(self):
        with tempfile.TemporaryDirectory(prefix="lane-busy-stale-") as root:
            lane_dir = os.path.join(root, "lanes", "L1")
            os.makedirs(lane_dir)
            state_path = os.path.join(lane_dir, "state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump({"state": "busy", "supervisor_pid": 123, "current_run": "run-1",
                           "last_activity": 1_000, "updated": 9_999}, fh)
            lane = {"name": "L1", "backend": "codex", "model": "test-model"}
            with mock.patch.object(xs, "DATA", root), mock.patch.object(xs, "lane_config", return_value=[lane]), \
                    mock.patch.object(xs, "pid_alive", return_value=True), mock.patch.object(xs, "NOW", return_value=10_000):
                self.assertEqual(xs.lane_status_rows()[0]["state"], "stale")
                xs.lane_write_state("L1", last_activity=9_999)
                row = xs.lane_status_rows()[0]
                persisted = xs.jload(state_path, {})

        self.assertEqual(row["state"], "stale")
        self.assertEqual(row["current_run"], "run-1")
        self.assertEqual(persisted["stale_run"], "run-1")

    def test_executor_events_refresh_activity_but_status_writes_do_not(self):
        with tempfile.TemporaryDirectory(prefix="lane-busy-stale-") as root:
            lane_dir = os.path.join(root, "lanes", "L1")
            run_dir = os.path.join(root, "runs", "run-1")
            os.makedirs(lane_dir)
            os.makedirs(run_dir)
            state_path = os.path.join(lane_dir, "state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump({"state": "busy", "current_run": "run-1", "last_activity": 100}, fh)
            event_path = os.path.join(run_dir, "events.1.codex.jsonl")
            lane = {"name": "L1", "backend": "codex", "model": "test-model"}
            with mock.patch.object(xs, "DATA", root), mock.patch.object(xs, "lane_config", return_value=[lane]), \
                    mock.patch.object(xs, "NOW", return_value=200):
                xs.lane_write_state("L1", turn_state="running")
                unchanged = xs.jload(state_path, {})["last_activity"]
                xs.lane_record_event(event_path, {"type": "item/started"})
                changed = xs.jload(state_path, {})["last_activity"]

        self.assertEqual(unchanged, 100)
        self.assertEqual(changed, 200)

    def test_future_activity_is_normalized_once_across_status_ticks(self):
        with tempfile.TemporaryDirectory(prefix="lane-busy-stale-") as root:
            lane_dir = os.path.join(root, "lanes", "L1")
            os.makedirs(lane_dir)
            state_path = os.path.join(lane_dir, "state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump({"state": "busy", "supervisor_pid": 123, "current_run": "run-1",
                           "last_activity": 12_000}, fh)
            lane = {"name": "L1", "backend": "codex", "model": "test-model"}
            patches = (
                mock.patch.object(xs, "DATA", root), mock.patch.object(xs, "lane_config", return_value=[lane]),
                mock.patch.object(xs, "pid_alive", return_value=True),
                mock.patch.object(xs, "load_policy", return_value={"thresholds": {"busy_stale_minutes": 1}}),
            )
            with patches[0], patches[1], patches[2], patches[3], mock.patch.object(xs, "NOW", return_value=10_000):
                self.assertEqual(xs.lane_status_rows()[0]["state"], "busy")
                self.assertEqual(xs.jload(state_path, {})["last_activity"], 10_000)
            with patches[0], patches[1], patches[2], patches[3], mock.patch.object(xs, "NOW", return_value=10_061):
                row = xs.lane_status_rows()[0]

        self.assertEqual(row["state"], "stale")
        self.assertEqual(row["current_run"], "run-1")

    def test_unreachable_supervisor_does_not_hide_an_expired_run(self):
        with tempfile.TemporaryDirectory(prefix="lane-busy-stale-") as root:
            lane_dir = os.path.join(root, "lanes", "L1")
            os.makedirs(lane_dir)
            with open(os.path.join(lane_dir, "state.json"), "w", encoding="utf-8") as fh:
                json.dump({"state": "busy", "supervisor_pid": 123, "current_run": "run-1",
                           "last_activity": 1_000}, fh)
            lane = {"name": "L1", "backend": "codex", "model": "test-model"}
            with mock.patch.object(xs, "DATA", root), mock.patch.object(xs, "lane_config", return_value=[lane]), \
                    mock.patch.object(xs, "pid_alive", return_value=False), mock.patch.object(xs, "NOW", return_value=10_000):
                row = xs.lane_status_rows()[0]

        self.assertEqual(row["state"], "stale")
        self.assertEqual(row["current_run"], "run-1")


class AutodispatchBusyStaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dispatch-busy-stale-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lanes = {"L1": {"host": "offline-host", "backend": "codex"}}
        self.state = {"L1": {"issue": 1786, "run": "run-1", "at": "2000-01-01 00:00:00"}}
        for name, value in (("D", str(self.root)), ("LOW", 0)):
            patch = mock.patch.object(ad, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("audit_switches", "host_fail", "host_good", "log", "replenish"):
            patch = mock.patch.object(ad, name)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(ad, "paused_scopes", return_value=set())
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, AD_BUSY_STALE_MINUTES="1")
        patch.start()
        self.addCleanup(patch.stop)

    def tick(self, now, response=None, queue_error=False):
        with mock.patch.object(ad.time, "time", return_value=now), \
                mock.patch.object(ad, "host_ok", return_value=response is not None), \
                mock.patch.object(ad, "remote", return_value=response) as remote, \
                mock.patch.object(ad, "queue", side_effect=RuntimeError("queue unavailable") if queue_error else None,
                                  return_value=[]), \
                mock.patch.object(ad, "gh", return_value=(0, "{}", "")):
            ad.tick(self.lanes, self.state, 0)
            if response is None:
                remote.assert_not_called()
        return json.loads((self.root / "lanes-status.json").read_text())

    def probe(self):
        result = subprocess.run([sys.executable, "-c", ad.REMOTE_STATUS, "L1", "run-1"],
                                env=dict(os.environ, OPENSWARM_HOME=str(self.root)),
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_real_executor_activity_keeps_long_run_busy_then_expires_without_events(self):
        (self.root / "lanes/L1").mkdir(parents=True)
        (self.root / "runs/run-1").mkdir(parents=True)
        lane = {"name": "L1", "backend": "codex", "model": "test-model"}
        with mock.patch.object(xs, "DATA", str(self.root)), \
                mock.patch.object(xs, "lane_config", return_value=[lane]):
            for now in (100, 150, 200, 250):
                with mock.patch.object(xs, "NOW", return_value=now):
                    xs.lane_write_state("L1", state="busy", current_run="run-1")
                    xs.lane_record_event(str(self.root / "runs/run-1/events.1.codex.jsonl"),
                                         {"type": "item/started"})
                status = self.tick(now, self.probe())
                self.assertEqual(status["L1"], "busy #1786")
            with mock.patch.object(xs, "NOW", return_value=311):
                xs.lane_write_state("L1", turn_state="running")
            status = self.tick(311, self.probe())
            self.assertIn("stale", status["L1"])
            with mock.patch.object(xs, "NOW", return_value=312):
                xs.lane_record_event(str(self.root / "runs/run-1/events.1.codex.jsonl"),
                                     {"type": "item/completed"})
            self.assertIn("stale", self.tick(312, self.probe())["L1"])

    def test_host_backoff_persists_stale_until_recovery_harvest(self):
        self.state["L1"]["last_activity"] = 100
        status = self.tick(161)
        self.assertIn("stale", status["L1"])
        self.assertEqual(status["_busy"], 0)
        self.assertEqual(status["_stale"], 1)
        self.state = json.loads((self.root / "state.json").read_text())
        self.assertIn("stale", self.tick(200)["L1"])
        self.assertEqual(self.state["L1"]["run"], "run-1")
        info = {"status": "done", "commit": "result-commit", "branch": "result-branch", "repo": "/executor/repo"}
        with mock.patch.object(ad, "finish", wraps=ad.finish) as finish, \
                mock.patch.object(ad, "harvest_fetch", return_value=(True, "", 0)), \
                mock.patch.object(ad, "filter_harvest", return_value=("result-commit", [], False)), \
                mock.patch.object(ad, "sh", return_value=(0, "#1786 fix", "")), \
                mock.patch.object(ad, "validate_lane_pr", return_value=[]):
            status = self.tick(201, {"lane": "idle", "run": info})
            self.assertEqual(finish.call_args.args[2]["run"], "run-1")
            self.assertEqual(finish.call_args.args[3], info)
        self.assertNotIn("L1", self.state)
        self.assertEqual(status["L1"], "idle")

    def test_ssh_failure_uses_cached_activity_before_backoff(self):
        self.state["L1"]["last_activity"] = 100
        with mock.patch.object(ad, "sh", return_value=(255, "", "Connection timed out")):
            response = ad.remote("L1", self.lanes["L1"], "run-1")
        self.assertIn("error", response)
        self.assertIn("stale", self.tick(161, response)["L1"])

    def test_queue_failure_still_updates_stale_status_and_state(self):
        self.state["L1"]["last_activity"] = 100
        status = self.tick(161, queue_error=True)
        self.assertIn("stale", status["L1"])
        self.assertEqual(status["_queue_error"], "queue unavailable")
        self.assertIsNone(status["_ready"])
        self.assertEqual(json.loads((self.root / "state.json").read_text())["L1"]["stale_since"], 161)

    def test_legacy_run_gets_one_observation_window_even_offline(self):
        self.assertEqual(self.tick(100)["L1"], "busy #1786")
        self.state = json.loads((self.root / "state.json").read_text())
        self.assertIn("stale", self.tick(161)["L1"])

    def test_repeated_future_host_reading_does_not_refresh_activity(self):
        response = {"lane": "busy", "current_run": "run-1", "last_activity": 10000}
        self.assertEqual(self.tick(100, response)["L1"], "busy #1786")
        self.assertIn("stale", self.tick(161, response)["L1"])

    def test_corrected_host_clock_accepts_new_activity(self):
        response = {"lane": "busy", "current_run": "run-1", "last_activity": 10000}
        self.tick(100, response)
        response["last_activity"] = 150
        self.assertEqual(self.tick(150, response)["L1"], "busy #1786")
        self.assertEqual(self.tick(200, response)["L1"], "busy #1786")

    def test_reviewed_binary_artifacts_are_absent(self):
        root = Path(__file__).resolve().parents[2]
        for directory in ("openswarm/tests", "swrm/tests"):
            artifacts = list((root / directory).glob("*.so"))
            self.assertEqual(artifacts, [], f"unexpected shared-library artifacts in {directory}")


# #1786 review closure (migration: openswarm/bin/openswarm -> swrm/bin/swrm;
# openswarm/tests/test_dispatch_busy_stale.py -> this file; the dispatch status
# writer lives in tools/lane-autodispatch/autodispatch.py).
# B1 -> swrm/tests/test_lane_busy_stale.py:334 ->
# test_reviewed_binary_artifacts_are_absent -> PASS; the reviewed .so files are
# absent, and `git diff a4a49b831 --numstat` contains only three text files.
# Status writer blocker -> tools/lane-autodispatch/autodispatch.py:878,1245 ->
# test_host_backoff_persists_stale_until_recovery_harvest,
# test_ssh_failure_uses_cached_activity_before_backoff,
# test_queue_failure_still_updates_stale_status_and_state -> PASS.
# Legacy clock blocker -> swrm/bin/swrm:8912 ->
# test_legacy_busy_without_activity_eventually_expires,
# test_legacy_run_gets_one_observation_window_even_offline -> PASS.
# Concurrent state writer blocker -> swrm/bin/swrm:7172,8904 ->
# test_status_and_supervisor_updates_are_serialized -> PASS (both transaction
# orders; completion, new run and fresh activity; stale, legacy and skewed clocks).
# Coverage blocker -> tools/lane-autodispatch/autodispatch.py:626,907 ->
# test_real_executor_activity_keeps_long_run_busy_then_expires_without_events,
# test_repeated_future_host_reading_does_not_refresh_activity -> PASS.


if __name__ == "__main__":
    unittest.main()
