"""The documented fleet topology must be the 2026-11-02 baseline (Refs #1480).

Issue #1480 rewrites `docs/fleet-topology-expected.md` to the 2026-11-02
baseline: the retired Instabox hosts are gone and the remaining remote hosts
carry at least ten lanes of the current fleet. This module pins the expected
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
    "gores1",
    "gores2",
    "govpn",
    "govpn2",
    "gorelay",
    "gocc",
)

# Reject any ib+digits substring in hosts and limits, including suffixed keys,
# as well as the retired node aliases this tree uses.
RETIRED_HOST = re.compile(r"ib\d+|node-[1-5]\.example\.invalid")


class FleetTopologyExpectedTests(unittest.TestCase):
    def setUp(self):
        self.text = EXPECTED.read_text(encoding="utf-8")
        self.topology = fenced_json(self.text)

    def test_no_retired_instabox_hosts_or_limits(self):
        self.assertFalse(any(RETIRED_HOST.search(host) for host in self.topology["hosts"]))
        self.assertFalse(any(
            RETIRED_HOST.search(key)
            for key in self.topology["limits"]
        ))

    def test_baseline_hosts_are_present(self):
        for host in BASELINE_HOSTS:
            self.assertIn(host, self.topology["hosts"])

    def test_guard_rejects_retired_hosts_and_all_limit_key_forms(self):
        for host in ("ib0", "ib6", "ib123", "ib6_worker", "worker_ib123", "prefixib42suffix"):
            with self.subTest(host=host):
                self.topology["hosts"].append(host)
                with self.assertRaises(AssertionError):
                    self.test_no_retired_instabox_hosts_or_limits()
                self.topology["hosts"].pop()
            for key in (host, f"host:{host}", f"backend:{host}"):
                with self.subTest(key=key):
                    self.topology["limits"][key] = 1
                    with self.assertRaises(AssertionError):
                        self.test_no_retired_instabox_hosts_or_limits()
                    del self.topology["limits"][key]

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
