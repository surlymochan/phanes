import importlib.util
from pathlib import Path
from unittest import TestCase, mock

spec = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
ad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ad)


class CardQualityTest(TestCase):
    def test_samples(self):
        command = "\n```sh\npytest tests\n```"
        with mock.patch.object(ad, "sh", return_value=(1, "", "")):
            for body in ("读取 govpn:/workspace/repo" + command,
                         "分支 `missing-branch`" + command,
                         "验收：通过或人工确认",
                         "验收：功能正常",
                         "读取 /home/other/file" + command,
                         "读取 `missing/file.py`" + command):
                with self.subTest(body=body):
                    self.assertIsNotNone(ad.card_quality(body))
        self.assertIsNone(ad.card_quality("验收：运行测试" + command))

    def test_rejection_removes_only_ready_and_logs(self):
        issue = '[{"number":1,"title":"x","body":"验收：正常","labels":["lane-ready"]}]'
        with mock.patch.object(ad, "gh", side_effect=[(0, issue, ""), (0, "", "")]) as gh, \
             mock.patch.object(ad, "log") as log:
            self.assertEqual(ad.queue(), [])
            gh.assert_called_with(["issue", "edit", "1", "--remove-label", "lane-ready"])
            self.assertIn("退回", log.call_args.args[0])

    def test_review_regressions(self):
        valid = "\n./scripts/test.sh"
        with mock.patch.object(ad, "sh", return_value=(1, "", "")):
            for body in ("读取/etc/passwd", "读取/etc", "读取missing/file.py",
                         "读取 missing/file.py 并新增 output.py"):
                with self.subTest(body=body):
                    self.assertIsNotNone(ad.card_quality(body + valid))
            for body in ("git status", "python", "```json\n{\"test\": true}\n```"):
                self.assertIsNotNone(ad.card_quality(body))
            self.assertIsNone(ad.card_quality("新增 output.py" + valid))
        with mock.patch.object(ad, "sh", return_value=(0, "", "")):
            self.assertIsNone(ad.card_quality("分支 `feature/foo`" + valid))

    def test_tick_rejects_bad_card_and_dispatches_good_card(self):
        import json
        issues = [dict(number=1, title="bad", body="读取/etc", labels=["lane-ready"]),
                  dict(number=2, title="good", body="验收\n./scripts/test.sh", labels=["lane-ready"])]
        edits = []
        def gh(args, **kwargs):
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps(issues), ""
            edits.append(args)
            return 0, "", ""
        state = {"review_rounds": 7}
        lanes = {"CX2": {"host": "local", "backend": "codex"}}
        with mock.patch.multiple(ad, gh=gh, paused_scopes=lambda: {"mint"},
                host_ok=lambda h: True, remote=lambda *a: {"lane": "idle"},
                host_good=lambda *a: None, deps_met=lambda i: True,
                prefetch_labels=lambda *a: None, label_state=lambda n: ("fresh", ""),
                sh=lambda *a, **k: (0, "abc123", ""), dispatch=mock.Mock(return_value=("run", False)),
                save_state=lambda s: None, log=mock.Mock(), freshen_queue=lambda q, s: q,
                status_snapshot=lambda *a: {}, D=str(Path(__file__).parent)), \
             mock.patch("builtins.open", mock.mock_open()):
            ad.tick(lanes, state, 0)
            self.assertEqual(ad.dispatch.call_args.args[2]["n"], 2)
        self.assertEqual(state["review_rounds"], 7)
        self.assertIn(["issue", "edit", "1", "--remove-label", "lane-ready"], edits)
        self.assertNotIn(["issue", "edit", "1", "--add-label", "in-progress"], edits)
