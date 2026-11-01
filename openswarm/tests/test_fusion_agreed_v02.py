"""Machine-checkable tests for the fusion agreement v0.2 doc (phanes #1481).

The document lives at the phanes repository root, two levels above
this file (openswarm/tests -> openswarm -> repo root). In a standalone
openswarm checkout it does not exist and every test skips; in the
phanes tree the tests run for real.

These tests pin only what issue #1481 requires, so a later edit
cannot silently drop a section the agreement depends on: the doc
exists, ends with the AGREED v0.2 marker, is at least 80 lines, and
contains the boundary section, the external-muse handover, and the
hard-constraint keywords autodispatch and openswarm.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "docs" / "fusion-openbot-openswarm-v0.2.md"


def _text():
    return DOC.read_text(encoding="utf-8")


@unittest.skipUnless(DOC.exists(), "fusion agreement doc only exists in the phanes tree")
class FusionAgreedV02DocTest(unittest.TestCase):
    def test_doc_exists(self):
        self.assertTrue(DOC.is_file(), "docs/fusion-openbot-openswarm-v0.2.md missing")

    def test_doc_has_at_least_80_lines(self):
        lines = _text().splitlines()
        self.assertGreaterEqual(len(lines), 80, "agreement doc shrank below 80 lines")

    def test_last_line_is_agreed_marker(self):
        lines = [line for line in _text().splitlines() if line.strip()]
        self.assertEqual(lines[-1].strip(), "AGREED v0.2")

    def test_boundary_section_present(self):
        text = _text()
        self.assertTrue(
            ("边界" in text) or ("boundary" in text),
            "agreement doc lost its boundary section",
        )

    def test_handover_names_external_muse(self):
        self.assertIn("external-muse", _text())

    def test_hard_constraint_keywords_present(self):
        text = _text()
        self.assertIn("autodispatch", text)
        self.assertIn("openswarm", text)

    def test_p0_checklist_present(self):
        text = _text()
        self.assertIn("P0", text)
        self.assertIn("- [ ]", text, "P0 exit list must stay a checkable list")

    def test_downtime_mapping_present(self):
        text = _text()
        self.assertIn("PAUSE-MINT", text)
        self.assertIn("继续跑完", text)

    def test_settled_clauses_present(self):
        text = _text()
        for needle in ("私密 bus 仓", "自托管", "9910", "PolyForm"):
            self.assertIn(needle, text, f"settled clause lost {needle!r}")


if __name__ == "__main__":
    unittest.main()
