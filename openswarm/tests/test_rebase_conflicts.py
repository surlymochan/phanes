import importlib.util
import os
import subprocess
import tempfile
import unittest

MODULE = os.path.join(os.path.dirname(__file__), "..", "lib", "dispatch", "rebase_conflicts.py")
SPEC = importlib.util.spec_from_file_location("rebase_conflicts", MODULE)
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)


class RebaseConflictTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="rebase-conflict-")
        self.addCleanup(__import__("shutil").rmtree, self.repo, ignore_errors=True)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.write("readme.md", "base\n")
        self.write("src.py", "base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", self.repo, *args], check=check,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()

    def write(self, path, value):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as stream:
            stream.write(value if isinstance(value, bytes) else value.encode())

    def make_conflict(self, path, left, right):
        self.git("checkout", "-qb", "main")
        self.write(path, left)
        self.git("add", path)
        self.git("commit", "-qm", "main change")
        self.git("checkout", "-q", self.base, "-b", "topic")
        self.write(path, right)
        self.git("add", path)
        self.git("commit", "-qm", "topic change")
        result = subprocess.run(["git", "-C", self.repo, "merge", "main"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode, 0)

    def test_document_conflict_is_auto_and_union_retry_stages_it(self):
        self.make_conflict("readme.md", "main text\n", "topic text\n")
        result = R.classify(self.repo)
        self.assertEqual(result["classification"], "automatic")
        self.assertEqual(R.resolve_document_union(self.repo, result["conflicts"]), ["readme.md"])
        self.assertFalse(R.conflict_entries(self.repo))
        with open(os.path.join(self.repo, "readme.md")) as stream:
            merged = stream.read()
        self.assertIn("main text", merged)
        self.assertIn("topic text", merged)

    def test_same_region_code_conflict_is_manual_with_author_direction(self):
        self.make_conflict("src.py", "main edit\n", "topic edit\n")
        result = R.classify(self.repo)
        self.assertEqual(result["classification"], "manual")
        self.assertEqual(result["conflicts"][0]["path"], "src.py")
        self.assertGreater(result["conflicts"][0]["conflict_lines"], 0)
        self.assertIn("作者", result["suggested_direction"])

    def test_binary_conflict_is_manual(self):
        self.make_conflict("asset.bin", b"\0main", b"\0topic")
        self.assertEqual(R.classify(self.repo)["conflicts"][0]["kind"], "binary")


if __name__ == "__main__":
    unittest.main()
