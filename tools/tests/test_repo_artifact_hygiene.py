"""Tracked repository artifacts must not include lane reviews or shared objects (Refs #1863)."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]


class TrackedArtifactHygieneTests(unittest.TestCase):
    def test_tracked_tree_has_no_lane_reviews_or_shared_objects(self):
        tracked = subprocess.check_output(
            ["git", "-C", str(ROOT), "ls-files"],
            text=True,
        ).splitlines()
        artifacts = [
            path for path in tracked
            if path.startswith(".lane-review/") or path.endswith(".so")
        ]

        self.assertEqual(artifacts, [], "tracked review/build artifacts: " + ", ".join(artifacts))


if __name__ == "__main__":
    unittest.main()
