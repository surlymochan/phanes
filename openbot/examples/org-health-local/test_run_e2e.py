"""编排器测试：使用临时桩脚本，不依赖各角色实现。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNNER = ROOT / 'run_e2e.py'


class RunE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workdir = self.root / 'out'
        self.log = self.root / 'calls.jsonl'
        self.raw = self.root / 'raw'
        self.raw.mkdir()
        self.bundle = self.root / 'fake.bundle.js'
        self.bundle.write_text('fake', encoding='utf-8')
        self.scripts = [self.root / f'{name}.py' for name in ('boss', 'da', 'builder', 'checker', 'reviewer')]

    def write_stub(self, path, role, failing=False):
        source = '''import json, pathlib, sys
role = ROLE
args = sys.argv[1:]
with open(LOG, 'a', encoding='utf-8') as stream:
    stream.write(json.dumps([role, args]) + "\\n")
if FAIL:
    raise SystemExit(7)
if role == 'boss':
    pathlib.Path(args[args.index('--out') + 1]).write_text('{}', encoding='utf-8')
elif role == 'da':
    pathlib.Path(args[args.index('--out') + 1]).write_text('{}', encoding='utf-8')
elif role == 'builder':
    out = pathlib.Path(args[args.index('--out') + 1])
    out.mkdir(parents=True, exist_ok=True)
    (out / 'index.html').write_text('ok', encoding='utf-8')
'''.replace('ROLE', repr(role)).replace('LOG', repr(str(self.log))).replace('FAIL', repr(failing))
        path.write_text(source, encoding='utf-8')

    def command(self):
        return [sys.executable, str(RUNNER), '--workdir', str(self.workdir), '--raw', str(self.raw),
                '--bundle', str(self.bundle), '--boss', str(self.scripts[0]), '--da', str(self.scripts[1]),
                '--builder', str(self.scripts[2]), '--checker', str(self.scripts[3])]

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def test_runs_in_order_with_expected_arguments_and_artifacts(self):
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        result = subprocess.run(self.command(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call[0] for call in calls], ['boss', 'da', 'builder', 'checker'])
        self.assertEqual(calls[0][1], ['--out', str(self.workdir / 'brief.json')])
        self.assertEqual(calls[1][1], ['--brief', str(self.workdir / 'brief.json'), '--raw', str(self.raw),
                                       '--out', str(self.workdir / 'diagnosis.json')])
        self.assertEqual(calls[2][1], ['--data', str(self.workdir / 'diagnosis.json'), '--out', str(self.workdir / 'dist'),
                                       '--bundle', str(self.bundle)])
        self.assertEqual(calls[3][1], [str(self.workdir / 'dist')])
        self.assertTrue((self.workdir / 'brief.json').is_file())
        self.assertTrue((self.workdir / 'diagnosis.json').is_file())
        self.assertTrue((self.workdir / 'dist' / 'index.html').is_file())
        self.assertIn('serve.py --root', result.stdout)

    def test_uses_demo_bundle_by_default(self):
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        command = [argument for argument in self.command() if argument not in ('--bundle', str(self.bundle))]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        builder_call = self.calls()[2][1]
        self.assertEqual(builder_call[-2:], ['--bundle', str(ROOT / 'demo' / 'vendor' / 'app.bundle.js')])
        self.assertIn(str(ROOT / 'demo' / 'vendor' / 'app.bundle.js'), result.stdout)

    def test_failure_stops_pipeline_and_returns_nonzero(self):
        for index, (path, role) in enumerate(zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker'))):
            self.write_stub(path, role, failing=(index == 1))
        result = subprocess.run(self.command(), capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('步骤失败: DA', result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da'])

    def test_optional_review_runs_after_checker_and_writes_output(self):
        for path, role in zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer')):
            self.write_stub(path, role)
        command = self.command() + ['--review', '--reviewer', str(self.scripts[4])]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da', 'builder', 'checker', 'reviewer'])
        review_call = self.calls()[-1][1]
        self.assertEqual(review_call, ['--brief', str(self.workdir / 'brief.json'), '--data',
                                       str(self.workdir / 'diagnosis.json'), '--dist', str(self.workdir / 'dist')])
        self.assertTrue((self.workdir / 'review.md').is_file())

    def test_review_failure_returns_nonzero(self):
        for index, (path, role) in enumerate(zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer'))):
            self.write_stub(path, role, failing=(index == 4))
        result = subprocess.run(self.command() + ['--review', '--reviewer', str(self.scripts[4])], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('步骤失败: 审查', result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da', 'builder', 'checker', 'reviewer'])


if __name__ == '__main__':
    unittest.main()
