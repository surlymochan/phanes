"""Regression tests for P5 card quality checks at the AD dispatch boundary."""
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("autodispatch.py")
SPEC = importlib.util.spec_from_file_location("autodispatch_card_quality", SCRIPT)
AD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AD)


TREE = {"README.md", "tools/tests/test_sample.py"}
BRANCHES = {"feature/foo"}


def fake_repo_sh(cmd, **kwargs):
    """A repository with a known tree and branches; any other git call answers like the real one."""
    cmd = list(cmd)
    if "/proc/meminfo" in " ".join(cmd):
        return 0, "8388608", ""
    if "ls-tree" in cmd:
        path = cmd[-1]
        return 0, (path + "\n" if path in TREE else ""), ""
    if "rev-parse" in cmd:
        ref = cmd[-1].split("^")[0]
        return (0, "a" * 40, "") if ref.replace("origin/", "", 1) in BRANCHES | {"main"} else (1, "", "")
    return 0, "a" * 40 + "\trefs/heads/main\n", ""


class CardQualityTickTests(unittest.TestCase):
    def run_tick(self, body):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        AD.D = str(root / "autodispatch")
        AD.BRAIN_LOG = str(root / "brain" / "log.md")
        Path(AD.D).mkdir()
        Path(AD.BRAIN_LOG).parent.mkdir()

        issue = {"number": 1853, "title": "质量检查样本", "state": "OPEN",
                 "labels": [{"name": "lane-ready"}], "body": body}
        edits = []

        def gh(args, timeout=60):
            if args[:3] == ["issue", "list", "--state"]:
                return 0, json.dumps([issue], ensure_ascii=False), ""
            if args[:2] == ["issue", "view"]:
                return 0, json.dumps(issue, ensure_ascii=False), ""
            if args[:2] == ["issue", "edit"]:
                edits.append(args)
                return 0, "", ""
            self.fail(f"unexpected gh call: {args}")

        lanes = {"CX2": {"host": "local", "backend": "codex"}}
        state = {}
        dispatched = []
        patches = [
            mock.patch.object(AD, "audit_switches"),
            mock.patch.object(AD, "paused_scopes", return_value=set()),
            mock.patch.object(AD, "host_ok", return_value=True),
            mock.patch.object(AD, "remote", return_value={"lane": "idle"}),
            mock.patch.object(AD, "host_good"),
            mock.patch.object(AD, "update_busy_stale"),
            mock.patch.object(AD, "gh", side_effect=gh),
            mock.patch.object(AD, "label_cache_reset"),
            mock.patch.object(AD, "prefetch_labels"),
            mock.patch.object(AD, "label_state", return_value=("fresh", issue)),
            mock.patch.object(AD, "sh", side_effect=fake_repo_sh),
            mock.patch.object(AD, "dispatch", side_effect=lambda *args: dispatched.append(args[2]) or ("run", None)),
            mock.patch.object(AD, "save_state"),
            mock.patch.object(AD, "log"),
            mock.patch.object(AD, "freshen_queue", side_effect=lambda queue, *_: queue),
            mock.patch.object(AD, "replenish", return_value=[]),
            mock.patch.object(AD, "status_snapshot", return_value={}),
            mock.patch.object(AD, "LOW", 0),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch("builtins.open", side_effect=lambda *args, **kwargs: io.StringIO()):
            AD.tick(lanes, state, 0)
        return dispatched, edits

    def test_bad_cards_are_returned_before_tick_dispatch(self):
        samples = {
            "cross-host path adjacent to Chinese text": "读取govpn:/data/config 并验证：\n```\npytest -q\n```",
            "absolute path outside the checkout": "读取/etc/passwd 并验证：\n```\npytest -q\n```",
            "missing ordinary file reference": "读取missing/file.py 并运行：\n```\npytest -q\n```",
            "missing repository-root file reference": "读取missing.py 并运行：\n```\npytest -q\n```",
            "ambiguous acceptance without executable command": "验收：通过或至少完成检查。",
            "non-command code block": "验收：检查结果正确。\n```json\n{\"ok\": true}\n```",
            "vague acceptance directly followed by a command": "验收：功能正常\n```sh\npytest -q\n```",
            "command with a manual alternative": "验收：pytest -q 或人工确认",
            "help-only command": "验收：\n```sh\npytest --help\n```",
            "collect-only command": "验收：\n```sh\npytest --collect-only\n```",
            "vague acceptance masked by a command elsewhere": "验收：功能正常\n\n其它说明。\n```sh\npytest -q\n```",
            "missing branch named before the word": "基于 missing-branch 分支修改代码\npytest -q",
            "missing test file argument": "验收：\n```sh\npytest missing_test.py\n```",
            "local mention does not excuse a cross-host reference": "本机与 govpn 交换数据\n```sh\npytest -q\n```",
        }
        for name, body in samples.items():
            with self.subTest(name=name):
                dispatched, edits = self.run_tick(body)
                self.assertEqual(dispatched, [], "low-quality card reached dispatch()")
                self.assertTrue(any("--remove-label" in edit and "lane-ready" in edit for edit in edits),
                                "rejected card was not returned to the backlog")

    def test_unmerged_but_existing_branch_and_a_failing_git_query_do_not_return_the_card(self):
        body = "基于分支 `feature/foo`，读取 README.md。\n```sh\npython3 -m unittest tools.tests.test_sample\n```"
        dispatched, edits = self.run_tick(body)
        self.assertEqual(len(dispatched), 1)
        with mock.patch.object(AD, "sh", side_effect=lambda cmd, **k: (128, "", "fatal: transient")):
            self.assertIsNone(AD.card_quality(body))

    def test_a_confirming_acceptance_with_a_command_reaches_dispatch(self):
        dispatched, edits = self.run_tick("验收：确认所有测试通过\n```sh\npytest -q\n```")
        self.assertEqual(len(dispatched), 1)
        self.assertFalse(any("--remove-label" in e and "lane-ready" in e for e in edits))

    def test_valid_card_reaches_dispatch_without_review_round_mutation(self):
        body = ("基于分支 `feature/foo`，新增 `new_file.py`。验收：所有测试通过。\n\n"
                "```sh\npython3 -m unittest tools.tests.test_sample\n```")
        dispatched, edits = self.run_tick(body)
        self.assertEqual(len(dispatched), 1)
        self.assertFalse(any("review" in " ".join(edit).lower() for edit in edits))


if __name__ == "__main__":
    unittest.main()
