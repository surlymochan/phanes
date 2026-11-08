"""The documented fleet topology must be the 2026-11-02 baseline (Refs #1480).

Issue #1480 rewrites `docs/fleet-topology-expected.md` to the 2026-11-02
baseline: the retired Instabox hosts are gone and the remaining remote hosts
carry the ten lanes of the current fleet. This module pins the expected
values, while `test_fleet_topology_baseline.py` pins the invariants that keep
the document from drifting back to the retired fleet.

    cd swrm && python -m unittest discover -s tests -p 'test_fleet_topology_expected.py' -v
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_topology_docs import EXPECTED, fenced_json  # noqa: E402


BASELINE_DATE = "2026-11-02"

# The 2026-11-02 remote fleet: the control plane runs on box, which is not a
# controlled fleet host (docs/fusion-openbot-swrm-v0.2.md).
BASELINE_HOSTS = (
    "worker",
    "gores2",
    "worker-a.example.invalid",
    "relay.example.invalid",
    "broker",
)

# The retired Instabox hosts, spelled as raw `ib1`-`ib5` aliases and as the
# `node-1.example.invalid`-`node-5.example.invalid` names this tree uses.
RETIRED_HOST = re.compile(r"\bib\d+\b|node-[1-5]\.example\.invalid")


class FleetTopologyExpectedTests(unittest.TestCase):
    def setUp(self):
        self.text = EXPECTED.read_text(encoding="utf-8")
        self.topology = fenced_json(self.text)

    def test_no_retired_instabox_hosts_or_limits(self):
        self.assertFalse(any(RETIRED_HOST.search(host) for host in self.topology["hosts"]))
        self.assertFalse(any(
            RETIRED_HOST.search(key.removeprefix("host:"))
            for key in self.topology["limits"]
            if key.startswith("host:")
        ))

    def test_baseline_hosts_are_present(self):
        for host in BASELINE_HOSTS:
            self.assertIn(host, self.topology["hosts"])

    def test_baseline_date_is_recorded(self):
        self.assertIn(BASELINE_DATE, self.text)

    def test_total_capacity_is_at_least_ten(self):
        self.assertGreaterEqual(self.topology["limits"]["total"], 10)

    def test_each_host_has_a_limit(self):
        host_limits = {
            key.removeprefix("host:") for key in self.topology["limits"]
            if key.startswith("host:")
        }
        self.assertEqual(host_limits, set(self.topology["hosts"]))

    def test_copilot_is_not_a_controlled_fleet_host(self):
        self.assertNotIn("copilot", self.topology["hosts"])


if __name__ == "__main__":
    unittest.main()
