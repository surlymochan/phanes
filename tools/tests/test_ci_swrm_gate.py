"""Contract of the Swrm CI gate script and of its path based test selection (Refs #1867).

The gate must also select the files that are moved *out* of tools/lane-autodispatch/
or tools/tests/: git reports only the target path of a rename, so a path selection
based on the default `git diff --name-only` would silently skip those changes.
The tests run the real gate script and the real diff command of phanes.yml against a
scratch repository that contains such a rename.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "ci-swrm.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "phanes.yml"
SCRATCH = ROOT / ".swrm-tmp"  # gitignored scratch root


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def make_repo(scratch):
    """A scratch repository with the two watched tools/ directories."""
    repo = Path(scratch) / "repo"
    (repo / "tools" / "tests").mkdir(parents=True)
    (repo / "tools" / "lane-autodispatch").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "tools" / "tests" / "test_sample.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "tools" / "lane-autodispatch" / "test_sample.py").write_text("y = 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    base = git(repo, "rev-parse", "HEAD")
    # The gate script is copied in: it resolves the repository root from its own path.
    shutil.copy(SCRIPT, repo / "tools" / "ci-swrm.sh")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add gate script")
    return repo, base


def script_paths(repo, base):
    """Paths the gate script selects for the change set based on `base`."""
    out = subprocess.check_output(
        ["bash", str(repo / "tools" / "ci-swrm.sh"), "--print-changed-paths"],
        cwd=str(repo), env={**os.environ, "BASE_SHA": base}, text=True)
    return [line for line in out.splitlines() if line]


def workflow_diff_command():
    """The diff command phanes.yml uses to select the changed paths."""
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        if "git diff --name-only" in line:
            return line[line.index("git diff --name-only"):].rstrip(")").strip()
    raise AssertionError("phanes.yml does not select the changed paths any more")


def workflow_paths(repo, base):
    """Paths phanes.yml selects for the change set based on `base`."""
    out = subprocess.check_output(
        ["bash", "-c", workflow_diff_command()],
        cwd=str(repo), env={**os.environ, "BASE_SHA": base}, text=True)
    return [line for line in out.splitlines() if line]


def workflow_swrm_pattern():
    """The path pattern that makes phanes.yml run the Swrm job."""
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.endswith("swrm=true ;;"):
            return stripped.split(")")[0]
    raise AssertionError("phanes.yml does not trigger the Swrm job any more")


def workflow_triggers_swrm(path):
    """Whether phanes.yml runs the Swrm job for a change of `path`."""
    out = subprocess.check_output(
        ["bash", "-c",
         f'case "$1" in {workflow_swrm_pattern()}) echo yes ;; *) echo no ;; esac',
         "gate", path],
        text=True)
    return out.strip() == "yes"


class SwrmGatePathSelectionTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self._scratch = tempfile.TemporaryDirectory(dir=str(SCRATCH))
        self.addCleanup(self._scratch.cleanup)
        self.repo, self.base = make_repo(self._scratch.name)

    def move_out(self, path, target):
        (self.repo / target).parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "mv", path, target)

    def test_moved_out_of_tools_tests_is_selected(self):
        self.move_out("tools/tests/test_sample.py", "moved/test_sample.py")
        git(self.repo, "commit", "-qm", "move out of tools/tests")
        paths = script_paths(self.repo, self.base)
        self.assertIn("tools/tests/test_sample.py", paths)
        self.assertIn("moved/test_sample.py", paths)

    def test_moved_out_of_lane_autodispatch_is_selected(self):
        self.move_out("tools/lane-autodispatch/test_sample.py", "moved/test_sample.py")
        git(self.repo, "commit", "-qm", "move out of tools/lane-autodispatch")
        paths = script_paths(self.repo, self.base)
        self.assertIn("tools/lane-autodispatch/test_sample.py", paths)
        self.assertIn("moved/test_sample.py", paths)

    def test_uncommitted_move_out_is_selected(self):
        # A staged rename is not part of `base...HEAD` but of the working tree diff.
        self.move_out("tools/tests/test_sample.py", "moved/test_sample.py")
        paths = script_paths(self.repo, self.base)
        self.assertIn("tools/tests/test_sample.py", paths)

    def test_workflow_selects_moved_out_paths(self):
        self.move_out("tools/tests/test_sample.py", "moved/test_sample.py")
        git(self.repo, "commit", "-qm", "move out of tools/tests")
        paths = workflow_paths(self.repo, self.base)
        self.assertIn("tools/tests/test_sample.py", paths)
        self.assertIn("moved/test_sample.py", paths)

    def test_script_and_workflow_select_the_same_paths(self):
        self.move_out("tools/lane-autodispatch/test_sample.py", "moved/test_sample.py")
        git(self.repo, "commit", "-qm", "move out of tools/lane-autodispatch")
        self.assertEqual(sorted(script_paths(self.repo, self.base)),
                         sorted(workflow_paths(self.repo, self.base)))

    def test_workflow_runs_the_gate_for_moved_out_paths(self):
        # A change that only moves a file out of a watched directory must not
        # skip the Swrm job.
        self.assertTrue(workflow_triggers_swrm("tools/tests/test_sample.py"))
        self.assertTrue(workflow_triggers_swrm("tools/lane-autodispatch/test_sample.py"))
        self.assertFalse(workflow_triggers_swrm("openbot/app.ts"))

    def test_workflow_runs_the_gate_for_the_files_of_the_gate(self):
        # The gate script and its contract belong to the Swrm job.
        self.assertTrue(workflow_triggers_swrm("tools/ci-swrm.sh"))
        self.assertTrue(workflow_triggers_swrm("tools/ci-swrm.md"))


if __name__ == "__main__":
    unittest.main()
