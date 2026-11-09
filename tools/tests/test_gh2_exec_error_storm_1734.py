"""Acceptance tests for the #1734 diagnosis document (Refs #1734).

The card is accepted on two things the review made explicit:

* **B2** -- the GH2 trial run must *succeed* and must not be an
  ``exec_error`` before ``PAUSE-GH2`` is lifted.  A run that merely is not
  ``failed/exec_error`` (``failed/auth``, ``failed/rate_limit``, ...) is not a
  pass, so the document must not use that weaker criterion.
* the delivery notes must map every blocker of the previous review (**B1**,
  **B2**, **B3**) to the change that fixes it, to the test that covers it and
  to the result of that test.

The remaining checks keep the comparison with CX9 (#1709/#1710) open: the
signature difference is not a root cause verdict.
"""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "gh2-exec-error-storm-1734.md"

TEST_RE = re.compile(r"(test_[A-Za-z0-9_]+|unittest|单测)")
RESULT_RE = re.compile(r"(通过|passed|PASS|OK\b|Ran \d+|结果)")
SUCCESS_RE = re.compile(r"(\bdone\b|成功|succeed|正常结束|非空提交)")
OPEN_RE = re.compile(r"(尚未知|未知|无法|不能|不足以|证据不足|待确认|不确定|尚无|insufficient|unknown)")


class Gh2ExecErrorStormDocumentTests(unittest.TestCase):
    """The diagnosis document of the card."""

    @property
    def doc(self):
        """The text of the diagnosis document."""
        if not DOC.exists():
            self.fail("missing %s: #1734 needs the diagnosis document"
                      % DOC.relative_to(ROOT))
        return DOC.read_text(encoding="utf-8")

    def blocks(self):
        """The paragraphs of the document."""
        return [part.strip() for part in re.split(r"\n\s*\n", self.doc) if part.strip()]

    def sections(self):
        """The ``## `` sections of the document."""
        return [part.strip() for part in re.split(r"\n(?=## )", self.doc) if part.strip()]

    def acceptance_sections(self):
        """The section that states how the trial run is accepted."""
        sections = [part for part in self.sections()
                    if part.splitlines()[0].startswith("## ") and "验收" in part.splitlines()[0]]
        if not sections:
            sections = [part for part in self.sections()
                        if "试跑" in part and "判据" in part]
        return sections

    def test_the_trial_run_criterion_requires_a_successful_run(self):
        sections = self.acceptance_sections()
        self.assertTrue(sections, "the document must describe the GH2 trial run")
        passing = [part for part in sections
                   if "exec_error" in part and SUCCESS_RE.search(part)]
        self.assertTrue(passing,
                        "the trial run criterion must require a successful run "
                        "(status done) and a non exec_error class")

    def test_the_trial_run_criterion_names_the_error_class(self):
        sections = self.acceptance_sections()
        self.assertTrue(sections, "the document must describe the GH2 trial run")
        self.assertTrue(any("error_class" in part for part in sections),
                        "the trial run criterion must name the error_class of the run")

    def test_the_weak_not_exec_error_criterion_is_gone(self):
        # "at least not failed/exec_error" also accepts failed/auth or
        # failed/rate_limit, so it must not be the criterion any more.
        sections = self.acceptance_sections()
        self.assertTrue(sections, "the document must describe the GH2 trial run")
        for section in sections:
            self.assertIsNone(re.search(r"至少不是", section),
                              "the trial run criterion must not be the weaker 'at least "
                              "not failed/exec_error'")
            self.assertIsNone(re.search(r"(?<!not )不是\s*`?failed/exec_error", section),
                              "the trial run criterion must not accept a merely non "
                              "exec_error run")

    def test_the_delivery_notes_map_every_blocker_to_its_change_and_its_test(self):
        for blocker in ("B1", "B2", "B3"):
            self.assertIsNotNone(re.search(r"\b%s\b" % blocker, self.doc),
                                 "the delivery notes must mention %s" % blocker)
            entries = [part for part in self.blocks()
                       if re.search(r"\b%s\b" % blocker, part)]
            self.assertTrue(entries, "the delivery notes must list %s" % blocker)
            documented = [part for part in entries
                          if TEST_RE.search(part) and RESULT_RE.search(part)]
            self.assertTrue(
                documented,
                "the %s entry must name the change, the test that covers it and the "
                "result of that test" % blocker)

    def test_the_delivery_notes_name_the_tests_of_the_whole_change(self):
        self.assertGreaterEqual(
            len(TEST_RE.findall(self.doc)), 3,
            "the delivery notes must name the tests of the whole change (B1, B2, B3)")

    def test_the_cx9_comparison_stays_open(self):
        blocks = [part for part in self.blocks()
                  if "CX9" in part and "根因" in part]
        self.assertTrue(blocks, "the document must discuss the CX9 root cause question")
        for block in blocks:
            self.assertIsNotNone(
                OPEN_RE.search(block),
                "the comparison with CX9 must stay open until the field evidence and "
                "the #1709/#1710 conclusions are read:\n%s" % block)

    def test_the_cx9_comparison_refers_to_the_other_cards(self):
        self.assertIn("#1709", self.doc)
        self.assertIn("#1710", self.doc)

    def test_the_comparison_example_states_what_it_needs_and_what_it_reports(self):
        sections = [part for part in self.sections()
                    if "--compare-lane" in part and "--runs" in part]
        self.assertTrue(sections,
                        "the document must show the comparison command")
        for section in sections:
            self.assertIn("--lane", section,
                          "the comparison example must state that the lane filter "
                          "must not drop the compared lane")
            self.assertIsNotNone(
                OPEN_RE.search(section),
                "the comparison example must say what it reports when the evidence "
                "is insufficient")


if __name__ == "__main__":
    unittest.main()
