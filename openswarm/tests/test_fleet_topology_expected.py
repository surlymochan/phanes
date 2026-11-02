import re
import unittest

from test_topology_docs import EXPECTED, fenced_json


RETIRED_HOST = re.compile(r"ib\d+")


class FleetTopologyExpectedTests(unittest.TestCase):
    def setUp(self):
        self.topology = fenced_json(EXPECTED.read_text())

    def test_no_retired_instabox_hosts_or_limits(self):
        self.assertFalse(any(RETIRED_HOST.fullmatch(host) for host in self.topology["hosts"]))
        self.assertFalse(any(
            RETIRED_HOST.fullmatch(key.removeprefix("host:"))
            for key in self.topology["limits"]
            if key.startswith("host:")
        ))

    def test_gores_hosts_are_present(self):
        self.assertIn("gores1", self.topology["hosts"])
        self.assertIn("gores2", self.topology["hosts"])

    def test_total_capacity_is_at_least_ten(self):
        self.assertGreaterEqual(self.topology["limits"]["total"], 10)

    def test_each_host_has_a_limit(self):
        host_limits = {
            key.removeprefix("host:") for key in self.topology["limits"]
            if key.startswith("host:")
        }
        self.assertEqual(host_limits, set(self.topology["hosts"]))


if __name__ == "__main__":
    unittest.main()
