"""Schema 2 dispatch lane status and legacy-reader compatibility."""
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "dispatch_engine_status_test", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)

R_SPEC = importlib.util.spec_from_file_location(
    "dispatch_capacity_reconcile_test", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "capacity_reconcile.py"))
R = importlib.util.module_from_spec(R_SPEC)
R_SPEC.loader.exec_module(R)


class LaneStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = os.path.join(self.tmp.name, "dispatch.json")
        self.policy_path = os.path.join(self.tmp.name, "policy.json")
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)
        self.topology_path = os.path.join(self.tmp.name, "topology.json")
        self.brain_log = os.path.join(self.tmp.name, "brain.log")
        self.health_state = os.path.join(self.tmp.name, "fleet-health.json")
        with open(self.policy_path, "w", encoding="utf-8") as stream:
            json.dump({"claude": {"default_model": "claude-haiku-4.5"}}, stream)
        with open(self.topology_path, "w", encoding="utf-8") as stream:
            json.dump({"hosts": ["gorelay", "gores1"],
                       "limits": {"total": 4, "claude": 1, "host:gorelay": 2}}, stream)
        with open(self.cfg_path, "w", encoding="utf-8") as stream:
            json.dump({"repo": "a/b", "local_repo": self.tmp.name, "state_dir": self.state_dir,
                       "policy_file": self.policy_path, "brain_log": self.brain_log,
                       "capacity_topology": self.topology_path,
                       "fleet_health_file": self.health_state,
                       "ssh": {"control_dir": os.path.join(self.tmp.name, "cm")}}, stream)
        self.cfg = E.load_config(self.cfg_path)
        self.d = E.Dispatcher(self.cfg, out=open(os.devnull, "w"))
        self.addCleanup(self.d.out.close)

    def lanes(self, configured_model):
        return {"GH1": {"backend": "claude", "host": "gorelay", "model": configured_model},
                "GH2": {"backend": "claude", "host": "gores1", "model": "claude-haiku-5.5"}}

    def tick(self, lanes, state):
        result = {name: {"lane": "idle", "run": {"model": "claude-haiku-5.5", "timestamp": "2026-11-02T07:30:00Z"}}
                  for name in lanes}
        self.d.probe = lambda _lanes, _state: result
        self.d.harvest = lambda *_args: None
        self.d.sweep_leaked_inprogress = lambda *_args: None
        self.d.queue = lambda: []
        self.d.dispatch_reviews = lambda *_args: None
        self.d.assign_issues = lambda *_args: None
        self.d.replenish = lambda *_args: None
        self.d.tick(lanes, state)

    def status(self, configured_model):
        self.tick(self.lanes(configured_model), {})
        with open(self.d.path("lanes-status.json"), encoding="utf-8") as stream:
            return json.load(stream)

    def fleet_snapshot(self, limits, lanes):
        """Write the fleet health snapshot the dispatcher reads: the whole fleet, not its own lanes."""
        report = R.reconcile(limits, lanes)
        with open(self.health_state, "w", encoding="utf-8") as stream:
            json.dump({"hosts": {}, "updated_at": "2026-11-02T22:00:00",
                       "capacity": {"signature": report["signature"], "status": report["status"],
                                    "at": "2026-11-02T22:00:00", "report": report}}, stream)
        return report

    def fleet_lanes(self):
        """The lanes of that night: 7 pi lanes on the box and 8 on the stable hosts."""
        lanes = [{"name": "PI%d" % number, "backend": "pi", "provider": "minimax-cn", "host": "box"}
                 for number in range(1, 8)]
        for host in ("ib1", "ib2", "ib3", "ib4"):
            lanes += [{"name": "%s-PI%d" % (host.upper(), number), "backend": "pi",
                       "provider": "minimax-cn", "host": host} for number in (1, 2)]
        return lanes

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

    def test_the_capacity_reconciliation_is_part_of_the_lane_status(self):
        """The watchdog reads the fleet capacity result from the status snapshot.

        The result is the fleet-wide one: 7 pi lanes on the box plus 8 on the
        stable hosts are 15 lanes against the limit 11 of that night.
        """
        self.fleet_snapshot({"total": 16, "pi": 11, "minimax-cn": 11}, self.fleet_lanes())
        data = self.status("claude-haiku-5.5")
        capacity = data["_capacity"]
        self.assertEqual(capacity["status"], "alert")
        self.assertEqual(capacity["source"], "fleet-health")
        self.assertEqual(capacity["lane_total"], 15)
        self.assertEqual(capacity["by_backend"], {"pi": 15})
        self.assertEqual(capacity["by_host"]["box"], 7)
        self.assertEqual(capacity["by_host"]["ib1"], 2)
        rows = {row["limit"]: row for row in capacity["alerts"]}
        self.assertEqual(sorted(rows), ["minimax-cn", "pi"])
        self.assertEqual(rows["pi"]["limit_value"], 11)
        self.assertEqual(rows["pi"]["lanes"], 15)
        self.assertEqual(rows["pi"]["deficit"], 4)
        self.assertEqual(rows["pi"]["suggested"], 16)
        self.assertEqual(rows["minimax-cn"]["deficit"], 4)

    def test_without_a_fleet_snapshot_the_fleet_is_not_verified(self):
        """The dispatch lanes are a partial view: nothing is claimed about the fleet."""
        data = self.status("claude-haiku-5.5")
        capacity = data["_capacity"]
        self.assertEqual(capacity["status"], "unverified")
        self.assertFalse(capacity["verified"])
        self.assertEqual(capacity["alerts"], [])
        self.assertIn(R.UNVERIFIED_LABEL, capacity["message"])
        self.assertEqual(capacity["source"], "dispatcher-lanes")

    def test_the_capacity_alert_is_written_to_the_brain_log_once(self):
        """The alert is written when it appears, not on every tick."""
        self.fleet_snapshot({"total": 16, "pi": 11, "minimax-cn": 11}, self.fleet_lanes())
        lanes = self.lanes("claude-haiku-5.5")
        self.tick(lanes, {})
        with open(self.brain_log, encoding="utf-8") as stream:
            first = stream.read()
        self.assertIn("fleet capacity", first)
        self.assertIn("pi: limit 11, lanes 15, deficit 4", first)
        with open(self.d.path("state.json"), encoding="utf-8") as stream:
            state = json.load(stream)
        self.assertEqual(state.get("_capacity_alert"), "minimax-cn:11/15;pi:11/15")
        # The resident loop loads the state again on the next tick.
        self.tick(lanes, state)
        with open(self.brain_log, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), first)

    def test_capacity_alert_is_retried_after_brain_log_failure(self):
        lanes = self.lanes("claude-haiku-5.5")
        self.fleet_snapshot({"total": 16, "pi": 11, "minimax-cn": 11}, self.fleet_lanes())
        self.cfg["brain_log"] = os.path.join(self.tmp.name, "missing", "brain.log")
        state = {}
        capacity = self.d.capacity_report(lanes)
        self.assertFalse(self.d.capacity_alert(capacity, state))
        self.assertNotIn("_capacity_alert", state)
        os.mkdir(os.path.dirname(self.cfg["brain_log"]))
        self.cfg["brain_log"] = self.brain_log
        self.assertTrue(self.d.capacity_alert(capacity, state))
        self.assertIn("_capacity_alert", state)


if __name__ == "__main__":
    unittest.main()
