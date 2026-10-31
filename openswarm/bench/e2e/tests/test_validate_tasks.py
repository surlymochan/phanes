"""validate_tasks.py unit tests. Synthetic tasks in a temp dir, no model calls."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import validate_tasks  # noqa: E402

SOURCE = "def add(a, b):\n    return a - b\n"

VISIBLE_TEST = """\
import unittest

from adder import add


class VisibleTests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(add(0, 0), 0)
"""

HIDDEN_TEST = """\
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adder import add


class HiddenTests(unittest.TestCase):
    def test_regression_adds_the_arguments(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-2, -3), -5)
        self.assertEqual(add(7, 0), 7)
        self.assertEqual(add(0, 7), 7)
"""

# The hidden suite passes on the original repo when it expects the buggy result.
HIDDEN_TEST_PASSES_ON_ORIGINAL = """\
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adder import add


class HiddenTests(unittest.TestCase):
    def test_regression_adds_the_arguments(self):
        self.assertEqual(add(2, 3), -1)
        self.assertEqual(add(-2, -3), 1)
        self.assertEqual(add(7, 0), 7)
        self.assertEqual(add(0, 7), -7)
"""

PATCH = """\
--- a/adder.py
+++ b/adder.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""

# Keeps the visible test green but leaves the bug in place.
VISIBLE_ONLY_PATCH = """\
--- a/adder.py
+++ b/adder.py
@@ -1,2 +1,3 @@
 def add(a, b):
-    return a - b
+    if a == 0 and b == 0:
+        return 0
+    return a - b
"""

# Breaks the visible test.
BROKEN_PATCH = """\
--- a/adder.py
+++ b/adder.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    raise ValueError("boom")
"""

TASK_MD = "Add two numbers.\n\nVisible tests: `python3 -m unittest tests/test_adder.py`.\n"

META = {
    "id": "adder",
    "language": "python",
    "difficulty": "medium",
    "visible_command": "python3 -m unittest tests/test_adder.py",
    "hidden_command": "python3 -m unittest _hidden_acceptance/test_hidden_adder.py",
    "hidden_dest": "_hidden_acceptance",
}


def scratch_parent() -> str | None:
    """TMPDIR when it is usable, otherwise the platform default."""
    parent = os.environ.get("TMPDIR")
    if parent and os.path.isdir(parent):
        return parent
    return None


def build_task(root: Path, task_id: str = "adder") -> Path:
    """Write a complete task: repo/, hidden/, solution.patch, TASK.md, meta.json."""
    task_dir = root / task_id
    (task_dir / "repo" / "tests").mkdir(parents=True)
    (task_dir / "hidden").mkdir(parents=True)
    (task_dir / "repo" / "adder.py").write_text(SOURCE, encoding="utf-8")
    (task_dir / "repo" / "tests" / "test_adder.py").write_text(VISIBLE_TEST, encoding="utf-8")
    (task_dir / "hidden" / "test_hidden_adder.py").write_text(HIDDEN_TEST, encoding="utf-8")
    (task_dir / "solution.patch").write_text(PATCH, encoding="utf-8")
    (task_dir / "TASK.md").write_text(TASK_MD, encoding="utf-8")
    (task_dir / "meta.json").write_text(json.dumps(dict(META, id=task_id), indent=2) + "\n", encoding="utf-8")
    return task_dir


class ValidateTasksTests(unittest.TestCase):
    """One synthetic task per test; every check of validate_tasks.check_task."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=scratch_parent())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task = build_task(self.root)

    def check(self, task_dir: Path | None = None) -> str:
        """Run check_task and return what it printed."""
        out = io.StringIO()
        with redirect_stdout(out):
            validate_tasks.check_task(task_dir or self.task)
        return out.getvalue()

    def failure(self, task_dir: Path | None = None) -> str:
        """Run check_task and return the AssertionError message."""
        with self.assertRaises(AssertionError) as caught:
            self.check(task_dir)
        return str(caught.exception)

    def run_main(self) -> tuple:
        """Run main() against the temp tasks dir and return (code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(validate_tasks, "TASKS", self.root):
            with redirect_stdout(out), redirect_stderr(err):
                code = validate_tasks.main()
        return code, out.getvalue(), err.getvalue()

    def test_valid_task_passes(self):
        """A complete task validates and is reported as ok."""
        self.assertIn("ok adder", self.check())

    def test_missing_repo_is_reported(self):
        """A task without repo/ fails validation."""
        shutil.rmtree(self.task / "repo")
        self.assertIn("missing repo, hidden, or solution.patch", self.failure())

    def test_missing_hidden_is_reported(self):
        """A task without hidden/ fails validation."""
        shutil.rmtree(self.task / "hidden")
        self.assertIn("missing repo, hidden, or solution.patch", self.failure())

    def test_missing_solution_patch_is_reported(self):
        """A task without solution.patch fails validation."""
        (self.task / "solution.patch").unlink()
        self.assertIn("missing repo, hidden, or solution.patch", self.failure())

    def test_meta_id_mismatch_is_reported(self):
        """meta.json id must match the task directory name."""
        meta = json.loads((self.task / "meta.json").read_text(encoding="utf-8"))
        meta["id"] = "other"
        (self.task / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        self.assertIn("meta id is other", self.failure())

    def test_hidden_tests_inside_repo_are_reported(self):
        """The hidden suite must not sit inside repo/."""
        leak = self.task / "repo" / "hidden"
        leak.mkdir()
        (leak / "test_hidden_adder.py").write_text(HIDDEN_TEST, encoding="utf-8")
        self.assertIn("hidden tests are inside repo/", self.failure())

    def test_task_md_mentioning_the_hidden_suite_is_reported(self):
        """TASK.md must not point at the hidden suite."""
        for mention in ("The hidden/ directory holds the checks.", "See _hidden_acceptance."):
            with self.subTest(mention=mention):
                (self.task / "TASK.md").write_text(mention + "\n", encoding="utf-8")
                self.assertIn("TASK.md mentions the hidden suite", self.failure())

    def test_hidden_tests_need_at_least_four_assertions(self):
        """The hidden suite needs four assertions."""
        few = HIDDEN_TEST.replace("        self.assertEqual(add(0, 7), 7)\n", "")
        (self.task / "hidden" / "test_hidden_adder.py").write_text(few, encoding="utf-8")
        self.assertIn("hidden tests need at least 4 assertions", self.failure())

    def test_hidden_tests_need_a_regression_assertion(self):
        """The hidden suite needs a regression case."""
        no_regression = HIDDEN_TEST.replace("test_regression_adds_the_arguments", "test_adds_the_arguments")
        (self.task / "hidden" / "test_hidden_adder.py").write_text(no_regression, encoding="utf-8")
        self.assertIn("hidden tests need a regression assertion", self.failure())

    def test_hidden_tests_must_fail_on_the_original_repo(self):
        """A hidden suite that already passes proves nothing."""
        (self.task / "hidden" / "test_hidden_adder.py").write_text(
            HIDDEN_TEST_PASSES_ON_ORIGINAL, encoding="utf-8"
        )
        self.assertIn("hidden tests passed on the original repo", self.failure())

    def test_solution_patch_must_make_the_hidden_tests_pass(self):
        """solution.patch has to fix the bug the hidden suite checks."""
        (self.task / "solution.patch").write_text(VISIBLE_ONLY_PATCH, encoding="utf-8")
        self.assertIn("hidden tests failed after solution.patch", self.failure())

    def test_solution_patch_must_keep_the_visible_tests_passing(self):
        """solution.patch may not break the visible tests."""
        (self.task / "solution.patch").write_text(BROKEN_PATCH, encoding="utf-8")
        self.assertIn("visible tests failed after solution.patch", self.failure())

    def test_main_reports_ok_for_valid_tasks(self):
        """main() exits 0 and prints one ok line per task."""
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("ok adder", out)
        self.assertIn("all tasks validated", out)
        self.assertEqual(err, "")

    def test_main_reports_a_failing_task(self):
        """main() exits 1 and names the task that failed."""
        broken = build_task(self.root, "broken")
        (broken / "solution.patch").unlink()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("ok adder", out)
        self.assertIn("missing repo, hidden, or solution.patch", err)
        self.assertIn("1 task(s) failed validation", err)

    def test_main_skips_directories_without_meta_json(self):
        """A directory without meta.json is not a task and is skipped."""
        (self.root / "notes").mkdir()
        (self.root / "notes" / "README.md").write_text("scratch\n", encoding="utf-8")
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("all tasks validated", out)
        self.assertNotIn("notes", out)

    @unittest.expectedFailure
    def test_missing_task_md_is_reported_as_a_failure(self):
        """TASK.md is part of the required layout; a task without it is reported, not fatal."""
        (self.task / "TASK.md").unlink()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("1 task(s) failed validation", err)

    @unittest.expectedFailure
    def test_malformed_meta_json_is_reported_as_a_failure(self):
        """A task with unreadable meta.json is reported, not fatal."""
        (self.task / "meta.json").write_text("{not json", encoding="utf-8")
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("1 task(s) failed validation", err)


if __name__ == "__main__":
    unittest.main()
