"""`openswarm/lib/merge/content.py`: a pull request has to deliver the card (Refs #1814).

The module is loaded by file path: ``openswarm/lib/merge`` is a namespace package
without an ``__init__.py``, so it cannot be imported as a package from the
repository root (same trick as ``tests/test_merge_rerun.py``).
"""
import importlib.util
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "merge" / "content.py"

spec = importlib.util.spec_from_file_location("merge_content", MODULE)
content = importlib.util.module_from_spec(spec)
spec.loader.exec_module(content)


class DeclaredPathsTests(unittest.TestCase):
    """The card declares the paths it delivers in its issue."""

    def test_a_chinese_declaration_line_is_read(self):
        """`产物路径：docs/x.md` declares one path."""
        self.assertEqual(content.declared_paths("产物路径：docs/x.md"), ["docs/x.md"])

    def test_an_english_declaration_line_is_read(self):
        """`Expected-Paths: docs/x.md` declares one path."""
        self.assertEqual(content.declared_paths("Expected-Paths: docs/x.md"), ["docs/x.md"])

    def test_a_backticked_path_is_read(self):
        """The backticks of a declaration line are the path."""
        self.assertEqual(content.declared_paths("交付路径：`tools/check.py`"), ["tools/check.py"])

    def test_several_paths_of_one_line_are_read(self):
        """Every path of a declaration line counts."""
        self.assertEqual(content.declared_paths("产物路径：`docs/a.md`, `docs/b.md`"),
                         ["docs/a.md", "docs/b.md"])

    def test_a_duplicate_path_is_reported_once(self):
        """The same path twice is one path."""
        self.assertEqual(content.declared_paths("产物路径：docs/a.md\n产物路径：docs/a.md"),
                         ["docs/a.md"])

    def test_another_line_declares_nothing(self):
        """The acceptance line is not a declaration."""
        body = "# 卡\n\n验收：`python3 -m unittest discover -s tests`\n"
        self.assertEqual(content.declared_paths(body), [])

    def test_a_prose_line_declares_nothing(self):
        """A line without a path declares nothing."""
        self.assertEqual(content.declared_paths("改一份文档。"), [])

    def test_a_declared_directory_matches_the_files_below_it(self):
        """A declared directory matches every file below it."""
        self.assertTrue(content.path_matches("docs/a.md", "docs"))
        self.assertTrue(content.path_matches("docs/a.md", "docs/"))
        self.assertFalse(content.path_matches("docsx/a.md", "docs"))

    def test_a_declared_glob_matches(self):
        """A declared glob is matched with fnmatch."""
        self.assertTrue(content.path_matches("docs/evidence/a.json", "docs/evidence/*.json"))
        self.assertFalse(content.path_matches("docs/evidence/a.md", "docs/evidence/*.json"))


class ClassifyPathsTests(unittest.TestCase):
    """The changed files are split into delivery, artifact and other paths."""

    def test_a_review_record_is_an_artifact(self):
        """`.lane-review/` holds the review record, not the delivery."""
        groups = content.classify_paths([".lane-review/PR-1799.md"])
        self.assertEqual(groups["artifacts"], [".lane-review/PR-1799.md"])
        self.assertEqual(groups["delivery"], [])

    def test_an_evidence_file_is_an_artifact(self):
        """`docs/evidence/` holds the evidence, not the delivery."""
        groups = content.classify_paths(["docs/evidence/report.json"])
        self.assertEqual(groups["artifacts"], ["docs/evidence/report.json"])

    def test_a_source_file_is_a_delivery(self):
        """A source file changes the product."""
        groups = content.classify_paths(["openswarm/lib/merge/content.py"])
        self.assertEqual(groups["delivery"], ["openswarm/lib/merge/content.py"])

    def test_a_test_file_is_a_delivery(self):
        """A test is a delivery."""
        groups = content.classify_paths(["openswarm/tests/test_merge_content.py"])
        self.assertEqual(groups["delivery"], ["openswarm/tests/test_merge_content.py"])

    def test_a_tool_file_is_a_delivery(self):
        """A tool is a delivery."""
        groups = content.classify_paths(["tools/check-merge-idle-heartbeat.py"])
        self.assertEqual(groups["delivery"], ["tools/check-merge-idle-heartbeat.py"])

    def test_a_tool_without_a_file_extension_is_a_delivery(self):
        """`openswarm/bin/openswarm` has no file extension: the directory says
        that it is the tool of this repository."""
        groups = content.classify_paths(["openswarm/bin/openswarm"])
        self.assertEqual(groups["delivery"], ["openswarm/bin/openswarm"])

    def test_a_nested_tool_without_a_file_extension_is_a_delivery(self):
        """A tool of a subdirectory counts as well."""
        groups = content.classify_paths(["openswarm/bin/fleet-capacity"])
        self.assertEqual(groups["delivery"], ["openswarm/bin/fleet-capacity"])

    def test_a_nested_test_directory_is_a_delivery(self):
        """A test directory of a subpackage counts as well."""
        groups = content.classify_paths(["openswarm/tests/fixtures/fake_gh"])
        self.assertEqual(groups["delivery"], ["openswarm/tests/fixtures/fake_gh"])

    def test_a_plain_document_is_neither(self):
        """A document is not a delivery on its own: the card has to declare it."""
        groups = content.classify_paths(["docs/architecture-review-brief.md"])
        self.assertEqual(groups["other"], ["docs/architecture-review-brief.md"])
        self.assertEqual(groups["delivery"], [])

    def test_a_declared_path_is_a_delivery(self):
        """The declaration of the card turns a document into a delivery."""
        groups = content.classify_paths(["docs/architecture-review-brief.md"],
                                        ["docs/architecture-review-brief.md"])
        self.assertEqual(groups["delivery"], ["docs/architecture-review-brief.md"])

    def test_the_declaration_wins_over_the_artifact_classification(self):
        """A card may deliver an evidence file when it says so."""
        groups = content.classify_paths(["docs/evidence/report.json"],
                                        ["docs/evidence/report.json"])
        self.assertEqual(groups["delivery"], ["docs/evidence/report.json"])
        self.assertEqual(groups["artifacts"], [])


class JudgeTests(unittest.TestCase):
    """The verdict of a pull request."""

    def test_a_pull_request_with_only_a_review_record_is_empty(self):
        """The #1799 case: a review record is not a delivery."""
        verdict = content.judge([".lane-review/PR-1799.md"])
        self.assertEqual(verdict["status"], "empty")

    def test_a_pull_request_with_a_code_change_delivers(self):
        """A code change is a delivery."""
        verdict = content.judge(["openswarm/lib/merge/content.py"])
        self.assertEqual(verdict["status"], "delivery")

    def test_a_pull_request_without_any_change_is_empty(self):
        """Nothing changed: nothing was delivered."""
        self.assertEqual(content.judge([])["status"], "empty")

    def test_the_declared_path_turns_the_verdict_around(self):
        """The declaration of the card releases the matching file."""
        verdict = content.judge(["docs/x.md"], ["docs/x.md"])
        self.assertEqual(verdict["status"], "delivery")
        self.assertEqual(verdict["expected"], ["docs/x.md"])


if __name__ == "__main__":
    unittest.main()
