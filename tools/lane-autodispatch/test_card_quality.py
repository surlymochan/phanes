"""B1 paths → test_paths; B2 main/targets → test_main_references;
B3 commands → test_commands; B4 admission → test_tick and test_exemptions.
All mappings exercise autodispatch.card_quality/queue on the current main.
"""
import importlib.util
import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock
from contextlib import ExitStack

spec = importlib.util.spec_from_file_location('autodispatch', Path(__file__).with_name('autodispatch.py'))
ad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ad)
VALID = '\n```sh\npython3 -m unittest\n```'


HOST = 'go' + 'vpn'
HOST2 = 'go' + 'res'


class CardQualityTest(TestCase):
    def test_paths(self):
        for body in ('读取' + HOST + ':/data', '读取 ' + HOST2, '读取/etc/passwd', '读取/etc', '读取~/x', '读取../x'):
            with self.subTest(body=body):
                self.assertIsNotNone(ad.card_quality(body + VALID))
        self.assertIsNone(ad.card_quality('本机 ' + HOST + VALID))

    def test_main_references(self):
        with mock.patch.object(ad, 'sh', return_value=(1, '', '')) as sh:
            for body in ('读取missing/file.py', '读取 README.md', '读取 missing/file.py 并新增 output.py', '分支 `feature/foo`'):
                with self.subTest(body=body):
                    self.assertIsNotNone(ad.card_quality(body + VALID))
                    self.assertIn('origin/main', ' '.join(sh.call_args.args[0]))
            for body in ('新增 output.py', '创建分支 feature/foo', '新增 new/file.py'):
                self.assertIsNone(ad.card_quality(body + VALID))
        with mock.patch.object(ad, 'sh', return_value=(0, '', '')) as sh:
            self.assertIsNone(ad.card_quality('分支 feature/foo\n读取 README.md' + VALID))
            self.assertEqual(sh.call_args.args[0][-1], 'origin/main:README.md')

    def test_real_main_snapshot(self):
        with mock.patch.object(ad, 'LOCAL', str(Path(__file__).resolve().parents[2])):
            self.assertIsNone(ad.card_quality('读取 README.md\n分支 origin/main' + VALID))
            self.assertIsNotNone(ad.card_quality('读取 missing/issue1853.py' + VALID))
            self.assertIsNotNone(ad.card_quality('分支 missing-issue1853' + VALID))

    def test_commands(self):
        for body in ('验收：通过或人工确认', '验收：功能正常', 'git status', 'python', '```json\n{"test":true}\n```', '```text\npytest tests\n```', './scripts/deploy.sh', 'bash deploy.sh'):
            with self.subTest(body=body):
                self.assertIsNotNone(ad.card_quality(body))
        for body in ('./scripts/test.sh', 'python3 -m unittest tests', 'bash test.sh', 'pytest tests', '验收：`python3 -m unittest`'):
            self.assertIsNone(ad.card_quality(body))

    def test_exemptions(self):
        verify = '## Verify Intent\n目标 PR: #1\n证据要求:\n- [ ] checks'
        rows = [dict(number=1, title='verify', body=verify, labels=['lane-ready', 'verify-intent']),
                dict(number=2, title='linted', body='功能正常', labels=['lane-ready'])]
        with mock.patch.object(ad, 'gh', return_value=(0, json.dumps(rows), '')) as gh, mock.patch.object(ad, 'preflight_card') as lint:
            self.assertEqual([i['n'] for i in ad.queue()], [1, 2])
            lint.assert_called_once_with('功能正常')
            gh.assert_called_once()

    def test_tick(self):
        rows = [dict(number=1, title='bad', body='读取/etc', labels=['lane-ready']),
                dict(number=2, title='good', body=VALID, labels=['lane-ready'])]
        edits = []
        def gh(args, **kwargs):
            if args[:2] == ['issue', 'list']: return 0, json.dumps(rows), ''
            if args[:2] == ['issue', 'view']:
                return 0, json.dumps({**rows[1], 'state': 'OPEN'}), ''
            edits.append(args)
            return 0, '', ''
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            dispatch = mock.Mock(return_value=('run', None))
            sh = mock.Mock(side_effect=lambda cmd, **k: (0, '8388608' if '/proc/meminfo' in ' '.join(cmd) else 'a' * 40, ''))
            replacements = dict(D=directory, gh=gh, dispatch=dispatch, sh=sh,
                remote=lambda *a: {'lane': 'idle'}, audit_switches=lambda: None,
                paused_scopes=lambda **k: {'mint'}, log=mock.Mock(),
                preflight_card=mock.Mock(side_effect=RuntimeError('legacy')))
            for name, value in replacements.items(): stack.enter_context(mock.patch.object(ad, name, value))
            ad.tick({'CX10': {'host': 'h', 'backend': 'codex'}}, {}, 0)
            self.assertEqual(dispatch.call_args.args[2]['n'], 2)
            self.assertIn(['issue', 'edit', '1', '--remove-label', 'lane-ready'], edits)
            self.assertEqual([e for e in edits if e[2] == '1'], [['issue', 'edit', '1', '--remove-label', 'lane-ready']])
            # Reviews/round accounting is external to AD: rejection must never invoke
            # a review command or write a review artifact, only admission/status state.
            self.assertFalse(any('review' in ' '.join(c.args[0]) for c in sh.call_args_list))
            self.assertFalse(list(Path(directory).rglob('*review*')))
