"""Offline tests of the review packet checker: complete packets, truncated ones, bad input."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "check-review-packet.py"
ROOT = Path(__file__).resolve().parents[2]

PATCH = """diff --git a/x b/x
index 1111111..2222222 100644
--- a/x
+++ b/x
@@ -1,2 +1,3 @@
 one
-two
+three
+four
diff --git a/y b/y
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/y
@@ -0,0 +1 @@
+hello
"""


def load_tool():
    """Import the checker as a module so its helpers can be exercised directly."""
    spec = importlib.util.spec_from_file_location("check_review_packet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReviewPacketTests(unittest.TestCase):
    """The checker proves that the review material is complete, not merely present."""

    def setUp(self):
        self.tool = load_tool()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name, text):
        """Write one file of the scratch packet and return its path."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def manifest(self, **overrides):
        """Return a manifest that names the files of the scratch packet."""
        manifest = {
            "schema": "phanes.review-packet/1",
            "pr": 1803,
            "previous_head": "0" * 40,
            "patch": ".lane-review/patch",
            "heads": ".lane-review/heads",
            "log": "docs/evidence/log.jsonl",
            "stats": "docs/evidence/stats.json",
            "files": [],
        }
        manifest.update(overrides)
        return manifest

    def entry(self, path):
        """Return the manifest entry of one file of the scratch packet."""
        return {"path": str(path.relative_to(self.root)),
                "bytes": path.stat().st_size,
                "sha256": self.tool.sha256_of(path)}

    def test_a_complete_patch_is_parsed_into_its_blocks(self):
        blocks = self.tool.parse_patch(PATCH)
        self.assertEqual(len(blocks), 2)
        self.assertEqual([len(block["hunks"]) for block in blocks], [1, 1])
        self.assertEqual(blocks[0]["hunks"][0]["old"], 2)
        self.assertEqual(blocks[0]["hunks"][0]["new"], 3)

    def test_a_truncated_patch_is_reported_instead_of_read_as_a_smaller_diff(self):
        with self.assertRaises(ValueError) as caught:
            self.tool.parse_patch(PATCH[:PATCH.index("+four\n")])
        self.assertIn("truncated", str(caught.exception))

    def test_a_patch_with_more_lines_than_its_header_declares_is_rejected(self):
        broken = PATCH.replace("@@ -1,2 +1,3 @@", "@@ -1,1 +1,1 @@")
        with self.assertRaises(ValueError):
            self.tool.parse_patch(broken)

    def test_a_manifest_that_names_a_missing_file_is_reported(self):
        self.write(".lane-review/patch", PATCH)
        manifest = self.manifest(files=[{"path": "gone.txt", "bytes": 3, "sha256": "0" * 64}])
        ok, detail = self.tool.check_manifest(self.root, manifest)
        self.assertFalse(ok)
        self.assertIn("gone.txt is missing", detail)

    def test_a_manifest_with_a_wrong_hash_or_size_is_reported(self):
        path = self.write("a.txt", "hello\n")
        entry = self.entry(path)
        ok, detail = self.tool.check_manifest(self.root, self.manifest(files=[entry]))
        self.assertTrue(ok, detail)
        entry["sha256"] = "0" * 64
        ok, detail = self.tool.check_manifest(self.root, self.manifest(files=[entry]))
        self.assertFalse(ok)
        self.assertIn("sha256", detail)
        entry["sha256"] = self.tool.sha256_of(path)
        entry["bytes"] = 99
        ok, detail = self.tool.check_manifest(self.root, self.manifest(files=[entry]))
        self.assertFalse(ok)
        self.assertIn("bytes", detail)

    def test_a_verdict_row_without_its_shadow_report_is_reported(self):
        self.write("docs/evidence/log.jsonl", json.dumps({
            "schema": "phanes.review-divergence/1", "kind": "verdict", "pr": 1751,
            "head": "a" * 40, "round": 1, "lane_verdict": "CHANGES", "tester_verdict": "FAIL",
            "recorded_at": "2026-11-02T00:00:00Z", "evidence": "docs/evidence/shadow/r1.json"}) + "\n")
        ok, detail = self.tool.check_evidence(self.root, self.manifest())
        self.assertFalse(ok)
        self.assertIn("missing report", detail)

    def test_a_shadow_report_that_disagrees_with_the_row_is_reported(self):
        self.write("docs/evidence/log.jsonl", json.dumps({
            "schema": "phanes.review-divergence/1", "kind": "verdict", "pr": 1751,
            "head": "a" * 40, "round": 1, "lane_verdict": "CHANGES", "tester_verdict": "FAIL",
            "recorded_at": "2026-11-02T00:00:00Z", "evidence": "docs/evidence/shadow/r1.json"}) + "\n")
        self.write("docs/evidence/shadow/r1.json", json.dumps({
            "schema": "phanes.review-shadow-tester/1", "pr": 1751, "head": "a" * 40,
            "verdict": "PASS", "recorded_at": "2026-11-02T00:00:00Z"}) + "\n")
        ok, detail = self.tool.check_evidence(self.root, self.manifest())
        self.assertFalse(ok)
        self.assertIn("verdict PASS", detail)

    def test_a_shadow_report_that_matches_the_row_is_accepted(self):
        self.write("docs/evidence/log.jsonl", json.dumps({
            "schema": "phanes.review-divergence/1", "kind": "verdict", "pr": 1751,
            "head": "a" * 40, "round": 1, "lane_verdict": "CHANGES", "tester_verdict": "FAIL",
            "recorded_at": "2026-11-02T00:00:00Z", "evidence": "docs/evidence/shadow/r1.json"}) + "\n")
        self.write("docs/evidence/shadow/r1.json", json.dumps({
            "schema": "phanes.review-shadow-tester/1", "pr": 1751, "head": "a" * 40,
            "verdict": "FAIL", "recorded_at": "2026-11-02T00:00:00Z"}) + "\n")
        ok, detail = self.tool.check_evidence(self.root, self.manifest())
        self.assertTrue(ok, detail)

    def test_the_packet_of_this_repository_passes_every_check(self):
        packet = ROOT / ".lane-review" / "PR-1803-packet.json"
        if not packet.is_file():
            self.skipTest("the packet of this repository is not committed")
        manifest = json.loads(packet.read_text(encoding="utf-8"))
        results = {row["check"]: row for row in self.tool.check_packet(ROOT, manifest)}
        for name in ("manifest", "patch", "evidence", "statistics"):
            self.assertTrue(results[name]["ok"], "%s: %s" % (name, results[name]["detail"]))

    def test_the_built_in_self_test_passes(self):
        self.assertEqual(self.tool.main(["--self-test"]), 0)


if __name__ == "__main__":
    unittest.main()
