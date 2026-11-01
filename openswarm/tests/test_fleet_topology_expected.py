"""Guard the documented fleet topology (Refs #1480).

The 2026-11-02 control plane retired the Instabox nodes, so the expected topology
must never name them again, must carry the two ``gores`` hosts, must keep at least
ten lanes addressable, and must give every documented host its own limit key.
"""

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXPECTED = ROOT / "docs" / "fleet-topology-expected.md"
# The retired Instabox hosts; none of them may reappear in the expected topology.
RETIRED_HOST = re.compile(r"ib[1-5]")
MIN_TOTAL = 10


def fenced_json(document):
    """Return the machine-readable topology block of the expected-topology document."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", document, re.DOTALL)
    if not match:
        raise AssertionError("fleet-topology-expected.md must contain a JSON topology block")
    return json.loads(match.group(1))


def backend_limits(document):
    """Return the backend limits of the document's backend table as {backend: limit}."""
    sections = document.split("## Backend 限额", 1)
    if len(sections) != 2:
        raise AssertionError("fleet-topology-expected.md must contain a backend limits section")
    limits = {}
    for line in sections[1].splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= {"-"} or set(cells[1]) <= {"-"}:
            continue
        name = re.search(r"`([a-z][a-z0-9-]*)`", cells[0])
        if not name:
            continue  # the table header row
        limits[name.group(1)] = int(cells[1])
    return limits


class FleetTopologyExpectedTests(unittest.TestCase):
    def setUp(self):
        self.text = EXPECTED.read_text(encoding="utf-8")
        self.topology = fenced_json(self.text)
        self.hosts = self.topology["hosts"]
        self.limits = self.topology["limits"]

    def test_no_retired_instabox_host_is_expected(self):
        self.assertIsInstance(self.hosts, list)
        self.assertTrue(self.hosts)
        for host in self.hosts:
            self.assertIsNone(RETIRED_HOST.search(host), f"retired host in topology: {host}")
        for key in self.limits:
            self.assertIsNone(RETIRED_HOST.search(key), f"retired limit key: {key}")

    def test_the_document_does_not_name_retired_hosts(self):
        self.assertIsNone(
            RETIRED_HOST.search(self.text),
            "fleet-topology-expected.md must not mention the retired Instabox hosts",
        )

    def test_gores_hosts_are_expected(self):
        self.assertIn("gores1", self.hosts)
        self.assertIn("gores2", self.hosts)

    def test_total_capacity_is_at_least_ten(self):
        self.assertIsInstance(self.limits, dict)
        self.assertGreaterEqual(self.limits["total"], MIN_TOTAL)

    def test_every_host_has_its_own_host_limit(self):
        host_limits = {
            key[len("host:"):] for key in self.limits if key.startswith("host:")
        }
        self.assertEqual(host_limits, set(self.hosts))
        for host in self.hosts:
            self.assertGreater(self.limits[f"host:{host}"], 0, host)

    def test_host_limits_add_up_to_the_total(self):
        host_limits = [
            value for key, value in self.limits.items() if key.startswith("host:")
        ]
        self.assertEqual(sum(host_limits), self.limits["total"])

    def test_backend_table_limits_pi_codex_agy_and_a_remote_only_copilot(self):
        limits = backend_limits(self.text)
        self.assertEqual(set(limits), {"pi", "codex", "agy", "copilot"})
        self.assertEqual(limits["copilot"], 0)
        self.assertIn("远程", self.text.split("## Backend 限额", 1)[1])


if __name__ == "__main__":
    unittest.main()
