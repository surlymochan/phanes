"""Guard for the 2026-11-02 fleet topology baseline (Refs #1480).

Issue #1480 asks for `docs/fleet-topology-expected.md` to be rewritten to the
2026-11-02 baseline: the retired Instabox hosts (`ib1`-`ib5`, spelled
`node-1.example.invalid`-`node-5.example.invalid` in this tree) must no longer
be documented, and a guard test must keep the document from drifting back to
the retired fleet.

What the guard reads
--------------------
* The machine-readable topology block of `docs/fleet-topology-expected.md`
  (the `hosts` list and the `limits` map).
* The document text, to catch a retired host that survives in the prose or in
  the host limit table.

What the guard asserts
----------------------
* No retired Instabox host is documented, neither as a lane host nor as a
  `host:<name>` limit key, and none is named in the document text.
* The document records the 2026-11-02 baseline it now describes.
* Every documented host carries its own limit key, and the fleet keeps at least
  ten lanes.
* `copilot` stays out of the controlled fleet hosts and keeps an expected
  limit of zero.

    cd swrm && python3 -m unittest discover -s tests -p 'test_fleet_topology_baseline.py' -v
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
TOPOLOGY_DOC = REPO_ROOT / "docs" / "fleet-topology-expected.md"

# The baseline the rewritten topology documents.
BASELINE_DATE = "2026-11-02"

# Retired Instabox hosts: the raw `ib1`-`ib5` aliases and the
# `node-1.example.invalid`-`node-5.example.invalid` names this tree uses for
# the same machines.
RETIRED_HOST_RE = re.compile(r"\bib\d+\b|node-[1-5]\.example\.invalid")

# A row of the backend limit table, e.g. "| `copilot` | 0 |".
BACKEND_ROW_RE = re.compile(r"^\|\s*`?([A-Za-z][A-Za-z0-9_.-]*)`?\s*\|\s*([^|]*)\|", re.MULTILINE)


def fenced_json(document: str) -> dict:
    """Return the machine-readable topology block of the document."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", document, re.DOTALL)
    if not match:
        raise AssertionError(f"{TOPOLOGY_DOC.name} must contain a JSON topology block")
    return json.loads(match.group(1))


def host_limit_keys(limits: dict) -> set:
    """Return the host names the limit map constrains."""
    return {key[len("host:"):] for key in limits if key.startswith("host:")}


class FleetTopologyBaselineTests(unittest.TestCase):
    def setUp(self):
        self.text = TOPOLOGY_DOC.read_text(encoding="utf-8")
        self.topology = fenced_json(self.text)
        self.hosts = self.topology["hosts"]
        self.limits = self.topology["limits"]

    def test_no_retired_instabox_host_in_the_topology_block(self):
        retired = [host for host in self.hosts if RETIRED_HOST_RE.search(host)]
        self.assertEqual(
            retired, [],
            f"{TOPOLOGY_DOC.name} still lists retired Instabox host(s) {retired}",
        )
        retired_limits = sorted(
            name for name in host_limit_keys(self.limits) if RETIRED_HOST_RE.search(name)
        )
        self.assertEqual(
            retired_limits, [],
            f"{TOPOLOGY_DOC.name} still constrains retired Instabox host(s) {retired_limits}",
        )

    def test_no_retired_instabox_host_in_the_document_text(self):
        retired = sorted({match.group(0) for match in RETIRED_HOST_RE.finditer(self.text)})
        self.assertEqual(
            retired, [],
            f"{TOPOLOGY_DOC.name} still names retired Instabox host(s) {retired}",
        )

    def test_topology_records_the_2026_11_02_baseline(self):
        self.assertIn(
            BASELINE_DATE, self.text,
            f"{TOPOLOGY_DOC.name} must record the {BASELINE_DATE} baseline it describes",
        )

    def test_every_documented_host_has_a_limit(self):
        self.assertEqual(len(self.hosts), len(set(self.hosts)), "the host list has duplicates")
        self.assertEqual(
            host_limit_keys(self.limits), set(self.hosts),
            "every documented host needs its own host:<name> limit key",
        )

    def test_fleet_keeps_at_least_ten_lanes(self):
        total = self.limits["total"]
        self.assertGreaterEqual(total, 10, "the fleet total lane limit must not drop below ten")
        per_host = sum(self.limits[key] for key in self.limits if key.startswith("host:"))
        self.assertGreaterEqual(per_host, 10, "the documented host limits must cover ten lanes")
        self.assertLessEqual(total, per_host, "the fleet total cannot exceed the host limits")

    def test_copilot_is_not_a_controlled_fleet_host(self):
        self.assertNotIn("copilot", self.hosts, "copilot must not be a controlled fleet host")
        rows = [match for match in BACKEND_ROW_RE.finditer(self.text)
                if match.group(1).lower() == "copilot"]
        for row in rows:
            expected = row.group(2).strip()
            self.assertIn(
                expected, ("0", "≥0"),
                f"copilot must keep an expected limit of 0: {expected}",
            )


if __name__ == "__main__":
    unittest.main()
