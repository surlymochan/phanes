import json
import os
import tempfile
import unittest
from unittest import mock

import _load


xs = _load.load()


class LaneBusyStaleTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
