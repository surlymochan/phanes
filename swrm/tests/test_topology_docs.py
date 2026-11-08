"""Consistency between the documented fleet topology and the dual-mode runbook.

`docs/fleet-topology-expected.md` holds the machine-readable topology (hosts
and their lane limits); `docs/dual-mode-ssh.md` holds the deployment snippet
the box actually exports. Both must describe the same fleet, otherwise the
dispatcher and the control plane drift apart (Refs #1480).

    cd swrm && python -m unittest discover -s tests -p 'test_topology_docs.py' -v
"""
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXPECTED = ROOT / "docs" / "fleet-topology-expected.md"
DUAL_MODE = ROOT / "docs" / "dual-mode-ssh.md"
ENV_EXAMPLE = ROOT / "docs" / "examples" / "hub-bridge.env.example"


def fenced_json(document):
    match = re.search(r"```json\s*(\{.*?\})\s*```", document, re.DOTALL)
    if not match:
        raise AssertionError("fleet-topology-expected.md must contain a JSON topology block")
    return json.loads(match.group(1))


def exported_json(document, variable):
    match = re.search(rf"^(?:export )?{variable}='([^']+)'", document, re.MULTILINE)
    if not match:
        raise AssertionError(f"dual-mode-ssh.md is missing {variable}")
    return json.loads(match.group(1))


def exported_value(document, variable):
    match = re.search(rf"export {variable}=(\S+)", document)
    if not match:
        raise AssertionError(f"dual-mode-ssh.md is missing {variable}")
    return match.group(1)


class FleetTopologyDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.expected_text = EXPECTED.read_text(encoding="utf-8")
        self.dual_mode_text = DUAL_MODE.read_text(encoding="utf-8")
        self.topology = fenced_json(self.expected_text)

    def test_dual_mode_lane_hosts_match_expected_topology(self):
        expected_hosts = self.topology["hosts"]
        self.assertIsInstance(expected_hosts, list)
        self.assertEqual(
            exported_json(self.dual_mode_text, "OPENSWARM_AUTO_FLEET_HOSTS"),
            expected_hosts,
        )

    def test_each_expected_host_has_a_host_limit(self):
        limits = self.topology["limits"]
        self.assertIsInstance(limits, dict)
        host_limit_keys = {
            key.removeprefix("host:") for key in limits if key.startswith("host:")
        }
        self.assertEqual(host_limit_keys, set(self.topology["hosts"]))

    def test_dual_mode_backend_keys_match_expected_hosts(self):
        backends = exported_json(self.dual_mode_text, "OPENSWARM_AUTO_HOST_BACKENDS")
        local_host = exported_value(self.dual_mode_text, "OPENSWARM_AUTO_LOCAL_HOST")
        self.assertEqual(set(backends) - {local_host}, set(self.topology["hosts"]))

    def test_ssh_configuration_covers_every_fleet_host(self):
        hosts = re.search(r"^Host (.+)$", self.dual_mode_text, re.MULTILINE)
        self.assertIsNotNone(hosts)
        self.assertEqual(set(hosts.group(1).split()), set(self.topology["hosts"]))

    def test_environment_example_matches_runbook(self):
        environment = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("OPENSWARM_HUB_LOCAL_ROUTER=1", environment)
        self.assertIn("export OPENSWARM_HUB_LOCAL_ROUTER=1", self.dual_mode_text)
        for variable in ("OPENSWARM_AUTO_FLEET_HOSTS", "OPENSWARM_AUTO_HOST_BACKENDS"):
            self.assertEqual(
                exported_json(environment, variable),
                exported_json(self.dual_mode_text, variable),
            )

    def test_backend_capacity_and_control_plane_exclusion(self):
        backends = exported_json(self.dual_mode_text, "OPENSWARM_AUTO_HOST_BACKENDS")
        self.assertEqual(backends["box"], [])
        self.assertNotIn("box", self.topology["hosts"])
        capacity = {"pi": 0, "codex": 0, "agy": 0}
        for host in self.topology["hosts"]:
            self.assertEqual(len(backends[host]), 1)
            capacity[backends[host][0]] += self.topology["limits"][f"host:{host}"]
        self.assertGreaterEqual(capacity["codex"], 4)
        self.assertEqual(capacity["agy"], 2)
        self.assertEqual(sum(capacity.values()), self.topology["limits"]["total"])


if __name__ == "__main__":
    unittest.main()
