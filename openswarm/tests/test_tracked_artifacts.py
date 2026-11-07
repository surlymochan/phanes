"""The repo tracks no build leftovers (Refs #1738).

The test suite leaves hidden shared objects behind in ``openswarm/tests/`` (the
native library of an executor is unpacked into the working directory).  The
ignore rules keep a fresh leftover out of the commits and the harvest filter
drops the artifact kind from a lane branch, but a tracked leftover survives both:
the ignore rules do not apply to a tracked path.  The guard here makes sure that
no such artifact is tracked in the first place.
"""
import importlib.util
import os
import re
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # the repo root
_spec = importlib.util.spec_from_file_location("harvest_filter_under_test",
                                               os.path.join(os.path.dirname(HERE), "lib", "dispatch",
                                                            "harvest_filter.py"))
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

# The artifact kind: a hidden shared object (a build leftover of the test suite).
ARTIFACT = re.compile(r"(^|/)\.[^/]*\.so$")
SAMPLE = "openswarm/tests/.dde4xxx-00000000.so"


def git(*args):
    """The output of a git command in the repo (fails the test when it errors)."""
    proc = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)
    if proc.returncode:
        raise AssertionError("git %s failed: %s" % (args[0], proc.stderr.strip()))
    return proc.stdout


def ignored(path):
    """Whether the ignore rules of the repo cover `path`."""
    proc = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "--no-index", path])
    return proc.returncode == 0


class TrackedArtifactTest(unittest.TestCase):
    def setUp(self):
        if not os.path.isdir(os.path.join(ROOT, ".git")) and not os.path.isfile(os.path.join(ROOT, ".git")):
            self.skipTest("the repo is not a git checkout")
        self.tracked = [path for path in git("ls-files", "-z").split("\0") if path]

    def test_no_build_leftover_is_tracked(self):
        leftovers = sorted(path for path in self.tracked if ARTIFACT.search(path))
        self.assertEqual(leftovers, [], "a build leftover is tracked in the repo")

    def test_the_guard_and_the_harvest_filter_agree_on_the_artifact_kind(self):
        self.assertTrue(ARTIFACT.search(SAMPLE))
        self.assertIn(SAMPLE, H.excluded_paths([SAMPLE]))

    def test_the_ignore_rules_keep_a_new_leftover_untracked(self):
        self.assertTrue(ignored(SAMPLE), "the ignore rules must cover %s" % SAMPLE)

    def test_the_leftovers_on_disk_are_ignored(self):
        directory = os.path.join(ROOT, "openswarm", "tests")
        leftovers = [name for name in os.listdir(directory) if ARTIFACT.search(name)]
        for name in leftovers:
            path = os.path.join("openswarm", "tests", name)
            self.assertTrue(ignored(path), "%s must stay untracked" % path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
