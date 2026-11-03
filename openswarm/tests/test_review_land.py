"""Lane-review verdict landing: the classes of a missed verdict and the evidence (Refs #1667).

A review run that ends without a verdict file used to be one generic "no verdict file"
line.  The landing classes say *why* the verdict is missing: the run committed nothing,
the commit does not carry the file, the file carries no VERDICT line, the lane ref could
not be read, or git could not read the object behind the ref.  The evidence record is the
scene that is kept before the PR is handed to the next lane.
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("review_land_under_test",
                                               os.path.join(HERE, "..", "lib", "review", "land.py"))
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)


class Classify(unittest.TestCase):
    def test_a_run_without_a_commit_has_no_verdict_to_read(self):
        self.assertEqual(L.classify(False, True, 0, None), L.LAND_NO_COMMIT)

    def test_a_missing_ref_is_unreadable_not_a_missing_file(self):
        self.assertEqual(L.classify(True, False, 128, None), L.LAND_UNREADABLE)

    def test_a_commit_without_the_file_is_file_missing(self):
        # The tree of the commit reads fine and does not carry the verdict path.
        self.assertEqual(L.classify(True, True, 128, None, True), L.LAND_FILE_MISSING)

    def test_a_git_failure_is_not_a_missing_file(self):
        # The file could not be shown and the tree could not be read either: git is
        # broken, the lane may have delivered the verdict.
        self.assertEqual(L.classify(True, True, 128, None, False), L.LAND_GIT_ERROR)

    def test_an_unknown_read_failure_is_a_git_failure(self):
        self.assertEqual(L.classify(True, True, 128, None), L.LAND_GIT_ERROR)

    def test_a_file_without_a_verdict_line_is_no_marker(self):
        self.assertEqual(L.classify(True, True, 0, None), L.LAND_NO_MARKER)

    def test_a_file_with_a_verdict_landed(self):
        self.assertEqual(L.classify(True, True, 0, "APPROVE"), L.LAND_LANDED)

    def test_the_commit_wins_over_the_missing_ref(self):
        # A run without a commit is the cause the lane has to fix, whatever the ref says.
        self.assertEqual(L.classify(False, False, 128, None), L.LAND_NO_COMMIT)

    def test_the_delivery_classes_are_the_lane_faults(self):
        self.assertEqual(set(L.DELIVERY_CLASSES),
                         {L.LAND_NO_COMMIT, L.LAND_FILE_MISSING, L.LAND_NO_MARKER})
        self.assertNotIn(L.LAND_UNREADABLE, L.DELIVERY_CLASSES)
        self.assertNotIn(L.LAND_GIT_ERROR, L.DELIVERY_CLASSES)
        self.assertNotIn(L.LAND_LANDED, L.DELIVERY_CLASSES)

    def test_the_transport_classes_are_never_a_lane_fault(self):
        self.assertEqual(set(L.TRANSPORT_CLASSES), {L.LAND_UNREADABLE, L.LAND_GIT_ERROR})
        for landing in L.TRANSPORT_CLASSES:
            self.assertNotIn(landing, L.DELIVERY_CLASSES)
            self.assertFalse(L.is_failure(landing))

    def test_only_the_delivery_classes_are_a_failure(self):
        for landing in L.DELIVERY_CLASSES:
            self.assertTrue(L.is_failure(landing))
        self.assertFalse(L.is_failure(L.LAND_LANDED))
        self.assertFalse(L.is_failure("nonsense"))


class FailureClass(unittest.TestCase):
    def test_the_run_error_explains_a_failed_run(self):
        self.assertEqual(L.failure_class(L.LAND_NO_COMMIT, "exec_error"), "exec_error")

    def test_the_landing_class_explains_a_run_without_an_error(self):
        self.assertEqual(L.failure_class(L.LAND_NO_COMMIT, ""), "no_commit")
        self.assertEqual(L.failure_class(L.LAND_FILE_MISSING, None), "file_missing")
        self.assertEqual(L.failure_class(L.LAND_NO_MARKER, "  "), "no_marker")
        self.assertEqual(L.failure_class(L.LAND_GIT_ERROR, ""), "git_error")

    def test_an_unknown_miss_is_still_named(self):
        self.assertEqual(L.failure_class("", ""), "unknown")


class Describe(unittest.TestCase):
    def test_every_class_has_a_sentence(self):
        for landing in (L.LAND_LANDED, L.LAND_NO_COMMIT, L.LAND_FILE_MISSING,
                        L.LAND_NO_MARKER, L.LAND_UNREADABLE, L.LAND_GIT_ERROR):
            self.assertTrue(L.describe(landing))
            self.assertNotIn("unknown landing", L.describe(landing))

    def test_an_unknown_class_says_so(self):
        self.assertEqual(L.describe("nonsense"), "unknown landing")


class Evidence(unittest.TestCase):
    INFO = {"status": "failed", "error_class": "exec_error", "error": "boom",
            "summary": "the lane stopped", "files": '["a.py"]', "verify_rc": 1,
            "backend": "pi", "model": "m", "branch": "openswarm/run1", "commit": "c" * 40}
    LANDING = {"land": L.LAND_NO_COMMIT, "ref": None, "path": ".lane-review/PR-7.md",
               "show_rc": None, "show_err": "", "verdict": None}

    def test_the_record_keeps_the_scene_of_the_miss(self):
        record = L.evidence(7, "CX2", "run1", self.INFO, self.LANDING)
        self.assertEqual(record["pr"], 7)
        self.assertEqual(record["lane"], "CX2")
        self.assertEqual(record["run"], "run1")
        self.assertEqual(record["landing"], L.LAND_NO_COMMIT)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_class"], "exec_error")
        self.assertEqual(record["error"], "boom")
        self.assertEqual(record["summary"], "the lane stopped")
        self.assertEqual(record["files"], '["a.py"]')
        self.assertEqual(record["branch"], "openswarm/run1")
        self.assertEqual(record["commit"], "c" * 40)
        self.assertEqual(record["verdict_path"], ".lane-review/PR-7.md")
        self.assertIsNone(record["verdict"])

    def test_a_long_error_is_cut(self):
        info = dict(self.INFO, error="x" * 5000, summary="y" * 5000)
        record = L.evidence(7, "CX2", "run1", info, self.LANDING)
        self.assertEqual(len(record["error"]), 400)
        self.assertEqual(len(record["summary"]), 600)

    def test_an_empty_run_still_produces_a_record(self):
        record = L.evidence(7, "CX2", "run1", {}, {})
        self.assertEqual(record["landing"], None)
        self.assertEqual(record["status"], None)
        self.assertEqual(record["error"], "")

    def test_the_text_of_the_lane_is_kept_when_it_cannot_be_parsed(self):
        # The harvest read the file, but it carries no verdict line: what the lane
        # wrote is the only thing the next reviewer can learn from.
        landing = {"land": L.LAND_NO_MARKER, "ref": "abc", "path": ".lane-review/PR-7.md",
                   "show_rc": 0, "show_err": "", "verdict": None,
                   "text": "# Lane review\n\nno verdict line here\n"}
        record = L.evidence(7, "CX2", "run1", self.INFO, landing)
        self.assertEqual(record["landing"], L.LAND_NO_MARKER)
        self.assertIsNone(record["verdict"])
        self.assertIn("no verdict line here", record["verdict_text"])

    def test_a_long_verdict_text_is_cut(self):
        landing = {"land": L.LAND_NO_MARKER, "text": "x" * 9000}
        record = L.evidence(7, "CX2", "run1", self.INFO, landing)
        self.assertEqual(len(record["verdict_text"]), L.VERDICT_TEXT_KEEP)

    def test_the_git_error_is_kept_as_evidence(self):
        landing = {"land": L.LAND_GIT_ERROR, "ref": "abc", "path": ".lane-review/PR-7.md",
                   "show_rc": 128, "show_err": "fatal: bad object abc",
                   "ls_tree_rc": 128, "ls_tree_err": "fatal: bad object abc",
                   "path_missing": False, "verdict": None}
        record = L.evidence(7, "CX2", "run1", self.INFO, landing)
        self.assertEqual(record["landing"], L.LAND_GIT_ERROR)
        self.assertEqual(record["show_rc"], 128)
        self.assertEqual(record["show_error"], "fatal: bad object abc")
        self.assertEqual(record["tree_rc"], 128)
        self.assertEqual(record["tree_error"], "fatal: bad object abc")
        self.assertIs(record["path_missing"], False)


if __name__ == "__main__":
    unittest.main()
