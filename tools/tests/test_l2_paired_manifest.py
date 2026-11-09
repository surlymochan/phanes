"""Acceptance tests for the reproducible L2 paired-experiment manifest.

These tests deliberately describe the public, file-based CLI before its
implementation.  The manifest contains only holdout digests; holdout content
must never be accepted or emitted.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "l2_paired_manifest.py"


class PairedManifestTests(unittest.TestCase):
    maxDiff = None

    def pair(self, number, *, card_id=None, digest=None, arms=("current", "spec-build")):
        card_id = card_id or f"L2-{2000 + number}"
        digest = digest or f"sha256:{number:064x}"
        return {
            "pair_id": f"pair-{number:02d}",
            "runs": [
                {
                    "run_id": f"fleet-{number:02d}-a",
                    "card_id": card_id,
                    "base": "bbcc27784816032c8ddd1578eeef7c5a8ddd86f4",
                    "arm": arms[0],
                    "holdout_suite_digest": digest,
                },
                {
                    "run_id": f"fleet-{number:02d}-b",
                    "card_id": card_id,
                    "base": "bbcc27784816032c8ddd1578eeef7c5a8ddd86f4",
                    "arm": arms[1],
                    "holdout_suite_digest": digest,
                },
            ],
        }

    def run_tool(self, pairs, *, seed=1979):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            tmp = Path(tmp)
            source = tmp / "input.json"
            output = tmp / "manifest.json"
            source.write_text(json.dumps({"pairs": pairs}), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOL),
                    "--input",
                    str(source),
                    "--output",
                    str(output),
                    "--seed",
                    str(seed),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            rendered = output.read_bytes() if output.exists() else None
            return result, rendered

    def test_fixed_seed_is_byte_reproducible_and_order_balanced(self):
        pairs = [self.pair(number) for number in range(10)]

        first, first_bytes = self.run_tool(pairs)
        second, second_bytes = self.run_tool(list(reversed(pairs)))

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(first_bytes, second_bytes)
        manifest = json.loads(first_bytes)
        self.assertEqual(10, len(manifest["pairs"]))
        first_arm_counts = {"current": 0, "spec-build": 0}
        for pair in manifest["pairs"]:
            first_arm_counts[pair["runs"][0]["arm"]] += 1
        self.assertLessEqual(
            abs(first_arm_counts["current"] - first_arm_counts["spec-build"]), 1
        )
        self.assertNotIn("holdout_content", first_bytes.decode("utf-8"))

    def assert_invalid(self, pairs):
        result, rendered = self.run_tool(pairs)
        self.assertNotEqual(0, result.returncode)
        self.assertIsNone(rendered, "invalid input must not leave a manifest")

    def test_duplicate_card_is_rejected(self):
        pairs = [self.pair(number) for number in range(10)]
        duplicate = pairs[0]["runs"][0]["card_id"]
        for run in pairs[1]["runs"]:
            run["card_id"] = duplicate
        self.assert_invalid(pairs)

    def test_pair_with_different_cards_is_rejected(self):
        pairs = [self.pair(number) for number in range(10)]
        pairs[0]["runs"][1]["card_id"] = "L2-other"
        self.assert_invalid(pairs)

    def test_pair_with_different_frozen_bases_is_rejected(self):
        pairs = [self.pair(number) for number in range(10)]
        pairs[0]["runs"][1]["base"] = "a" * 40
        self.assert_invalid(pairs)

    def test_pair_missing_one_arm_is_rejected(self):
        pairs = [self.pair(number) for number in range(10)]
        pairs[0]["runs"] = pairs[0]["runs"][:1]
        self.assert_invalid(pairs)

    def test_pair_with_repeated_arm_is_rejected(self):
        pairs = [self.pair(number) for number in range(10)]
        pairs[0]["runs"][1]["arm"] = pairs[0]["runs"][0]["arm"]
        self.assert_invalid(pairs)

    def test_pair_with_different_holdout_digests_is_rejected(self):
        pairs = [self.pair(number) for number in range(10)]
        pairs[0]["runs"][1]["holdout_suite_digest"] = "sha256:" + "f" * 64
        self.assert_invalid(pairs)

    def test_duplicate_run_id_is_rejected(self):
        pairs = [self.pair(number) for number in range(10)]
        pairs[1]["runs"][0]["run_id"] = pairs[0]["runs"][0]["run_id"]
        self.assert_invalid(pairs)

    def test_manifest_requires_ten_to_twenty_pairs(self):
        self.assert_invalid([self.pair(number) for number in range(9)])
        self.assert_invalid([self.pair(number) for number in range(21)])

    def test_holdout_content_is_rejected(self):
        pairs = [self.pair(number) for number in range(10)]
        pairs[0]["runs"][0]["holdout_content"] = "hidden assertion"
        self.assert_invalid(pairs)


if __name__ == "__main__":
    unittest.main()
