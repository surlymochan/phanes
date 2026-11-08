"""The stray build artifacts of the lane tooling must never be tracked (Refs #1738).

The Zig build of the lane tooling (uucode) drops a hidden shared object into the
test directory it runs in, and `git add -A` used to sweep it into the change.
The binaries are identical to each other and unrelated to any feature, so they
must never be tracked, and the directory that hosts the build must ignore them.

The old openswarm/ copy is deprecated (Refs #1894), but the artifacts it produced
are still tracked in the baseline.  The guards below reproduce that gap and keep
the migrated path (swrm/tests/, covered by the root .gitignore) guarded.
"""
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]

# A stray artifact is a hidden shared object: the same shape the harvest filter
# of the lane tooling keeps out of a PR (tools/lane-autodispatch/autodispatch.py).
STRAY = re.compile(r"(^|/)\.[^/]*\.so$")

# The test directory that hosts the build of the lane tooling today, and the one
# of the deprecated openswarm/ copy (Refs #1894).
CURRENT_SITE = "swrm/tests"
LEGACY_SITE = "openswarm/tests"


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


class StrayBuildArtifactTests(unittest.TestCase):
    def setUp(self):
        inside = git("rev-parse", "--is-inside-work-tree")
        if inside.returncode or inside.stdout.strip() != "true":
            self.skipTest("the checkout is not a git work tree")

    def tracked(self):
        listing = git("ls-files", "-z")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        return [path for path in listing.stdout.split("\0") if path]

    def ignored(self, path):
        """Whether an ignore rule of the repository covers the path.

        --no-index asks the rules alone: the question is "would `git add -A` sweep
        this file into the change?", which a tracked file does not answer."""
        return git("check-ignore", "-q", "--no-index", "--", path).returncode == 0

    def on_disk(self):
        """The stray artifacts that lie in the working tree."""
        found = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [name for name in dirnames if name not in (".git", "node_modules")]
            for name in filenames:
                relative = (Path(dirpath) / name).relative_to(ROOT).as_posix()
                if STRAY.search(relative):
                    found.append(relative)
        return sorted(found)

    def test_no_stray_build_artifact_is_tracked(self):
        stray = [path for path in self.tracked() if STRAY.search(path)]
        self.assertEqual(stray, [], "stray build artifacts are tracked: " + ", ".join(stray))

    def test_the_stray_build_artifacts_of_the_working_tree_are_ignored(self):
        stray = self.on_disk()
        not_ignored = [path for path in stray if not self.ignored(path)]
        self.assertEqual(not_ignored, [],
                         "the stray build artifacts are not ignored, `git add -A` sweeps "
                         "them into the change: " + ", ".join(not_ignored))

    def test_the_current_test_directory_ignores_the_stray_build_artifact(self):
        self.assertTrue(self.ignored(f"{CURRENT_SITE}/.dde4probe-00000000.so"),
                        f"the root .gitignore does not ignore the build artifacts of {CURRENT_SITE}")

    @unittest.skipUnless((ROOT / LEGACY_SITE).is_dir(), f"{LEGACY_SITE} is gone")
    def test_the_legacy_test_directory_ignores_the_stray_build_artifact(self):
        self.assertTrue(self.ignored(f"{LEGACY_SITE}/.dde4probe-00000000.so"),
                        f"the root .gitignore does not ignore the build artifacts of {LEGACY_SITE}")
