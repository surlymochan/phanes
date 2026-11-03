"""Schema 2 dispatch lane status and legacy-reader compatibility."""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "dispatch_engine_status_test", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)


class LaneStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = os.path.join(self.tmp.name, "dispatch.json")
        self.policy_path = os.path.join(self.tmp.name, "policy.json")
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)
        with open(self.policy_path, "w", encoding="utf-8") as stream:
            json.dump({"claude": {"default_model": "claude-haiku-4.5"}}, stream)
        with open(self.cfg_path, "w", encoding="utf-8") as stream:
            json.dump({"repo": "a/b", "local_repo": self.tmp.name, "state_dir": self.state_dir,
                       "policy_file": self.policy_path,
                       "ssh": {"control_dir": os.path.join(self.tmp.name, "cm")}}, stream)
        self.cfg = E.load_config(self.cfg_path)
        self.calls = []
        self.d = E.Dispatcher(self.cfg, runner=self._runner, out=open(os.devnull, "w"))
        self.addCleanup(self.d.out.close)

    def _runner(self, cmd, timeout=120, inp=None, env=None):
        """The commands of the dispatcher are recorded, never executed."""
        self.calls.append(cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd))
        return 0, "", ""

    def status(self, configured_model):
        lanes = {"GH1": {"backend": "claude", "host": "gorelay", "model": configured_model},
                 "GH2": {"backend": "claude", "host": "gores1", "model": "claude-haiku-5.5"}}
        result = {name: {"lane": "idle", "run": {"model": "claude-haiku-5.5", "timestamp": "2026-11-02T07:30:00Z"}}
                  for name in lanes}
        self.d.probe = lambda _lanes, _state: result
        self.d.harvest = lambda *_args: None
        self.d.sweep_leaked_inprogress = lambda *_args: None
        self.d.queue = lambda: []
        self.d.dispatch_reviews = lambda *_args: None
        self.d.assign_issues = lambda *_args: None
        self.d.replenish = lambda *_args: None
        self.d.tick(lanes, {})
        with open(self.d.path("lanes-status.json"), encoding="utf-8") as stream:
            return json.load(stream)

    def test_matching_config_and_actual_model_has_no_drift(self):
        data = self.status("claude-haiku-5.5")
        self.assertEqual(data["schema"], 2)
        self.assertEqual(data["GH1"]["model_cfg"], "claude-haiku-5.5")
        self.assertEqual(data["GH1"]["model_last"], "claude-haiku-5.5")
        self.assertEqual(data["GH1"]["model_last_at"], "2026-11-02T07:30:00Z")
        self.assertNotIn("model_drift", data["GH1"])
        self.assertEqual(data["_summary"]["model_drift"], [])

    def test_mismatched_model_is_reported_as_drift(self):
        data = self.status("claude-haiku-4.5")
        self.assertTrue(data["GH1"]["model_drift"])
        self.assertEqual(data["_summary"]["model_drift"], ["GH1"])

    def test_summary_counts_backends_and_states(self):
        summary = self.status("claude-haiku-5.5")["_summary"]
        self.assertEqual(summary["by_backend"], {"claude": 2})
        self.assertEqual((summary["busy"], summary["idle"]), (0, 2))

    def test_legacy_string_entries_are_normalized(self):
        view = E.lanes_status_view({"GH1": "idle", "GH2": "busy #42", "_busy": 1})
        self.assertEqual(view, {"GH1": {"state": "idle"}, "GH2": {"state": "busy #42"}})

    def test_schema_2_file_view_excludes_schema_metadata(self):
        raw = {"schema": 2, "GH1": {"state": "idle", "backend": "claude"},
               "_summary": {"busy": 0}, "_busy": 0}
        view = E.lanes_status_view(raw)
        self.assertNotIn("schema", view)
        self.assertEqual(view["GH1"], {"state": "idle", "backend": "claude"})

    def _burn_snapshot(self, age_s, tokens=166):
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
        with open(os.path.join(self.state_dir, "minimax-burn.json"), "w", encoding="utf-8") as stream:
            json.dump({"status": "ok", "hourly_tokens": tokens, "token_alert": True,
                       "token_observed_at": stamp, "token_sample_at": stamp}, stream)
        return stamp

    def test_lane_status_shows_the_minimax_hourly_token_burn(self):
        # The burn watchdog measures the real MiniMax token burn (Refs #1671).
        stamp = self._burn_snapshot(30)
        data = self.status("claude-haiku-4.5")
        self.assertEqual(data["_minimax_hourly_tokens"], 166)
        self.assertTrue(data["_minimax_token_alert"])
        self.assertEqual(data["_minimax_usage_at"], stamp)

    def test_lane_status_drops_an_expired_minimax_reading(self):
        # A stopped watchdog must not show the last known burn as current.
        self._burn_snapshot(7200)
        data = self.status("claude-haiku-4.5")
        self.assertNotIn("_minimax_hourly_tokens", data)

    def test_lane_status_refreshes_the_burn_snapshot_with_the_default_watchdog(self):
        # Without a configured burn_cmd the fleet status would carry no token
        # reading at all (Refs #1671).
        self.status("claude-haiku-4.5")
        burn = [call for call in self.calls if "mm-burn-rate.py" in call]
        self.assertTrue(burn)
        self.assertIn(os.path.join(self.state_dir, "minimax-burn.json"), burn[0])


if __name__ == "__main__":
    unittest.main()
