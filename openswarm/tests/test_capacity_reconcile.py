"""The fleet capacity limits are reconciled with the configured lanes (Refs #1693).

The replay uses the configuration of the night the limit was found: the ``pi``
and ``minimax-cn`` limits of the capacity topology were stuck at 11 while 15 pi
lanes were configured (7 on the box, 8 on the stable hosts).
"""

import importlib.util
import json
import os
import tempfile
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "openswarm_capacity_reconcile", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "capacity_reconcile.py"))
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)


# The topology of that night: pi and minimax-cn stuck at 11, the host limits and
# the fleet limit were still large enough for the lanes that were configured.
REAL_LIMITS = {"total": 16, "pi": 11, "minimax-cn": 11,
               "host:box": 8, "host:ib1": 2, "host:ib2": 2, "host:ib3": 2, "host:ib4": 2,
               "codex": 2, "agy": 1, "copilot": 0}

REAL_HOSTS = ["box", "ib1", "ib2", "ib3", "ib4"]


def real_lanes():
    """The 15 pi lanes of that night: 7 on the box, 8 on the stable hosts."""
    lanes = []
    for number in range(1, 8):
        lanes.append({"name": "PI%d" % number, "backend": "pi", "host": "box",
                      "provider": "minimax-cn"})
    for host in ("ib1", "ib2", "ib3", "ib4"):
        for number in (1, 2):
            lanes.append({"name": "%s-PI%d" % (host.upper(), number), "backend": "pi",
                          "host": host, "provider": "minimax-cn"})
    return lanes


class CapacityReconcileTests(unittest.TestCase):
    """Every limit is compared with the lane count it covers."""

    def test_the_night_topology_flags_pi_and_minimax_cn(self):
        report = R.reconcile(REAL_LIMITS, real_lanes())
        self.assertEqual(report["status"], R.STATUS_ALERT)
        self.assertFalse(report["ok"])
        self.assertEqual([row["limit"] for row in report["alerts"]], ["minimax-cn", "pi"])

    def test_the_alert_names_the_limit_the_lane_count_and_the_deficit(self):
        report = R.reconcile(REAL_LIMITS, real_lanes())
        rows = {row["limit"]: row for row in report["alerts"]}
        for key in ("pi", "minimax-cn"):
            row = rows[key]
            self.assertEqual(row["limit_value"], 11)
            self.assertEqual(row["lanes"], 15)
            self.assertEqual(row["deficit"], 4)
            self.assertEqual(row["status"], R.STATUS_ALERT)

    def test_a_suggested_value_carries_one_lane_of_headroom(self):
        report = R.reconcile(REAL_LIMITS, real_lanes())
        for row in report["alerts"]:
            self.assertEqual(row["suggested"], row["lanes"] + R.DEFAULT_MARGIN)
            self.assertEqual(row["suggested"], 16)

    def test_the_summary_names_every_limit_that_is_behind(self):
        report = R.reconcile(REAL_LIMITS, real_lanes())
        self.assertIn("pi: limit 11, lanes 15, deficit 4, suggested 16", report["summary"])
        self.assertIn("minimax-cn: limit 11, lanes 15, deficit 4, suggested 16", report["summary"])

    def test_sufficient_limits_report_no_alert(self):
        limits = dict(REAL_LIMITS, total=20, pi=15, **{"minimax-cn": 15})
        report = R.reconcile(limits, real_lanes())
        self.assertEqual(report["status"], R.STATUS_OK)
        self.assertTrue(report["ok"])
        self.assertEqual(report["alerts"], [])
        self.assertEqual(report["lane_total"], 15)

    def test_a_host_limit_below_its_lane_count_is_flagged(self):
        limits = dict(REAL_LIMITS, **{"host:ib1": 1})
        report = R.reconcile(limits, real_lanes())
        rows = {row["limit"]: row for row in report["alerts"]}
        self.assertIn("host:ib1", rows)
        self.assertEqual(rows["host:ib1"]["kind"], "host")
        self.assertEqual(rows["host:ib1"]["lanes"], 2)
        self.assertEqual(rows["host:ib1"]["deficit"], 1)
        self.assertEqual(rows["host:ib1"]["suggested"], 3)

    def test_the_total_limit_is_compared_with_the_lane_count(self):
        report = R.reconcile(dict(REAL_LIMITS, total=12), real_lanes())
        rows = {row["limit"]: row for row in report["alerts"]}
        self.assertIn("total", rows)
        self.assertEqual(rows["total"]["kind"], "total")
        self.assertEqual(rows["total"]["lanes"], 15)
        self.assertEqual(rows["total"]["deficit"], 3)

    def test_a_limit_without_a_lane_is_not_flagged(self):
        report = R.reconcile(REAL_LIMITS, real_lanes())
        rows = {row["limit"]: row for row in report["checks"]}
        self.assertEqual(rows["copilot"]["lanes"], 0)
        self.assertEqual(rows["copilot"]["deficit"], 0)
        self.assertIsNone(rows["copilot"]["suggested"])

    def test_the_lane_distribution_counts_backend_provider_and_host(self):
        distribution = R.lane_distribution(real_lanes())
        self.assertEqual(distribution["total"], 15)
        self.assertEqual(distribution["by_backend"], {"pi": 15})
        self.assertEqual(distribution["by_provider"], {"minimax-cn": 15})
        self.assertEqual(distribution["by_host"]["box"], 7)
        self.assertEqual(distribution["by_host"]["ib1"], 2)
        self.assertEqual(distribution["by_host_backend"]["box"], {"pi": 7})

    def test_a_pi_lane_without_a_provider_uses_the_policy_default(self):
        lanes = [{"name": "PI1", "backend": "pi", "host": "box"},
                 {"name": "PI2", "backend": "pi", "host": "box", "provider": "minimax-cn"}]
        distribution = R.lane_distribution(lanes)
        self.assertEqual(distribution["by_provider"], {"minimax-cn": 2})

    def test_the_report_carries_a_stable_alert_signature(self):
        first = R.reconcile(REAL_LIMITS, real_lanes())
        second = R.reconcile(REAL_LIMITS, real_lanes())
        self.assertEqual(first["signature"], second["signature"])
        self.assertEqual(first["signature"], "minimax-cn:11/15;pi:11/15")


class CapacityReconcileFileTests(unittest.TestCase):
    """The files are read; a file that cannot be read is reported, never guessed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.topology = os.path.join(self.root, "topology.json")
        self.lanes = os.path.join(self.root, "lanes.json")

    def write_topology(self, limits=None, hosts=None):
        with open(self.topology, "w", encoding="utf-8") as stream:
            json.dump({"hosts": hosts if hosts is not None else REAL_HOSTS,
                       "limits": limits if limits is not None else REAL_LIMITS}, stream)

    def write_lanes(self, lanes=None):
        with open(self.lanes, "w", encoding="utf-8") as stream:
            json.dump({"lanes": lanes if lanes is not None else real_lanes()}, stream)

    def test_the_files_are_reconciled(self):
        self.write_topology()
        self.write_lanes()
        report = R.reconcile_paths(self.topology, self.lanes)
        self.assertEqual(report["status"], R.STATUS_ALERT)
        self.assertEqual([row["limit"] for row in report["alerts"]], ["minimax-cn", "pi"])
        self.assertEqual(report["topology"], self.topology)
        self.assertEqual(report["lanes"], self.lanes)
        self.assertEqual(report["hosts"], REAL_HOSTS)

    def test_a_missing_topology_is_unverified(self):
        self.write_lanes()
        report = R.reconcile_paths(self.topology, self.lanes)
        self.assertEqual(report["status"], R.STATUS_UNVERIFIED)
        self.assertFalse(report["verified"])
        self.assertEqual(report["alerts"], [])
        self.assertIn(R.UNVERIFIED_LABEL, report["message"])

    def test_a_malformed_topology_is_unverified(self):
        with open(self.topology, "w", encoding="utf-8") as stream:
            stream.write('{"hosts": ["box"]')
        self.write_lanes()
        report = R.reconcile_paths(self.topology, self.lanes)
        self.assertEqual(report["status"], R.STATUS_UNVERIFIED)
        self.assertIn(R.UNVERIFIED_LABEL, report["message"])

    def test_a_topology_without_limits_is_unverified(self):
        with open(self.topology, "w", encoding="utf-8") as stream:
            json.dump({"hosts": ["box"]}, stream)
        self.write_lanes()
        report = R.reconcile_paths(self.topology, self.lanes)
        self.assertEqual(report["status"], R.STATUS_UNVERIFIED)
        self.assertIn("limits", report["error"])

    def test_a_topology_with_a_broken_limit_is_unverified(self):
        self.write_topology(limits=dict(REAL_LIMITS, pi="eleven"))
        self.write_lanes()
        report = R.reconcile_paths(self.topology, self.lanes)
        self.assertEqual(report["status"], R.STATUS_UNVERIFIED)

    def test_a_missing_lane_config_is_unverified(self):
        self.write_topology()
        report = R.reconcile_paths(self.topology, self.lanes)
        self.assertEqual(report["status"], R.STATUS_UNVERIFIED)
        self.assertIn(R.UNVERIFIED_LABEL, report["message"])

    def test_the_reconciliation_never_writes_the_topology(self):
        self.write_topology()
        self.write_lanes()
        with open(self.topology, "rb") as stream:
            before = stream.read()
        report = R.reconcile_paths(self.topology, self.lanes)
        self.assertEqual(report["alerts"], report["alerts"])
        with open(self.topology, "rb") as stream:
            self.assertEqual(stream.read(), before)
        self.assertNotIn("apply", report)

    def test_the_topology_path_follows_the_capacity_directory(self):
        self.assertEqual(R.topology_path(self.root), os.path.join(self.root, "topology.json"))


class CapacityMergeTests(unittest.TestCase):
    """The lanes of the control plane and of every host are one fleet count."""

    def test_the_control_plane_and_the_host_lanes_are_merged(self):
        lanes, incomplete = R.merge_lanes(
            [{"name": "PI1", "backend": "pi", "host": "box"}],
            {"ib1": [{"name": "IB1-PI1", "backend": "pi"}]})
        self.assertEqual(incomplete, [])
        distribution = R.lane_distribution(lanes)
        self.assertEqual(distribution["total"], 2)
        self.assertEqual(distribution["by_host"], {"box": 1, "ib1": 1})

    def test_a_lane_in_the_control_plane_and_on_its_host_is_counted_once(self):
        lanes, incomplete = R.merge_lanes(
            [{"name": "PI1", "backend": "pi", "host": "box"}],
            {"box": [{"name": "PI1", "backend": "pi", "model": "minimax"}]})
        self.assertEqual(incomplete, [])
        self.assertEqual(len(lanes), 1)

    def test_a_control_plane_lane_without_a_host_is_reported(self):
        lanes, incomplete = R.merge_lanes([{"name": "PI1", "backend": "pi"}], {})
        self.assertEqual(len(lanes), 1)
        self.assertEqual(incomplete, ["control-plane lane PI1 has no host"])

    def test_a_control_plane_lane_without_a_host_belongs_to_the_control_host(self):
        lanes, incomplete = R.merge_lanes([{"name": "PI1", "backend": "pi"}], {}, "box")
        self.assertEqual(incomplete, [])
        self.assertEqual(R.lane_distribution(lanes)["by_host"], {"box": 1})

    def test_a_lane_of_a_host_without_a_name_is_reported(self):
        lanes, incomplete = R.merge_lanes([], {"ib1": [{"backend": "pi"}]})
        self.assertEqual(lanes, [])
        self.assertEqual(incomplete, ["lane on ib1 has no name"])


class CapacityUnverifiedTests(unittest.TestCase):
    """A reconciliation that could not read everything claims nothing."""

    def test_an_unverified_report_names_the_reason(self):
        report = R.unverified("the topology is unreadable")
        self.assertEqual(report["status"], R.STATUS_UNVERIFIED)
        self.assertFalse(report["verified"])
        self.assertEqual(report["alerts"], [])
        self.assertIn(R.UNVERIFIED_LABEL, report["message"])

    def test_a_report_can_be_marked_unverified(self):
        report = R.reconcile(REAL_LIMITS, real_lanes())
        self.assertEqual(report["status"], R.STATUS_ALERT)
        marked = R.mark_unverified(report, "the lanes of ib5 cannot be read")
        self.assertEqual(marked["status"], R.STATUS_UNVERIFIED)
        self.assertFalse(marked["verified"])
        self.assertEqual(marked["alerts"], [])
        self.assertEqual(marked["signature"], "")
        self.assertIn("ib5", marked["error"])
        # the counts that could be read stay visible
        self.assertEqual(marked["lane_total"], 15)


if __name__ == "__main__":
    unittest.main()
