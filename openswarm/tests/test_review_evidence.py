"""`openswarm/lib/review/evidence.py`: the raw evidence pack of a lane review (Refs #1788).

The module is loaded by path: `openswarm/lib/review/` is a namespace package without an
`__init__.py`, so it must not be imported as a package.
"""
import importlib.util
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
EVIDENCE_MODULE = HERE.parent / "lib" / "review" / "evidence.py"

spec = importlib.util.spec_from_file_location("review_evidence", EVIDENCE_MODULE)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)

CI_ITEMS = [{"name": "OpenSwarm tests", "status": "pass", "detail": "https://example.test/runs/1"}]
TEST_LOGS = [{"command": "python -m unittest discover -s tests", "summary": "42 tests, all passed",
              "link": "https://example.test/runs/1/logs"}]
LOG_LINES = ["Ran 42 tests in 1.234s", "OK"]
DIFFSTAT = {"files": 3, "insertions": 120, "deletions": 12, "effective_lines": 95}
CHECKS = [{"item": "acceptance item 1", "result": "pass", "evidence": "`python -m unittest` → OK"}]


def complete_pack(**kwargs):
    """A pack with all five sections."""
    arguments = {"pr": 1788, "sha": "a" * 40, "ci": CI_ITEMS, "test_logs": TEST_LOGS,
                 "log_lines": LOG_LINES, "diffstat": DIFFSTAT, "checks": CHECKS,
                 "opinion": "looks fine to me"}
    arguments.update(kwargs)
    return evidence.build_evidence(**arguments)


class BuildEvidenceTests(unittest.TestCase):
    """The pack is assembled from structured fields, not from free text."""

    def test_the_pack_carries_the_five_sections(self):
        pack = complete_pack()

        for section in evidence.SECTIONS:
            self.assertIn(section["title"], pack)

    def test_the_sections_come_in_the_fixed_order(self):
        pack = complete_pack()

        positions = [pack.index(section["title"]) for section in evidence.SECTIONS]
        self.assertEqual(positions, sorted(positions))

    def test_the_fields_are_written_with_their_markers(self):
        pack = complete_pack()

        self.assertIn("CI: OpenSwarm tests | pass | https://example.test/runs/1", pack)
        self.assertIn("TEST: python -m unittest discover -s tests | 42 tests, all passed | "
                      "https://example.test/runs/1/logs", pack)
        self.assertIn("LOG: Ran 42 tests in 1.234s", pack)
        self.assertIn("DIFFSTAT: files=3 | insertions=120 | deletions=12 | effective_lines=95", pack)
        self.assertIn("CHECK: acceptance item 1 | pass | `python -m unittest` → OK", pack)
        self.assertIn("OPINION: looks fine to me", pack)

    def test_the_diff_statistics_carry_the_effective_line_rule(self):
        pack = complete_pack()

        self.assertIn("RULE: ", pack)
        self.assertIn("effective_lines", pack)

    def test_a_section_without_data_is_left_out(self):
        pack = complete_pack(ci=None)

        self.assertNotIn("CI results", pack)
        self.assertEqual(evidence.missing_sections(pack), ["ci"])

    def test_a_section_with_an_empty_list_is_written_but_empty(self):
        pack = complete_pack(ci=[])

        self.assertIn("CI results", pack)
        self.assertEqual(evidence.missing_sections(pack), [])
        self.assertIn("ci-empty", evidence.parse_evidence(pack)["errors"])

    def test_the_optional_opinion_section_may_be_left_out(self):
        pack = complete_pack(opinion=None)

        self.assertNotIn("Preliminary opinion", pack)
        self.assertTrue(evidence.is_complete(pack))


class ParseEvidenceTests(unittest.TestCase):
    """The pack is parsed into structured fields."""

    def test_a_complete_pack_is_complete(self):
        parsed = evidence.parse_evidence(complete_pack())

        self.assertTrue(parsed["complete"])
        self.assertEqual(parsed["missing"], [])
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["sections"], ["ci", "testlog", "diffstat", "checks", "opinion"])

    def test_the_ci_items_are_parsed_one_by_one(self):
        parsed = evidence.parse_evidence(complete_pack())

        self.assertEqual(len(parsed["ci"]), 1)
        self.assertEqual(parsed["ci"][0]["name"], "OpenSwarm tests")
        self.assertEqual(parsed["ci"][0]["status"], "pass")
        self.assertEqual(parsed["ci"][0]["detail"], "https://example.test/runs/1")

    def test_a_github_conclusion_is_folded_into_the_status(self):
        parsed = evidence.parse_evidence(complete_pack(
            ci=[{"name": "OpenSwarm tests", "status": "SUCCESS", "detail": "url"}]))

        self.assertEqual(parsed["ci"][0]["status"], "pass")

    def test_the_test_logs_keep_the_summary_the_link_and_the_raw_lines(self):
        parsed = evidence.parse_evidence(complete_pack())

        self.assertEqual(parsed["test_logs"][0]["command"], "python -m unittest discover -s tests")
        self.assertEqual(parsed["test_logs"][0]["summary"], "42 tests, all passed")
        self.assertEqual(parsed["test_logs"][0]["link"], "https://example.test/runs/1/logs")
        self.assertEqual(parsed["log_lines"], LOG_LINES)

    def test_the_diff_statistics_are_numbers(self):
        parsed = evidence.parse_evidence(complete_pack())

        self.assertEqual(parsed["diffstat"]["files"], 3)
        self.assertEqual(parsed["diffstat"]["insertions"], 120)
        self.assertEqual(parsed["diffstat"]["deletions"], 12)
        self.assertEqual(parsed["diffstat"]["effective_lines"], 95)
        self.assertTrue(parsed["diffstat_rule"])

    def test_the_acceptance_checks_carry_the_result_and_the_raw_evidence(self):
        parsed = evidence.parse_evidence(complete_pack())

        self.assertEqual(parsed["checks"][0]["item"], "acceptance item 1")
        self.assertEqual(parsed["checks"][0]["result"], "pass")
        self.assertEqual(parsed["checks"][0]["evidence"], "`python -m unittest` → OK")

    def test_the_three_results_of_an_acceptance_check(self):
        pack = complete_pack(checks=[{"item": "a", "result": "pass", "evidence": "ok"},
                                     {"item": "b", "result": "fail", "evidence": "boom"},
                                     {"item": "c", "result": "未查", "evidence": "no log"}])

        parsed = evidence.parse_evidence(pack)

        self.assertEqual([check["result"] for check in parsed["checks"]],
                         ["pass", "fail", "unchecked"])

    def test_the_opinion_is_read_and_marked(self):
        parsed = evidence.parse_evidence(complete_pack())

        self.assertTrue(parsed["opinion"]["present"])
        self.assertTrue(parsed["opinion"]["marked"])
        self.assertEqual(parsed["opinion"]["text"], "looks fine to me")

    def test_the_raw_evidence_keeps_a_pipe_inside_it(self):
        pack = complete_pack(checks=[{"item": "a", "result": "pass",
                                      "evidence": "`grep -E 'a|b' f` → 1 match | 0 errors"}])

        parsed = evidence.parse_evidence(pack)

        self.assertEqual(parsed["checks"][0]["evidence"], "`grep -E 'a|b' f` → 1 match | 0 errors")

    def test_the_raw_ci_detail_keeps_a_pipe_inside_it(self):
        pack = complete_pack(ci=[{"name": "OpenSwarm tests", "status": "pass",
                                  "detail": "run 1 | ubuntu-24.04 | python 3.11"}])

        parsed = evidence.parse_evidence(pack)

        self.assertEqual(parsed["ci"][0]["detail"], "run 1 | ubuntu-24.04 | python 3.11")

    def test_the_raw_test_log_link_keeps_a_pipe_inside_it(self):
        pack = complete_pack(test_logs=[{"command": "python -m unittest", "summary": "42 tests",
                                         "link": "runs/42.log | line 3"}])

        parsed = evidence.parse_evidence(pack)

        self.assertEqual(parsed["test_logs"][0]["link"], "runs/42.log | line 3")


class MissingSectionTests(unittest.TestCase):
    """A pack that does not carry every section is incomplete, and says what is missing."""

    def test_a_pack_without_the_ci_section_is_incomplete(self):
        pack = complete_pack(ci=None)

        self.assertEqual(evidence.missing_sections(pack), ["ci"])
        self.assertFalse(evidence.is_complete(pack))

    def test_a_pack_without_the_test_log_section_is_incomplete(self):
        pack = complete_pack(test_logs=None)

        self.assertEqual(evidence.missing_sections(pack), ["testlog"])
        self.assertFalse(evidence.is_complete(pack))

    def test_a_pack_without_the_diff_statistics_is_incomplete(self):
        pack = complete_pack(diffstat=None)

        self.assertEqual(evidence.missing_sections(pack), ["diffstat"])
        self.assertFalse(evidence.is_complete(pack))

    def test_a_pack_without_the_acceptance_checks_is_incomplete(self):
        pack = complete_pack(checks=None)

        self.assertEqual(evidence.missing_sections(pack), ["checks"])
        self.assertFalse(evidence.is_complete(pack))

    def test_free_text_is_not_a_pack(self):
        parsed = evidence.parse_evidence("The PR looks fine to me, tests pass.\n")

        self.assertEqual(parsed["missing"], ["ci", "testlog", "diffstat", "checks"])
        self.assertFalse(parsed["complete"])

    def test_an_empty_pack_is_not_a_pack(self):
        self.assertEqual(evidence.missing_sections(""), ["ci", "testlog", "diffstat", "checks"])
        self.assertFalse(evidence.is_complete(None))


class FailureModeTests(unittest.TestCase):
    """The three failure modes of the card are detected, not tolerated."""

    def test_a_verdict_line_in_the_pack_is_refused(self):
        pack = complete_pack() + "\nVERDICT: APPROVE\n"

        parsed = evidence.parse_evidence(pack)

        self.assertIn("verdict-in-pack", parsed["errors"])
        self.assertFalse(parsed["complete"])

    def test_a_judgement_line_in_the_pack_is_refused(self):
        pack = complete_pack() + "\n判定：APPROVE\n"

        self.assertIn("verdict-in-pack", evidence.parse_evidence(pack)["errors"])

    def test_an_unmarked_opinion_section_is_refused(self):
        pack = complete_pack().replace("Preliminary opinion (初步意见 · 参考，不算判定)",
                                       "Preliminary opinion")

        parsed = evidence.parse_evidence(pack)

        self.assertIn("opinion-not-marked", parsed["errors"])
        self.assertFalse(parsed["complete"])

    def test_a_test_log_without_the_raw_lines_is_refused(self):
        pack = complete_pack(log_lines=[])

        parsed = evidence.parse_evidence(pack)

        self.assertIn("testlog-no-raw", parsed["errors"])
        self.assertFalse(parsed["complete"])

    def test_a_test_log_without_a_link_is_refused(self):
        pack = complete_pack(test_logs=[{"command": "pytest", "summary": "all passed"}])

        parsed = evidence.parse_evidence(pack)

        self.assertIn("testlog-no-link", parsed["errors"])

    def test_an_acceptance_check_without_the_raw_evidence_is_refused(self):
        pack = complete_pack(checks=[{"item": "acceptance item 1", "result": "pass", "evidence": ""}])

        parsed = evidence.parse_evidence(pack)

        self.assertIn("check-no-evidence", parsed["errors"])
        self.assertFalse(parsed["complete"])

    def test_an_acceptance_check_with_an_unknown_result_is_refused(self):
        pack = complete_pack(checks=[{"item": "acceptance item 1", "result": "maybe",
                                      "evidence": "raw output"}])

        self.assertIn("check-bad-result", evidence.parse_evidence(pack)["errors"])

    def test_diff_statistics_without_the_effective_lines_are_refused(self):
        pack = complete_pack().replace(" | effective_lines=95", "")

        parsed = evidence.parse_evidence(pack)

        self.assertIn("diffstat-incomplete", parsed["errors"])
        self.assertFalse(parsed["complete"])

    def test_a_duplicated_section_is_refused(self):
        pack = complete_pack() + "\n## 1. CI results (CI 结论逐项)\nCI: again | pass | url\n"

        self.assertIn("section-duplicated", evidence.parse_evidence(pack)["errors"])


class DiffstatTests(unittest.TestCase):
    """The effective lines are the code and test lines of the diff, the docs do not count."""

    def test_the_code_and_the_test_files_are_effective(self):
        numstat = "10\t2\tsrc/app.py\n4\t1\topenswarm/tests/test_app.py\n"

        stats = evidence.compute_diffstat(numstat)

        self.assertEqual(stats["files"], 2)
        self.assertEqual(stats["insertions"], 14)
        self.assertEqual(stats["deletions"], 3)
        self.assertEqual(stats["effective_lines"], 17)
        self.assertEqual(stats["docs_lines"], 0)

    def test_the_documentation_does_not_count(self):
        numstat = "40\t10\tdocs/design/thing.md\n5\t0\tREADME.md\n"

        stats = evidence.compute_diffstat(numstat)

        self.assertEqual(stats["docs_lines"], 55)
        self.assertEqual(stats["effective_lines"], 0)

    def test_a_binary_file_counts_as_no_line(self):
        stats = evidence.compute_diffstat("-\t-\tassets/logo.png\n")

        self.assertEqual(stats["files"], 1)
        self.assertEqual(stats["insertions"], 0)
        self.assertEqual(stats["effective_lines"], 0)


class SpecTests(unittest.TestCase):
    """The spec the review task hands to the lane names the five sections."""

    def test_the_spec_names_every_section(self):
        spec = evidence.render_spec(1788)

        for section in evidence.SECTIONS:
            self.assertIn(section["title"], spec)

    def test_the_spec_names_the_markers_and_the_rules(self):
        spec = evidence.render_spec(1788)

        for marker in ("CI:", "TEST:", "LOG:", "DIFFSTAT:", "CHECK:", "OPINION:"):
            self.assertIn(marker, spec)
        self.assertIn("no `VERDICT:` line anywhere", spec)
        self.assertIn("参考，不算判定", spec)

    def test_the_spec_carries_the_pr_number(self):
        self.assertIn("1788", evidence.render_spec(1788))


if __name__ == "__main__":
    unittest.main()
