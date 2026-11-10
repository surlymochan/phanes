"""The ci-gate job of .github/workflows/phanes.yml must cover every check job.

The workflow is read as text so that the test needs nothing beyond the standard
library (the Swrm CI job installs no third-party packages).
"""
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).parents[2] / ".github/workflows/phanes.yml"
EXPECTED_JOBS = {
    "forbidden-paths",
    "changes",
    "example-json",
    "upstream-drift",
    "swrm",
    "volto",
    "volto-scenario-tests",
}


def job_blocks():
    """Return {job name: text of its block} for the top-level jobs."""
    text = WORKFLOW.read_text()
    jobs = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):\n", jobs, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def gate_needs(block):
    match = re.search(r"^    needs:\n((?:      - .+\n)+)", block, flags=re.M)
    assert match, block
    return {line.strip()[2:] for line in match.group(1).splitlines()}


def gate_script(block):
    match = re.search(r"python3 - <<'PY'\n(.*?)^\s*PY\n", block, flags=re.M | re.S)
    assert match, block
    lines = match.group(1).splitlines()
    indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "python3 - <<'PY'\n" + "\n".join(line[indent:] for line in lines) + "\nPY\n"


class CiGateWorkflowTest(unittest.TestCase):
    def test_ci_gate_covers_every_check_job(self):
        jobs = job_blocks()
        self.assertIn("ci-gate", jobs)
        gate = jobs["ci-gate"]
        self.assertIn("    name: ci-gate\n", gate)
        self.assertEqual(gate_needs(gate), set(jobs) - {"ci-gate"})
        self.assertEqual(gate_needs(gate), EXPECTED_JOBS)
        self.assertIn("    if: always()\n", gate)
        self.assertIn("    timeout-minutes: 5\n", gate)
        self.assertIn("    permissions: {}\n", gate)
        self.assertIn("NEEDS_JSON: ${{ toJSON(needs) }}", gate)

    def test_ci_gate_result_policy(self):
        script = gate_script(job_blocks()["ci-gate"])
        for statuses, expected in [
            ({}, 0),
            ({"forbidden-paths": "skipped"}, 0),
            ({"example-json": "failure"}, 1),
            ({"volto": "cancelled"}, 1),
        ]:
            needs = {job: {"result": statuses.get(job, "success")} for job in EXPECTED_JOBS}
            result = subprocess.run(
                ["bash", "-c", script],
                env={**os.environ, "NEEDS_JSON": json.dumps(needs)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, expected, (statuses, result.stdout, result.stderr))


if __name__ == "__main__":
    unittest.main()
