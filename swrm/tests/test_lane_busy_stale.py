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

        self.assertEqual(row["current_run"], "run-1")
        self.assertEqual(row["state"], "stale")


if __name__ == "__main__":
    unittest.main()
