"""Exercise replenish -> real CLI preflight -> issue transport and import state.

Only remote Git/GitHub transports and state persistence are replaced. Generated
cards retain their real shape (# title, labels, body), and the validator runs as
an actual subprocess; a rejected card cannot reach the issue-create transport.
"""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "card_replenish", ROOT / "tools/lane-autodispatch/autodispatch.py")
AD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AD)


def card(check="`bun scripts/check.ts`", failure=True):
    body = "\n".join(f"Repair scripts/check.ts case {i} with a fixture verifying its output."
                     for i in range(35))
    return (f"# Generated card\nlabels: p1\n{body}\n验收：{check}\n"
            + ("## 失败模式清单\n参考 docs/defect-patterns.md\n" if failure else ""))


class ReplenishPreflightTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.created = []
        self.logs = []
        self.repo_cards = {}
        self.create_rc = 0
        for name, value in (("D", str(self.home)), ("LOCAL", str(ROOT)),
                            ("sh", self.git), ("gh", self.github),
                            ("save_state", lambda state: None),
                            ("log", lambda message, **kw: self.logs.append(message))):
            patcher = patch.object(AD, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, args, **kwargs):
        if "fetch" in args:
            return 0, "", ""
        if "ls-tree" in args:
            return 0, "\n".join(self.repo_cards), ""
        if "show" in args:
            return 0, self.repo_cards[args[-1].split(":", 1)[1]], ""
        self.fail(f"unexpected git transport: {args}")

    def github(self, args):
        if args[:2] == ["issue", "create"]:
            if self.create_rc:
                return self.create_rc, "", "creation failed"
            self.created.append(args)
            return 0, f"https://example.invalid/issues/{len(self.created)}", ""
        if args[:2] == ["issue", "list"]:
            return 0, "[]", ""
        self.fail(f"unexpected issue transport: {args}")

    def test_local_rejection_preserves_card_and_reports_item(self):
        backlog = self.home / "backlog"
        backlog.mkdir()
        source = backlog / "bad.md"
        source.write_text(card("pytest 功能正常"))
        state = {}
        self.assertEqual(AD.replenish(1, state), [])
        self.assertTrue(source.exists())
        self.assertEqual(self.created, [])
        self.assertIn("pytest 功能正常", "\n".join(self.logs))
        source.write_text(card(failure=False))
        self.assertEqual(AD.replenish(1, state), [])
        self.assertIn("missing_failure_modes", "\n".join(self.logs))
        source.write_text(card())
        self.assertEqual(AD.replenish(1, state), ["#1"])
        self.assertFalse(source.exists())
        self.assertTrue((backlog / "filed/bad.md").exists())

    def test_repo_generation_rejects_then_imports_only_after_success(self):
        bad = "docs/backlog/next/01-bad.md"
        good = "docs/backlog/next/02-good.md"
        self.repo_cards = {bad: card("`功能正常`"), good: card()}
        state = {}
        self.assertEqual(AD.replenish(1, state), ["#1"])
        self.assertEqual(state["_imported"], [good])
        self.assertEqual(len(self.created), 1)
        self.assertIn("docs/defect-patterns.md", self.created[0][self.created[0].index("-b") + 1])
        self.repo_cards[bad] = card("`ls docs/backlog/next/*.md | wc -l | grep -qx 20`")
        self.create_rc = 1
        AD.replenish(1, state)
        self.assertEqual(state["_imported"], [good])
        self.create_rc = 0
        self.assertEqual(AD.replenish(1, state), ["#2"])
        self.assertEqual(state["_imported"], [good, bad])

    def test_production_planner_prompt_passes_and_carries_requirements(self):
        state = {}
        self.assertEqual(AD.replenish(1, state), ["#1(planner)"])
        body = self.created[0][self.created[0].index("-b") + 1]
        for text in ("docs/defect-patterns.md", "swrm/bin/swrm card-lint",
                     "复刻的引擎数据形状清单", "真实形状 fixture", "tick/集成级测试",
                     "单后端 / drain / 暂停 / 已派发后变化 / 合并后清理 / dry-run 无副作用"):
            self.assertIn(text, body)
        # Simulate the planner's merged output entering the next replenish call.
        self.repo_cards = {"docs/backlog/next/generated.md": card()}
        self.assertEqual(AD.replenish(1, state), ["#2"])
        self.assertEqual(state["_imported"], list(self.repo_cards))


if __name__ == "__main__":
    unittest.main()
