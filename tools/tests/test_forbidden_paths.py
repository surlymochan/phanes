import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "ci" / "forbidden_paths.py"


class ForbiddenPathsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "checker" / "forbidden_paths.py"
        self.script.parent.mkdir()
        shutil.copyfile(SCRIPT, self.script)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.write("openswarm/x.py", "old")
        self.git("add", "openswarm/x.py")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True)

    def write(self, name, content="content"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self):
        self.git("add", "-A", "--", ".", ":(exclude)checker")
        self.git("commit", "-qm", "change")

    def check(self, *args):
        return subprocess.run([sys.executable, str(self.script), *args], cwd=self.root,
                              text=True, capture_output=True)

    def diff(self):
        self.commit()
        return self.check("--base", self.base)

    def test_tmp_backup(self):
        self.write("nested/.tmp/a.bak")
        result = self.diff()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FORBIDDEN A nested/.tmp/a.bak", result.stdout)

    def test_modified_openswarm(self):
        self.write("openswarm/x.py", "new")
        result = self.diff()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FORBIDDEN M openswarm/x.py", result.stdout)

    def test_added_openswarm(self):
        self.write("openswarm/y.py")
        result = self.diff()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FORBIDDEN A openswarm/y.py", result.stdout)

    def test_deleted_openswarm(self):
        (self.root / "openswarm/x.py").unlink()
        result = self.diff()
        self.assertEqual(result.returncode, 0)

    def test_shared_library(self):
        self.write("swrm/tests/.foo.so")
        result = self.diff()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FORBIDDEN A swrm/tests/.foo.so", result.stdout)

    def test_normal_file(self):
        self.write("swrm/a.py")
        self.assertEqual(self.diff().returncode, 0)

    def test_allow(self):
        self.write("swrm/tests/.foo.so")
        self.script.with_name("forbidden_paths.allow").write_text("# exception\nswrm/tests/*.so\n")
        self.assertEqual(self.diff().returncode, 0)

    def test_tree(self):
        result = self.check("--tree")
        self.assertEqual(result.returncode, 0)
        self.assertIn("forbidden-paths: OK", result.stdout)

    def test_tree_other_rules(self):
        for path in ("nested/.tmp/a", "a/.lane-review/history.md", "openswarm/x.so", "swrm/x.bak-1"):
            self.write(path)
        self.commit()
        result = self.check("--tree")
        self.assertEqual(result.returncode, 1)
        for path in ("nested/.tmp/a", "a/.lane-review/history.md", "openswarm/x.so", "swrm/x.bak-1"):
            self.assertIn(f"FORBIDDEN A {path}", result.stdout)

    def test_review_and_backup(self):
        self.write("a/.lane-review/history.md")
        self.write("swrm/x.bak-20261108")
        result = self.diff()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FORBIDDEN A a/.lane-review/history.md", result.stdout)
        self.assertIn("FORBIDDEN A swrm/x.bak-20261108", result.stdout)


if __name__ == "__main__":
    unittest.main()
