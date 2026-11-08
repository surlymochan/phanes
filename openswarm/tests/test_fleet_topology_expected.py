"""Guard for the live fleet topology document (Refs #1480).

The control plane topology changed on 2026-11-02: the Instabox hosts are gone
and the controlled fleet is `gores1`, `gores2`, `gocc`, `gorelay`, `govpn` and
`govpn2`. This test pins the machine-readable part of
`docs/fleet-topology-expected.md` and keeps every live deployment example in
sync with it, so a retired host or a diverging backend allowlist cannot come
back through a documentation-only change.
"""

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXPECTED = ROOT / "docs" / "fleet-topology-expected.md"
# Directories that may hold a live deployment example of the routing config.
DEPLOYMENT_DIRS = ("docs", "integrations", "examples")
# The deployment examples this topology has to agree with.
DEPLOYMENT_DOCS = (
    ROOT / "docs" / "dual-mode-ssh.md",
    ROOT / "docs" / "examples" / "hub-bridge.env.example",
    ROOT / "integrations" / "README.md",
)

RETIRED_HOST = re.compile(r"ib\d+")
LOCAL_HOST = re.compile(r"^(?:export )?OPENSWARM_AUTO_LOCAL_HOST=(\S+)$", re.MULTILINE)
FLEET_HOSTS = re.compile(r"^(?:export )?OPENSWARM_AUTO_FLEET_HOSTS='([^']*)'$", re.MULTILINE)
HOST_BACKENDS = re.compile(r"^(?:export )?OPENSWARM_AUTO_HOST_BACKENDS='([^']*)'$", re.MULTILINE)


def fenced_json(document):
    match = re.search(r"```json\s*(\{.*?\})\s*```", document, re.DOTALL)
    if not match:
        raise AssertionError("fleet-topology-expected.md must contain a JSON topology block")
    return json.loads(match.group(1))


def deployment_documents():
    """Return every live document that declares the automatic routing config."""
    paths = list(DEPLOYMENT_DOCS)
    for directory in DEPLOYMENT_DIRS:
        for path in sorted((ROOT / directory).rglob("*")):
            if path.suffix not in (".md", ".example") or path in paths:
                continue
            paths.append(path)
    documents = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if FLEET_HOSTS.search(text) or HOST_BACKENDS.search(text):
            documents.append((path, text))
    return documents


class FleetTopologyExpectedTests(unittest.TestCase):
    def setUp(self):
        self.text = EXPECTED.read_text(encoding="utf-8")
        self.topology = fenced_json(self.text)

    def test_no_retired_instabox_host_is_documented(self):
        self.assertIsNone(RETIRED_HOST.search(self.text),
                          "the topology document must not mention a retired Instabox host")

    def test_no_retired_instabox_host_or_limit_key(self):
        for host in self.topology["hosts"]:
            self.assertIsNone(RETIRED_HOST.fullmatch(host),
                              f"retired host {host!r} must not be a fleet host")
        for key in self.topology["limits"]:
            self.assertIsNone(RETIRED_HOST.fullmatch(key.removeprefix("host:")),
                              f"retired limit key {key!r} must not be documented")

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

    def test_documented_host_limits_match_the_host_set(self):
        limits = self.topology["limits"]
        for host in self.topology["hosts"]:
            self.assertGreaterEqual(limits[f"host:{host}"], 1)

    def test_every_deployment_example_uses_the_documented_topology(self):
        documents = deployment_documents()
        self.assertTrue(documents, "no deployment document declares the fleet configuration")
        for path, text in documents:
            hosts = FLEET_HOSTS.search(text)
            backends = HOST_BACKENDS.search(text)
            self.assertIsNotNone(hosts, f"{path} must declare OPENSWARM_AUTO_FLEET_HOSTS")
            self.assertIsNotNone(backends, f"{path} must declare OPENSWARM_AUTO_HOST_BACKENDS")
            self.assertEqual(json.loads(hosts.group(1)), self.topology["hosts"],
                             f"{path} must use the documented fleet hosts")
            allowlist = json.loads(backends.group(1))
            local = LOCAL_HOST.search(text)
            self.assertIsNotNone(local, f"{path} must declare OPENSWARM_AUTO_LOCAL_HOST")
            self.assertEqual(set(allowlist) - {local.group(1)}, set(self.topology["hosts"]),
                             f"{path} must allowlist exactly the documented fleet hosts")
            self.assertEqual(allowlist[local.group(1)], [],
                             f"{path} must not dispatch to the local host")

    def test_deployment_examples_keep_the_backend_capacity(self):
        capacity = {}
        for path, text in deployment_documents():
            allowlist = json.loads(HOST_BACKENDS.search(text).group(1))
            for host, backends in allowlist.items():
                if host not in self.topology["hosts"]:
                    continue
                for backend in backends:
                    capacity[backend] = capacity.get(backend, 0) + self.topology["limits"][f"host:{host}"]
        self.assertGreaterEqual(capacity.get("pi", 0), 4)
        self.assertGreaterEqual(capacity.get("agy", 0), 2)
        self.assertGreaterEqual(capacity.get("codex", 0), 4)

    def test_gorelay_runs_codex(self):
        for path, text in deployment_documents():
            allowlist = json.loads(HOST_BACKENDS.search(text).group(1))
            if "gorelay" in allowlist:
                self.assertEqual(allowlist["gorelay"], ["codex"],
                                 f"{path} must keep the codex lane on gorelay")


if __name__ == "__main__":
    unittest.main()
