"""card-lint rejects thin cards and thin PRs, with stable reason codes (Refs #1354).

The three thin cards are the known thin bodies of #560, #568 and #552, kept as
fixtures so the rejection rules stay reproducible.  They are stored under
``tests/fixtures`` next to the other offline fixtures of this suite.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = ROOT / "bin" / "openswarm"
FIXTURES = HERE / "fixtures"
sys.path.insert(0, str(HERE))
import _load
xs = _load.load()

# The known thin cards and the reason codes each of them must be rejected for.
THIN_CARDS = {
    "card_thin_560.md": {"weak_acceptance", "thin_effective_diff"},
    "card_thin_568.md": {"thin_keywords", "weak_acceptance", "thin_effective_diff"},
    "card_thin_552.md": {"thin_effective_diff"},
}


def run_cli(*args, **kwargs):
    """Run the openswarm CLI in a child process and return the completed process."""
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                          timeout=120, **kwargs)


class CardLintTests(unittest.TestCase):
    """A card that promises too little is rejected, and says why."""

    def lint(self, *args, **kwargs):
        result = run_cli("card-lint", *args, **kwargs)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result, json.loads(result.stdout)

    def test_known_thin_cards_are_rejected_with_reason_codes(self):
        for name, expected in THIN_CARDS.items():
            with self.subTest(card=name):
                result, report = self.lint(str(FIXTURES / name))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(report["ok"])
                codes = {reason["code"] for reason in report["reasons"]}
                self.assertTrue(expected.issubset(codes), (name, codes))
                self.assertEqual(set(report["codes"]), codes)
                self.assertIn("card-lint: rejected", result.stderr)

    def test_a_thick_card_is_accepted(self):
        result, report = self.lint(str(FIXTURES / "card_thick.md"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["ok"])
        self.assertEqual(report["reasons"], [])
        self.assertGreaterEqual(report["effective_diff"], report["min_effective_diff"])

    def test_a_card_can_be_read_from_stdin(self):
        text = (FIXTURES / "card_thin_552.md").read_text(encoding="utf-8")
        result, report = self.lint("-", input=text)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(report["source"], "stdin")
        self.assertIn("thin_effective_diff", {reason["code"] for reason in report["reasons"]})

    def test_a_card_without_a_title_or_labels_is_rejected(self):
        card = "Implement the bounded path.\n验收：`python3 -m unittest`\n"
        result, report = self.lint("-", input=card)
        self.assertEqual(result.returncode, 1, result.stderr)
        codes = {reason["code"] for reason in report["reasons"]}
        self.assertIn("missing_title", codes)
        self.assertIn("missing_labels", codes)

    def test_a_missing_card_file_is_an_error_not_a_rejection(self):
        result = run_cli("card-lint", str(FIXTURES / "card-does-not-exist.md"))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stderr)["ok"])

    def test_a_card_and_a_pr_diff_cannot_be_linted_together(self):
        result = run_cli("card-lint", str(FIXTURES / "card_thick.md"), "--pr-diff", "main", "HEAD")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stderr)["ok"])


class CardLintPrDiffTests(unittest.TestCase):
    """A pull request is measured by the effective code lines it adds and removes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openswarm-card-lint-")
        self.addCleanup(self.temp.cleanup)
        self.repo = pathlib.Path(self.temp.name)
        self.git("init", "-q", ".")
        self.git("config", "user.email", "card-lint@example.invalid")
        self.git("config", "user.name", "card lint")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def commit(self, name, path, content):
        (self.repo / path).write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", name)
        return self.git("rev-parse", "HEAD").strip()

    def lint_diff(self, ref):
        result = run_cli("card-lint", "--pr-diff", self.base, ref, "--repo", str(self.repo))
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result, json.loads(result.stdout)

    def test_a_commit_that_only_touches_markdown_is_thin(self):
        ref = self.commit("docs", "README.md",
                          "\n".join(f"# documentation line {i}" for i in range(40)) + "\n")
        result, report = self.lint_diff(ref)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(report["target"], "pr-diff")
        self.assertEqual(report["effective_diff"], 0)
        self.assertIn("thin_pr_diff", {reason["code"] for reason in report["reasons"]})

    def test_a_commit_with_an_implementation_is_accepted(self):
        ref = self.commit("impl", "impl.py",
                          "\n".join(f"value_{i} = {i}" for i in range(40)) + "\n")
        result, report = self.lint_diff(ref)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["ok"])
        self.assertEqual(report["reasons"], [])
        self.assertGreaterEqual(report["effective_diff"], report["min_effective_diff"])

    def test_an_extensionless_bin_script_counts_as_code(self):
        (self.repo / "bin").mkdir()
        ref = self.commit("bin", "bin/tool",
                          "#!/usr/bin/env python3\n" + "\n".join(f"value_{i} = {i}" for i in range(40)) + "\n")
        result, report = self.lint_diff(ref)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(report["effective_diff"], report["min_effective_diff"])

    def test_an_extensionless_file_outside_bin_is_not_code(self):
        self.assertFalse(xs.card_lint_is_code("LICENSE"))
        self.assertFalse(xs.card_lint_is_code("docs/bin.md"))
        self.assertTrue(xs.card_lint_is_code("openswarm/bin/openswarm"))

    def test_a_missing_ref_is_an_error_not_a_rejection(self):
        result = run_cli("card-lint", "--pr-diff", self.base, "no-such-ref", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stderr)["ok"])


class CardLintPolicyTests(unittest.TestCase):
    """The rules are read from the policy, and the defaults stay the autodispatch ones."""

    def test_the_rules_come_from_the_policy(self):
        rules = xs.card_lint_rules({"acceptance": {"min_effective_diff": 5,
                                                  "thin_keywords": ["cache"],
                                                  "weak_acceptance_re": "^ls\\b"}})
        self.assertEqual(rules["min_effective_diff"], 5)
        self.assertEqual(rules["thin_keywords"], ["cache"])
        self.assertEqual(rules["weak_acceptance_re"], "^ls\\b")

    def test_the_defaults_match_the_autodispatch(self):
        rules = xs.card_lint_rules({})
        self.assertEqual(rules["min_effective_diff"], 30)
        self.assertIn("todo", rules["thin_keywords"])
        self.assertIn("rg", rules["weak_acceptance_re"])

    def test_the_shipped_policy_matches_the_defaults(self):
        policy = json.loads((ROOT / "policy.json").read_text(encoding="utf-8"))
        rules = xs.card_lint_rules(policy)
        self.assertEqual(rules["min_effective_diff"], xs.CARD_LINT_DEFAULTS["min_effective_diff"])
        self.assertEqual(rules["thin_keywords"], xs.CARD_LINT_DEFAULTS["thin_keywords"])
        self.assertEqual(rules["weak_acceptance_re"], xs.CARD_LINT_DEFAULTS["weak_acceptance_re"])

    def test_a_policy_floor_lets_a_small_card_through(self):
        card = "# Title\nlabels: p1\nImplement the bounded path.\n验收：`python3 -m unittest`\n"
        reasons, estimate = xs.card_lint_card(card, xs.card_lint_rules({"acceptance": {"min_effective_diff": 1}}))
        self.assertEqual(reasons, [])
        self.assertEqual(estimate, 1)

    def test_a_policy_keyword_list_marks_a_body_as_keyword_only(self):
        card = "# Title\nlabels: p1\ncache\n验收：`python3 -m unittest`\n"
        rules = xs.card_lint_rules({"acceptance": {"thin_keywords": ["cache"], "min_effective_diff": 1}})
        reasons, _ = xs.card_lint_card(card, rules)
        self.assertIn("thin_keywords", {reason["code"] for reason in reasons})

    def test_a_policy_acceptance_pattern_marks_read_only_acceptance(self):
        card = "# Title\nlabels: p1\nImplement the bounded path.\n验收：`ls -R src`\n"
        rules = xs.card_lint_rules({"acceptance": {"weak_acceptance_re": "^ls\\b", "min_effective_diff": 1}})
        reasons, _ = xs.card_lint_card(card, rules)
        self.assertIn("weak_acceptance", {reason["code"] for reason in reasons})


if __name__ == "__main__":
    unittest.main(verbosity=2)
