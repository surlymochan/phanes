"""Regression tests for the fusion agreement document (phanes #1473).

The agreement (`docs/fusion-openbot-openswarm-v0.2.md`, AGREED in
surlymochan/phanes#1468) is a governance artifact: its value is that the
boundary table, the P0 exit checklist, the downtime semantics, the
dispatch handover and the settled clauses stay present and pinned.
These tests pin the machine-checkable invariants of that document so a
later edit cannot silently drop a section the agreement depends on.

The document lives at the phanes repository root, four levels above
this file.  In a standalone openswarm checkout it does not exist and
every test skips; in the phanes tree (which vendors openswarm) the
tests run for real.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DOC = REPO_ROOT / "docs" / "fusion-openbot-openswarm-v0.2.md"


def _text():
    return DOC.read_text(encoding="utf-8")


@unittest.skipUnless(DOC.exists(), "fusion agreement doc only exists in the phanes tree")
class FusionAgreementDocTest(unittest.TestCase):
    def test_doc_has_at_least_80_lines(self):
        lines = _text().splitlines()
        self.assertGreaterEqual(len(lines), 80, "agreement doc shrank below the agreed 80 lines")

    def test_last_line_is_agreed_marker(self):
        lines = [ln for ln in _text().splitlines() if ln.strip()]
        self.assertEqual(lines[-1].strip(), "AGREED v0.2")

    def test_boundary_table_present(self):
        text = _text()
        self.assertIn("边界表", text)
        self.assertIn("boundary", text)
        # The three write-authority questions the table must answer.
        for needle in ("lane-claimed", "Run", "stop"):
            self.assertIn(needle, text, f"boundary table lost {needle!r}")

    def test_p0_checklist_present_and_current(self):
        text = _text()
        self.assertIn("P0 出口清单", text)
        self.assertIn("- [ ]", text, "P0 exit list must stay a checkable list")
        # 2026-11-02 baseline hosts must be named as the current topology.
        for host in ("gores1", "gorelay", "gocc"):
            self.assertIn(host, text, f"P0 section lost current host {host}")

    def test_downtime_semantics_cover_pause_mint_and_host_pause(self):
        text = _text()
        self.assertIn("PAUSE-MINT", text)
        self.assertIn("host pause", text)
        self.assertIn("继续跑完", text, "claimed lanes must be stated to run to completion")

    def test_handover_is_option_c_with_two_week_milestones(self):
        text = _text()
        self.assertIn("(c)", text)
        self.assertIn("红线否决", text)
        self.assertIn("第一周", text)
        self.assertIn("第二周", text)

    def test_settled_clauses_present(self):
        text = _text()
        for needle in ("私密 bus 仓", "自托管", "9910", "公约瘦身", "PolyForm", "OpenBot 作壳"):
            self.assertIn(needle, text, f"settled clause lost {needle!r}")

    def test_openswarm_stays_sole_execution_owner(self):
        text = _text()
        self.assertIn("唯一主人", text)
        self.assertIn("平行调度器", text, "the no-second-scheduler rule must stay explicit")


if __name__ == "__main__":
    unittest.main()
