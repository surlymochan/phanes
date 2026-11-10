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
CLI = ROOT / "bin" / "swrm"
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
    """Run the swrm CLI in a child process and return the completed process."""
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
        self.temp = tempfile.TemporaryDirectory(prefix="swrm-card-lint-")
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
        self.assertTrue(xs.card_lint_is_code("swrm/bin/swrm"))

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


class CardPreReviewTests(unittest.TestCase):
    def test_unmeasurable_acceptance_is_rejected_with_the_item(self):
        card = "# Sample\nlabels: bug\n验收：功能正常\n## 失败模式清单\n参考 docs/defect-patterns.md\n"
        reasons, _ = xs.card_lint_card(card, xs.card_lint_rules({}))
        match = next(reason for reason in reasons if reason["code"] == "untestable_acceptance")
        self.assertIn("功能正常", match["detail"])

    def test_multiline_and_quoted_vague_items_are_rejected(self):
        card = "# T\nlabels: p1\ncache\n验收：`pytest`\n- `功能正常`\n## 失败模式清单\n参考 docs/defect-patterns.md\n"
        reasons, estimate = xs.card_lint_card(card, xs.card_lint_rules({}))
        self.assertTrue(any(r["code"] == "untestable_acceptance" and "功能正常" in r["detail"] for r in reasons))
        self.assertIn("thin_keywords", {r["code"] for r in reasons})
        self.assertEqual(estimate, 1)

    def test_unbulleted_and_command_name_only_items_are_rejected(self):
        card = "# T\nlabels: p1\n验收：`pytest`\n功能正常\n- 使用 python 修复\n## 失败模式清单\n参考 docs/defect-patterns.md\n"
        reasons, _ = xs.card_lint_card(card, xs.card_lint_rules({}))
        details = [r["detail"] for r in reasons if r["code"] == "untestable_acceptance"]
        self.assertTrue(any("功能正常" in detail for detail in details))
        self.assertTrue(any("使用 python 修复" in detail for detail in details))

    def test_commands_and_explicit_conditions_are_measurable(self):
        for item in ("`bun scripts/check.ts`", "bun scripts/check.ts",
                     "`ls docs/backlog/next/*.md | wc -l | grep -qx 20`",
                     "`python3 -m unittest`", '任务总数 等于 20', '接口响应 输出包含 "ok"',
                     "`tool --fixture '中文.json'`"):
            with self.subTest(item=item):
                self.assertTrue(xs.card_lint_measurable(item))

    def test_keywords_and_vague_comparisons_do_not_prove_acceptance(self):
        for item in ("pytest 功能正常", "`pytest 功能正常`", "`功能正常`",
                     "功能正常", "pytest", "输出 应为 正常", "`bun 'unfinished`"):
            with self.subTest(item=item):
                self.assertFalse(xs.card_lint_measurable(item))

    def test_each_bad_item_is_reported_beside_a_valid_command(self):
        card = ("# T\nlabels: p1\ncache\n## Acceptance (measurable)\n"
                "- `bun scripts/check.ts`\n- pytest 功能正常\n- `功能正常`\n"
                "## 失败模式清单\n参考 docs/defect-patterns.md\n")
        reasons, estimate = xs.card_lint_card(card, xs.card_lint_rules({}))
        details = [r["detail"] for r in reasons if r["code"] == "untestable_acceptance"]
        self.assertEqual(len(details), 2)
        self.assertIn("pytest 功能正常", details[0])
        self.assertIn("`功能正常`", details[1])
        self.assertEqual(estimate, 1)
        self.assertIn("thin_keywords", {r["code"] for r in reasons})

    def test_fenced_template_does_not_supply_required_fields(self):
        card = ("# T\nlabels: p1\ncache\n```markdown\n验收：`bun scripts/check.ts`\n"
                "## 失败模式清单\n参考 docs/defect-patterns.md\n```\n")
        reasons, estimate = xs.card_lint_card(card, xs.card_lint_rules({}))
        self.assertTrue({"missing_acceptance", "missing_failure_modes", "thin_keywords"}
                        .issubset({r["code"] for r in reasons}))
        self.assertEqual(estimate, 1)

    def test_missing_failure_modes_is_rejected(self):
        card = "# Sample\nlabels: bug\n验收：`python -m unittest`\n"
        reasons, _ = xs.card_lint_card(card, xs.card_lint_rules({}))
        self.assertIn("missing_failure_modes", {reason["code"] for reason in reasons})

    def test_a_policy_floor_lets_a_small_card_through(self):
        card = "# Title\nlabels: p1\nImplement the bounded path.\n验收：`python3 -m unittest`\n## 失败模式清单\n参考 docs/defect-patterns.md\n"
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
