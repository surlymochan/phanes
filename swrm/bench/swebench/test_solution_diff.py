"""Storage of a run's solution diff: plain text, or gzipped when it exceeds the export cap."""

import gzip
import hashlib
import tempfile
import unittest
from pathlib import Path

import solution_diff as SD


class SolutionDiffTests(unittest.TestCase):
    def test_small_diff_is_stored_as_text_with_its_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp)
            stored = SD.store_solution_diff(events, "diff --git a/x b/x\n")

            self.assertEqual(stored["file"], "solution.diff")
            self.assertFalse(stored["compressed"])
            self.assertEqual(stored["sha256"], hashlib.sha256(b"diff --git a/x b/x\n").hexdigest())
            self.assertTrue((events / "solution.diff").is_file())
            self.assertFalse((events / "solution.diff.gz").exists())
            self.assertEqual(SD.read_solution_diff(events), "diff --git a/x b/x\n")

    def test_diff_over_the_export_cap_is_stored_compressed_with_a_sha256_of_the_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp)
            diff = "+" * (SD.EXPORT_CAP_BYTES + 1)
            stored = SD.store_solution_diff(events, diff)

            self.assertEqual(stored["file"], "solution.diff.gz")
            self.assertTrue(stored["compressed"])
            self.assertEqual(stored["bytes"], len(diff))
            self.assertEqual(stored["sha256"], hashlib.sha256(diff.encode()).hexdigest())
            self.assertLess(stored["stored_bytes"], stored["bytes"])
            self.assertFalse((events / "solution.diff").exists())
            self.assertEqual((events / "solution.diff.sha256").read_text().split()[0], stored["sha256"])
            self.assertEqual(SD.read_solution_diff(events), diff)

    def test_gzipped_diff_is_readable_by_the_regrade_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp)
            diff = "line\n" * (SD.EXPORT_CAP_BYTES // 5 + 1)
            SD.store_solution_diff(events, diff)

            path = SD.solution_diff_path(events)

            self.assertEqual(path.name, "solution.diff.gz")
            self.assertEqual(gzip.decompress(path.read_bytes()).decode(), diff)

    def test_missing_diff_reads_as_empty_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(SD.solution_diff_path(Path(tmp)))
            self.assertEqual(SD.read_solution_diff(Path(tmp)), "")


if __name__ == "__main__":
    unittest.main()
