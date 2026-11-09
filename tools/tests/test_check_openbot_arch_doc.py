"""Offline contracts for the OpenBot architecture-document drift check (Refs #18).

The check reads the architecture notes of the OpenBot tree and reports every
repository-relative path they name while the tree does not hold it. The fixtures here
are local trees, so the check is exercised without a network or a release.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "check-openbot-arch-doc.py"
SPEC = importlib.util.spec_from_file_location("check_openbot_arch_doc", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
REPO_ROOT = Path(__file__).resolve().parents[2]


def write_tree(root: Path, notes: dict) -> Path:
    """Write a small OpenBot tree whose architecture notes are the given mapping."""
    tree = root / "volto"
    (tree / "docs" / "architecture").mkdir(parents=True)
    (tree / "src" / "backend").mkdir(parents=True)
    (tree / "apps").mkdir(parents=True)
    (tree / "src" / "backend" / "volto-database.ts").write_text("export const schema = 1;\n", encoding="utf-8")
    for name, text in notes.items():
        document = tree / "docs" / "architecture" / name
        document.write_text(text, encoding="utf-8")
    return tree


class RuleTests(unittest.TestCase):
    def test_only_two_segment_paths_of_the_tree_count(self):
        roots = MODULE.top_level_roots(REPO_ROOT / "volto")
        self.assertTrue(MODULE.is_repo_relative("src/backend/volto-database.ts", roots))
        self.assertTrue(MODULE.is_repo_relative("apps/auth-api/src/lib/news.ts", roots))
        self.assertFalse(MODULE.is_repo_relative("agent/tool-catalog.ts", roots))
        self.assertFalse(MODULE.is_repo_relative("src/main/<name>-service.ts", roots))
        self.assertFalse(MODULE.is_repo_relative("https://example.com/src/main/index.ts", roots))
        self.assertFalse(MODULE.is_repo_relative("~/.config/opencode/config.json", roots))

    def test_link_targets_keep_the_path_and_drop_everything_else(self):
        self.assertEqual(MODULE.link_target("../../PRIVACY.md"), "../../PRIVACY.md")
        self.assertEqual(MODULE.link_target("database-migrations.md#what-the-fork-appends"),
                         "database-migrations.md")
        self.assertEqual(MODULE.link_target("https://example.com/x"), "")
        self.assertEqual(MODULE.link_target("/etc/hosts"), "")

    def test_a_marked_open_question_is_reported_but_not_a_finding(self):
        line = "The path `apps/desktop` (待核实: the repository has no such directory) is a counterexample."
        spans = MODULE.line_spans(line, MODULE.top_level_roots(REPO_ROOT / "volto"))
        self.assertEqual([span[2] for span in spans], ["apps/desktop"])
        self.assertTrue(MODULE.is_marked_todo(line, spans, 0))
        self.assertFalse(MODULE.is_marked_todo(line.replace(MODULE.TODO_MARKER, ""), spans, 0))


class TreeCheckTests(unittest.TestCase):
    def check(self, notes: dict) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            tree = write_tree(Path(directory), notes)
            return MODULE.check(tree)

    def test_a_path_the_tree_holds_is_no_finding(self):
        report = self.check({"topic.md": "The runner lives in `src/backend/volto-database.ts`.\n"})
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["todo"], [])
        self.assertEqual(report["referenced"], 1)

    def test_a_path_the_tree_does_not_hold_is_a_finding(self):
        report = self.check({"topic.md": "The runner lives in `src/backend/missing.ts`.\n"})
        self.assertEqual(len(report["missing"]), 1)
        self.assertIn("missing path `src/backend/missing.ts`", report["missing"][0])

    def test_a_renamed_file_is_reported_with_the_file_the_tree_holds(self):
        report = self.check({"topic.md": "The runner lives in `src/backend/openbot-database.ts`.\n"})
        self.assertEqual(len(report["missing"]), 1)
        self.assertIn("the tree holds src/backend/volto-database.ts", report["missing"][0])

    def test_a_marked_open_question_does_not_fail_the_check(self):
        report = self.check({"topic.md": "The path `apps/desktop` (待核实) is a counterexample.\n"})
        self.assertEqual(report["missing"], [])
        self.assertEqual(len(report["todo"]), 1)

    def test_a_broken_link_target_is_a_finding(self):
        report = self.check({"topic.md": "See [missing](./missing.md) for the details.\n"})
        self.assertEqual(len(report["missing"]), 1)
        self.assertIn("missing link target `./missing.md`", report["missing"][0])


class ShippedTreeTests(unittest.TestCase):
    def test_the_shipped_architecture_notes_name_paths_the_tree_holds(self):
        report = MODULE.check(REPO_ROOT / "volto")
        self.assertEqual(report["missing"], [], "\n".join(report["missing"]))
        self.assertGreater(report["referenced"], 50)


class CliTests(unittest.TestCase):
    def run_cli(self, *options):
        return subprocess.run([sys.executable, str(SCRIPT), *options],
                              capture_output=True, text=True, check=False)

    def test_the_shipped_tree_passes(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("0 missing path(s)", result.stdout)

    def test_a_drifted_tree_fails_with_the_finding(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = write_tree(Path(directory), {"topic.md": "See `src/backend/missing.ts`.\n"})
            result = self.run_cli("--tree", str(tree))
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing path `src/backend/missing.ts`", result.stdout)

    def test_json_report_names_the_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = write_tree(Path(directory), {"topic.md": "See `src/backend/missing.ts`.\n"})
            result = self.run_cli("--tree", str(tree), "--json")
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(len(report["missing"]), 1)
        self.assertEqual(report["todo"], [])
        self.assertEqual(report["referenced"], 1)

    def test_self_test(self):
        result = self.run_cli("--self-test")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
