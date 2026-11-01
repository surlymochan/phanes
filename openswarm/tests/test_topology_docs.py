import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXPECTED = ROOT / "docs" / "fleet-topology-expected.md"
DUAL_MODE = ROOT / "docs" / "dual-mode-ssh.md"


def fenced_json(document):
    match = re.search(r"```json\s*(\{.*?\})\s*```", document, re.DOTALL)
    if not match:
        raise AssertionError("fleet-topology-expected.md must contain a JSON topology block")
    return json.loads(match.group(1))


def exported_json(document, variable):
    match = re.search(rf"export {variable}='([^']+)'", document)
    if not match:
        raise AssertionError(f"dual-mode-ssh.md is missing {variable}")
    return json.loads(match.group(1))


class FleetTopologyDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.expected_text = EXPECTED.read_text()
        self.dual_mode_text = DUAL_MODE.read_text()
        self.topology = fenced_json(self.expected_text)

    def test_dual_mode_lane_hosts_match_expected_topology(self):
        expected_hosts = self.topology["hosts"]
        self.assertIsInstance(expected_hosts, list)
        # The control-plane host runs its lane locally; the SSH fleet is the rest.
        control_plane = self.topology.get("control_plane_host")
        self.assertIn(control_plane, expected_hosts)
        ssh_fleet_hosts = [host for host in expected_hosts if host != control_plane]
        self.assertEqual(
            exported_json(self.dual_mode_text, "OPENSWARM_AUTO_FLEET_HOSTS"),
            ssh_fleet_hosts,
        )

    def test_dual_mode_local_host_is_the_control_plane_host(self):
        control_plane = self.topology.get("control_plane_host")
        self.assertIsNotNone(control_plane)
        match = re.search(r"export OPENSWARM_AUTO_LOCAL_HOST='?([^'\s]+)'?", self.dual_mode_text)
        self.assertIsNotNone(match, "dual-mode-ssh.md must export OPENSWARM_AUTO_LOCAL_HOST")
        self.assertEqual(match.group(1), control_plane)

    def test_each_expected_host_has_a_host_limit(self):
        limits = self.topology["limits"]
        self.assertIsInstance(limits, dict)
        host_limit_keys = {
            key.removeprefix("host:") for key in limits if key.startswith("host:")
        }
        self.assertEqual(host_limit_keys, set(self.topology["hosts"]))

    def test_dual_mode_backend_keys_match_expected_hosts(self):
        backends = exported_json(self.dual_mode_text, "OPENSWARM_AUTO_HOST_BACKENDS")
        self.assertEqual(set(backends) - {"local"}, set(self.topology["hosts"]))


if __name__ == "__main__":
    unittest.main()
