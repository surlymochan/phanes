"""Pattern matching, proposal generation and retirement checks use local fixtures only."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load

xs = _load.load()


class PatternsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-patterns-")
        self.root = Path(self.tmp.name)
        self.knowledge = self.root / "knowledge"
        self.skills = self.knowledge / "skills"
        self.skills.mkdir(parents=True)
        self.proposals = self.knowledge / "proposals"
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.data = self.root / "data"
        self.ledger = self.data / "ledger.jsonl"
        self.policy = {"knowledge": {"repo": str(self.knowledge)}}
        self.constants = {key: getattr(xs, key) for key in ("DATA", "LEDGER", "RUNS", "HOME", "NOW")}
        xs.DATA, xs.LEDGER, xs.RUNS, xs.HOME = str(self.data), str(self.ledger), str(self.runs), str(self.root)
        fixed = dt.datetime(2026, 10, 27, 12, tzinfo=dt.timezone.utc).timestamp()
        xs.NOW = lambda: fixed

    def tearDown(self):
        for key, value in self.constants.items():
            setattr(xs, key, value)
        self.tmp.cleanup()

    def test_parse_pattern_requires_yaml_front_matter(self):
        with self.assertRaisesRegex(ValueError, "^pattern must start with YAML front matter$"):
            xs.parse_pattern_markdown("id: P-0001\n")

    def test_parse_pattern_requires_closed_front_matter(self):
        with self.assertRaisesRegex(ValueError, "^pattern front matter is not closed$"):
            xs.parse_pattern_markdown("---\nid: P-0001\n")

    def test_parse_pattern_rejects_tabs(self):
        text = "---\nid: P-0001\n\ttitle: Tabbed\n---\nBody\n"
        with self.assertRaisesRegex(ValueError, "line 3: tabs are not supported"):
            xs.parse_pattern_markdown(text)

    def test_parse_pattern_rejects_duplicate_top_level_keys(self):
        text = "---\nid: P-0001\nid: P-0002\n---\nBody\n"
        with self.assertRaisesRegex(ValueError, "line 3: duplicate key 'id'"):
            xs.parse_pattern_markdown(text)

    def test_parse_pattern_rejects_unsupported_applies_to_key(self):
        text = "---\napplies_to:\n  audience: [developers]\n---\nBody\n"
        with self.assertRaisesRegex(ValueError, "line 3: unsupported applies_to key 'audience'"):
            xs.parse_pattern_markdown(text)

    def test_parse_pattern_rejects_id_with_whitespace(self):
        for bad_id in ('"P-0001 "', '" P-0001"', "P- 0001", '"P-00 01"', '"P 0001"'):
            text = (
                "---\n"
                f"id: {bad_id}\n"
                "title: \"Invalid ID with whitespace\"\n"
                "applies_to:\n"
                "source: \"PR #556\"\n"
                "status: active\n"
                "---\n"
                "Body\n"
            )
            with self.subTest(bad_id=bad_id):
                with self.assertRaisesRegex(ValueError, "^id must use the P-0001 pattern ID format$"):
                    xs.parse_pattern_markdown(text)

    def test_parse_pattern_rejects_id_with_path_separators(self):
        for bad_id in ('"P-0001/nested"', '"../P-0001"', '"/P-0001"', "P-0001/sub", '"P-0001/"',
                       "'P-0001\\nested'", "'..\\P-0001'", "'\\P-0001'", "'P-0001\\'"):
            text = (
                "---\n"
                f"id: {bad_id}\n"
                "title: \"Invalid ID with path separator\"\n"
                "applies_to:\n"
                "source: \"PR #556\"\n"
                "status: active\n"
                "---\n"
                "Body\n"
            )
            with self.subTest(bad_id=bad_id):
                with self.assertRaisesRegex(ValueError, "^id must use the P-0001 pattern ID format$"):
                    xs.parse_pattern_markdown(text)

    def test_parse_pattern_extracts_metadata_and_body(self):
        text = (
            "---\n"
            "id: P-0042\n"
            "title: \"Use the parser\"\n"
            "applies_to:\n"
            "  card_types: [fix, feature]\n"
            "  paths: [openswarm/**]\n"
            "  backends: [codex]\n"
            "source: \"PR #459\"\n"
            "status: active\n"
            "---\n"
            "# Pattern\n\nValidate the input.\n"
        )
        self.assertEqual(xs.parse_pattern_markdown(text), {
            "id": "P-0042",
            "title": "Use the parser",
            "applies_to": {
                "card_types": ["fix", "feature"],
                "paths": ["openswarm/**"],
                "backends": ["codex"],
            },
            "source": "PR #459",
            "status": "active",
            "body": "# Pattern\n\nValidate the input.",
        })

    def test_pattern_next_id_scans_ids_and_skips_unreadable_files(self):
        self.assertEqual(xs.pattern_next_id(str(self.knowledge)), 1)

        (self.skills / "nested").mkdir()
        (self.skills / "nested" / "P-7.md").write_text("id: P-7\n", encoding="utf-8")
        self.proposals.mkdir()
        quoted_id = self.proposals / "P-12.md"
        quoted_id.write_text('id: "P-12"\n', encoding="utf-8")
        (self.proposals / "body-id.md").write_text("text id: P-99\n", encoding="utf-8")
        unreadable = self.skills / "unreadable.md"
        unreadable.write_text("id: P-100\n", encoding="utf-8")

        original_open = open

        def open_for_test(path, *args, **kwargs):
            if os.fspath(path) == os.fspath(unreadable):
                raise OSError("fixture is unreadable")
            return original_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=open_for_test):
            self.assertEqual(xs.pattern_next_id(str(self.knowledge)), 13)

    def test_pattern_existing_sources_reads_proposal_front_matter_only(self):
        self.proposals.mkdir()
        nested = self.proposals / "nested"
        nested.mkdir()
        (nested / "valid.md").write_text("---\nsource: alpha\n---\nBody\n", encoding="utf-8")
        (self.proposals / "quoted.md").write_text('---\nsource: "beta"\n---\nBody\n', encoding="utf-8")
        (self.proposals / "no-front-matter.md").write_text("source: ignored\n", encoding="utf-8")
        (self.proposals / "unclosed.md").write_text("---\ntitle: Unclosed\n", encoding="utf-8")
        (self.proposals / "no-source.md").write_text("---\ntitle: No source\n---\nBody\n", encoding="utf-8")
        (self.proposals / "ignored.txt").write_text("---\nsource: ignored-extension\n---\n", encoding="utf-8")
        (self.skills / "skill.md").write_text("---\nsource: ignored-skill\n---\n", encoding="utf-8")

        self.assertEqual(xs.pattern_existing_sources(str(self.knowledge)), {"alpha", "beta"})

    def write_pattern(self, pattern_id, title, body="When to use: common change.\nPattern:\n- Validate input.\n- Test boundary cases.\nExample: [PR](https://example.invalid/pr/1)",
                      card_types="[]", paths="[]", backends="[]", status="active"):
        path = self.skills / f"{pattern_id}.md"
        path.write_text(
            f"---\nid: {pattern_id}\ntitle: {title}\napplies_to:\n"
            f"  card_types: {card_types}\n  paths: {paths}\n  backends: {backends}\n"
            f"source: \"PR #1\"\nstatus: {status}\n---\n{body}\n", encoding="utf-8")
        return path

    def run_command(self, *args):
        old_argv = sys.argv
        output = io.StringIO()
        try:
            sys.argv = ["openswarm", *args]
            with mock.patch.object(xs, "load_policy", return_value=self.policy), contextlib.redirect_stdout(output):
                xs.main()
        finally:
            sys.argv = old_argv
        return output.getvalue()

    def test_selection_matches_and_caps_three_patterns_and_1500_characters(self):
        for number in range(1, 5):
            self.write_pattern(f"P-{number:04d}", f"Pattern {number}", card_types="[fix]",
                               paths="[src/**]", backends="[codex]")
        self.write_pattern("P-0005", "Wrong card", card_types="[docs]")
        self.write_pattern("P-0006", "Retired", status="retired")
        block, ids = xs.select_patterns(self.policy, "fix", "codex", paths=["src/file.py"])
        self.assertEqual(ids, ["P-0001", "P-0002", "P-0003"])
        self.assertLessEqual(len(block), 1500)
        self.assertNotIn("Wrong card", block)
        self.assertNotIn("Retired", block)

        (self.skills / "P-0001.md").write_text(
            (self.skills / "P-0001.md").read_text(encoding="utf-8").replace("- Validate input.", "x" * 2000),
            encoding="utf-8")
        block, ids = xs.select_patterns(self.policy, "fix", "codex", paths=["src/file.py"])
        self.assertLessEqual(len(block), xs.PATTERNS_MAX_CHARS)
        self.assertEqual(ids[0], "P-0001")

    def test_selection_returns_empty_block_and_ids_for_empty_skills_directory(self):
        block, ids = xs.select_patterns(self.policy, "fix", "codex", paths=["src/file.py"])
        self.assertEqual(block, "")
        self.assertEqual(ids, [])

    def test_pattern_apply_paths_normalizes_and_collapses_paths(self):
        cases = (
            (["README.md"], ["README.md"]),
            (["src/file.py"], ["src/**"]),
            (["src/one.py", "src/two.py"], ["src/**"]),
            (["src/one.py", "tests/two.py"], []),
            (["./src\\file.py"], ["src/**"]),
            (["", None, "src/file.py"], ["src/**"]),
        )
        for paths, expected in cases:
            with self.subTest(paths=paths):
                self.assertEqual(xs.pattern_apply_paths(paths), expected)

    def test_preview_shows_matching_lessons_and_patterns(self):
        self.write_pattern("P-0010", "Preview pattern", card_types="[fix]", backends="[codex]")
        (self.knowledge / "lessons").mkdir()
        (self.knowledge / "lessons" / "L-0010.yaml").write_text(
            "id: L-0010\nrule: Preview lesson\nguard_level: L2\napplies_to:\n"
            "  card_types: [fix]\n  paths: []\n  backends: [codex]\n", encoding="utf-8")
        output = self.run_command("lessons", "preview", "--card-type", "fix", "--backend", "codex")
        self.assertIn("[L-0010] Preview lesson", output)
        self.assertIn("[P-0010] Preview pattern", output)

    def test_ledger_normalization_preserves_patterns_injected(self):
        xs.append_ledger({"id": "run-1", "patterns_injected": ["P-0001"]})
        entry = json.loads(self.ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["patterns_injected"], ["P-0001"])
        xs.append_ledger({"id": "run-2"})
        entry = json.loads(self.ledger.read_text(encoding="utf-8").splitlines()[1])
        self.assertEqual(entry["patterns_injected"], [])

    def test_propose_converts_compare_winner_proposal_without_editing_skills(self):
        source_dir = self.proposals / "2026-10-26"
        source_dir.mkdir(parents=True)
        source = source_dir / "pattern-cmp-123.md"
        source.write_text("# Pattern proposal from compare cmp-123\n\n## Proposed pattern\n\nReview the winner.\n",
                          encoding="utf-8")
        run_dir = self.runs / "cmp-123"
        run_dir.mkdir()
        (run_dir / "compare.json").write_text(json.dumps({
            "id": "cmp-123", "created_at": dt.datetime.fromtimestamp(xs.NOW() - 60, dt.timezone.utc).isoformat(),
            "title": "Update the archive parser", "card_type": "fix", "winner_label": "A",
            "diff_notes": ["Winner rejects traversal paths before extraction."],
            "runs": {"a": {"backend": "codex", "run_id": "winner-1",
                            "result": {"result": {"files_changed": ["src/archive.py"]}}}}}),
            encoding="utf-8")
        original_skills = list(self.skills.iterdir())
        output = json.loads(self.run_command("patterns", "propose", "--since", "30"))
        self.assertEqual(output["count"], 1)
        draft = Path(output["proposals"][0])
        text = draft.read_text(encoding="utf-8")
        self.assertIn("proposal_type: pattern_draft", text)
        self.assertIn('source: "compare cmp-123"', text)
        self.assertIn('card_types: ["fix"]', text)
        self.assertIn('backends: ["codex"]', text)
        self.assertIn("Winner rejects traversal paths", text)
        self.assertIn("Example: [compare proposal](../2026-10-26/pattern-cmp-123.md)", text)
        self.assertEqual(list(self.skills.iterdir()), original_skills)

    def test_propose_collects_repeated_peer_review_findings(self):
        for run_id in ("review-one", "review-two"):
            run_dir = self.runs / run_id
            run_dir.mkdir()
            (run_dir / "review.json").write_text(json.dumps({"findings": [{
                "severity": "P1", "path": "src/archive.py", "line": 10,
                "title": "Validate archive paths", "body": "Reject paths outside the extraction root."}]}),
                encoding="utf-8")
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text("".join(json.dumps({"id": run_id, "ts": xs.NOW() - 60,
                                                     "backend": "codex", "card_type": "fix"}) + "\n"
                                       for run_id in ("review-one", "review-two")), encoding="utf-8")
        output = json.loads(self.run_command("patterns", "propose"))
        self.assertEqual(output["count"], 1)
        text = Path(output["proposals"][0]).read_text(encoding="utf-8")
        self.assertIn("review finding", text)
        self.assertIn("Validate archive paths", text)

    def test_retire_check_requires_eight_runs_in_both_cohorts(self):
        self.write_pattern("P-0020", "Old pattern")
        entries = []
        for index in range(8):
            entries.append({"id": f"injected-{index}", "ts": xs.NOW() - 60, "card_type": "fix",
                            "backend": "codex", "patterns_injected": ["P-0020"], "exec_pass": False,
                            "accepted": False})
        for index in range(8):
            entries.append({"id": f"control-{index}", "ts": xs.NOW() - 60, "card_type": "fix",
                            "backend": "codex", "patterns_injected": [], "exec_pass": True,
                            "accepted": True})
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
        output = json.loads(self.run_command("patterns", "retire-check"))
        self.assertEqual(len(output["retirement_proposals"]), 1)
        self.assertIn("retire-P-0020.md", output["retirement_proposals"][0])

        self.ledger.write_text("".join(json.dumps(entry) + "\n" for entry in entries if not entry["id"] == "injected-7"),
                               encoding="utf-8")
        output = json.loads(self.run_command("patterns", "retire-check"))
        self.assertEqual(output["retirement_proposals"], [])
        self.assertEqual(output["insufficient_samples"], 1)

    def test_retire_check_without_history_leaves_patterns_unchanged(self):
        pattern_path = self.write_pattern("P-0030", "Active pattern")
        original_content = pattern_path.read_text(encoding="utf-8")
        original_skills = sorted(p.name for p in self.skills.iterdir())

        output = json.loads(self.run_command("patterns", "retire-check"))
        self.assertEqual(output["retirement_proposals"], [])
        self.assertEqual(output["patterns_checked"], 1)
        self.assertEqual(output["insufficient_samples"], 1)
        self.assertFalse(self.proposals.exists())
        self.assertEqual(sorted(p.name for p in self.skills.iterdir()), original_skills)
        self.assertEqual(pattern_path.read_text(encoding="utf-8"), original_content)
        parsed = xs.parse_pattern_markdown(pattern_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["status"], "active")

        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps({"id": "other-run", "ts": xs.NOW() - 60,
                                           "card_type": "docs", "backend": "codex",
                                           "patterns_injected": [], "exec_pass": True,
                                           "accepted": True}) + "\n", encoding="utf-8")
        output = json.loads(self.run_command("patterns", "retire-check"))
        self.assertEqual(output["retirement_proposals"], [])
        self.assertEqual(output["patterns_checked"], 1)
        self.assertEqual(output["insufficient_samples"], 1)
        self.assertFalse(self.proposals.exists())
        self.assertEqual(sorted(p.name for p in self.skills.iterdir()), original_skills)
        self.assertEqual(pattern_path.read_text(encoding="utf-8"), original_content)



if __name__ == "__main__":
    unittest.main()
