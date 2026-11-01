"""Integration tests for union conflict merges using disposable Git repos."""
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

MODULE = pathlib.Path(__file__).resolve().parents[1] / "lib" / "merge" / "union.py"
spec = importlib.util.spec_from_file_location("merge_union", MODULE)
union = importlib.util.module_from_spec(spec)
spec.loader.exec_module(union)


class UnionMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="merge-union-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "docs").mkdir()
        (self.repo / "docs/ctl-boot.md").write_text("start\nanchor\n", encoding="utf-8")
        (self.repo / "app.py").write_text("value = 'origin'\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.git("branch", "base")
        self.git("checkout", "-qb", "feature")

    def git(self, *args, **kwargs):
        return subprocess.run(["git"] + list(args), cwd=self.repo, check=True,
                              capture_output=True, text=True, **kwargs)

    def branch_change(self, name, docs_line, code_value=None):
        self.git("checkout", "-q", "base")
        (self.repo / "docs/ctl-boot.md").write_text(docs_line + "\n", encoding="utf-8")
        if code_value is not None:
            (self.repo / "app.py").write_text("value = '{}'\n".format(code_value), encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", name)
        self.git("checkout", "-q", "feature")

    def worktree_paths(self):
        output = self.git("worktree", "list", "--porcelain").stdout
        return [line.split(" ", 1)[1] for line in output.splitlines() if line.startswith("worktree ")]

    def test_union_conflict_preserves_both_lines_and_cleans_worktree(self):
        self.branch_change("base", "from-base")
        feature = self.repo / "docs/ctl-boot.md"
        feature.write_text("from-feature\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "feature")
        before = set(self.worktree_paths())

        result = union.union_merge(str(self.repo), "feature", "base", ["docs/*.md"])

        self.assertEqual(result["status"], "merged")
        merged = subprocess.run(["git", "show", result["sha"] + ":docs/ctl-boot.md"],
                                cwd=self.repo, check=True, capture_output=True, text=True).stdout
        self.assertIn("from-base", merged)
        self.assertIn("from-feature", merged)
        self.assertEqual(set(self.worktree_paths()), before)

    def test_non_union_conflict_aborts_without_changing_repository(self):
        self.branch_change("base", "from-base", "base")
        (self.repo / "docs/ctl-boot.md").write_text("from-feature\n", encoding="utf-8")
        (self.repo / "app.py").write_text("value = 'feature'\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "feature")
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        before_files = (self.repo / "docs/ctl-boot.md").read_text(encoding="utf-8")

        result = union.union_merge(str(self.repo), "feature", "base", ["docs/*.md"])

        self.assertEqual(result, {"status": "mixed", "files": ["app.py"]})
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), before_head)
        self.assertEqual((self.repo / "docs/ctl-boot.md").read_text(encoding="utf-8"), before_files)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_base_already_ancestor_is_noop(self):
        result = union.union_merge(str(self.repo), "feature", "base", ["docs/*.md"])
        self.assertEqual(result, {"status": "noop"})
        self.assertEqual(self.worktree_paths(), [str(self.repo)])

    def test_fast_forward_base_reports_merged_with_new_head(self):
        self.branch_change("base", "from-base")
        feature_head = self.git("rev-parse", "feature").stdout.strip()
        base_head = self.git("rev-parse", "base").stdout.strip()
        self.assertNotEqual(feature_head, base_head)
        before = set(self.worktree_paths())

        result = union.union_merge(str(self.repo), "feature", "base", ["docs/*.md"])

        self.assertEqual(result, {"status": "merged", "sha": base_head})
        self.assertEqual(set(self.worktree_paths()), before)

    def test_unknown_base_ref_reports_error(self):
        before = set(self.worktree_paths())

        result = union.union_merge(str(self.repo), "feature", "missing-base", ["docs/*.md"])

        self.assertEqual(result["status"], "error")
        self.assertIn("missing-base", result["error"])
        self.assertNotEqual(result.get("files"), [])
        self.assertEqual(set(self.worktree_paths()), before)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_clean_divergent_merge_creates_merge_commit(self):
        self.git("checkout", "-q", "base")
        (self.repo / "app.py").write_text("value = 'base'\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base-change")
        self.git("checkout", "-q", "feature")
        (self.repo / "docs/ctl-boot.md").write_text("from-feature\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "feature-change")
        feature_head = self.git("rev-parse", "feature").stdout.strip()
        before = set(self.worktree_paths())

        result = union.union_merge(str(self.repo), "feature", "base", ["docs/*.md"])

        self.assertEqual(result["status"], "merged")
        self.assertNotEqual(result["sha"], feature_head)
        parents = self.git("rev-list", "--parents", "-n", "1", result["sha"]).stdout.split()
        self.assertEqual(len(parents), 3)
        self.assertEqual(set(self.worktree_paths()), before)


class UnionCandidatesTests(unittest.TestCase):
    def test_patterns_and_sorting(self):
        prs = [
            {"number": 12, "mergeable": "CONFLICTING", "headRefName": "b"},
            {"number": 4, "mergeable": "CONFLICTING", "headRefName": "a"},
            {"number": 2, "mergeable": "MERGEABLE", "headRefName": "c"},
            {"number": 1, "mergeable": "CONFLICTING", "headRefName": "d"},
        ]
        conflicts = {12: ["docs/guide.md"], 4: ["docs/x.md", "docs/y.md"],
                     2: ["docs/no.md"], 1: []}

        result = union.union_candidates(prs, conflicts, ["docs/*.md"])

        self.assertEqual([pr["number"] for pr in result], [4, 12])


if __name__ == "__main__":
    unittest.main()
