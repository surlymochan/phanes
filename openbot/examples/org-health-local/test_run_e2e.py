"""编排器测试：使用临时桩脚本，不依赖各角色实现。

审查产物契约用例改用真实的 `review/cross_check.py` 当审查者，其余角色仍是临时桩，
不依赖 Bun 与网络。
"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from run_e2e import REVIEW_REPORT  # noqa: E402  (需要先让示例目录可被导入)

RUNNER = ROOT / 'run_e2e.py'
REVIEWER = ROOT / 'review' / 'cross_check.py'

# 供交叉审查契约用例使用的合成任务书与诊断快照（与 review/test_cross_check.py 的夹具同构）。
BRIEF = {
    'schema版本': 4,
    '观察期间': {
        '起始期次': '2026-01',
        '参考期次': '2026-05',
        '截止期次': '2026-09',
        '数据截止': '2026-09-30',
    },
    '必答问题': ['整体技术人力成本在 2026-09 的实际值是多少？'],
}

DIAGNOSIS = {
    'scope': {'观察月份': '2026-09', '数据截止': '2026-09-30', 'schema版本': 4},
    'insights': [{
        '类型': '事实',
        '标题': '整体技术人力成本在 2026-09 的实际值是多少？',
        '内容': '12 万元/月。',
    }],
    'recommendations': [],
}


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

    def write_stub(self, path, role, failing=False, probe=False, exit_code=7, echo=None):
        source = '''import json, pathlib, sys
role = ROLE
args = sys.argv[1:]
with open(LOG, 'a', encoding='utf-8') as stream:
    stream.write(json.dumps([role, args]) + "\\n")
if ECHO is not None:
    print(ECHO, flush=True)
if FAIL:
    raise SystemExit(EXIT_CODE)
if role == 'boss':
    pathlib.Path(args[args.index('--out') + 1]).write_text('{}', encoding='utf-8')
elif role == 'da':
    out = pathlib.Path(args[args.index('--out') + 1])
    raw = pathlib.Path(args[args.index('--raw') + 1])
    payload = {'raw_files': sorted(p.name for p in raw.iterdir())} if PROBE else {}
    out.write_text(json.dumps(payload), encoding='utf-8')
elif role == 'builder':
    out = pathlib.Path(args[args.index('--out') + 1])
    out.mkdir(parents=True, exist_ok=True)
    if PROBE:
        bundle = pathlib.Path(args[args.index('--bundle') + 1])
        (out / 'index.html').write_text(bundle.read_text(encoding='utf-8'), encoding='utf-8')
    else:
        (out / 'index.html').write_text('ok', encoding='utf-8')
'''.replace('ROLE', repr(role)).replace('LOG', repr(str(self.log)))
        source = source.replace('FAIL', repr(failing)).replace('PROBE', repr(probe))
        source = source.replace('EXIT_CODE', repr(exit_code))
        source = source.replace('ECHO', repr(echo))
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

    def test_custom_raw_and_bundle_are_forwarded_and_artifacts_are_checkable(self):
        """自定义 --raw / --bundle 必须透传给对应角色，最终产物可检查。"""
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role, probe=True)
        (self.raw / 'metrics.csv').write_text('metric,value\nrevenue,1\n', encoding='utf-8')
        self.bundle.write_text('console.log("custom-bundle");', encoding='utf-8')
        result = subprocess.run(self.command(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = dict(self.calls())
        self.assertEqual(calls['boss'], ['--out', str(self.workdir / 'brief.json')])
        self.assertEqual(calls['da'], ['--brief', str(self.workdir / 'brief.json'), '--raw', str(self.raw),
                                       '--out', str(self.workdir / 'diagnosis.json')])
        self.assertEqual(calls['builder'], ['--data', str(self.workdir / 'diagnosis.json'), '--out', str(self.workdir / 'dist'),
                                            '--bundle', str(self.bundle)])
        self.assertEqual(calls['checker'], [str(self.workdir / 'dist')])
        # 自定义路径生效，不回退到默认 sample-raw 与 demo bundle
        self.assertNotIn(str(ROOT / 'da' / 'sample-raw'), result.stdout)
        self.assertNotIn(str(ROOT / 'demo' / 'vendor' / 'app.bundle.js'), result.stdout)
        self.assertIn(f'使用 bundle: {self.bundle}', result.stdout)
        self.assertIn(str(self.raw), result.stdout)
        # 产物可检查：DA 读到自定义 raw 目录，Builder 用自定义 bundle 产出站点
        self.assertTrue((self.workdir / 'brief.json').is_file())
        diagnosis = json.loads((self.workdir / 'diagnosis.json').read_text(encoding='utf-8'))
        self.assertEqual(diagnosis['raw_files'], ['metrics.csv'])
        self.assertEqual((self.workdir / 'dist' / 'index.html').read_text(encoding='utf-8'),
                         'console.log("custom-bundle");')

    def test_uses_demo_bundle_by_default(self):
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        command = [argument for argument in self.command() if argument not in ('--bundle', str(self.bundle))]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        builder_call = self.calls()[2][1]
        self.assertEqual(builder_call[-2:], ['--bundle', str(ROOT / 'demo' / 'vendor' / 'app.bundle.js')])
        self.assertIn(str(ROOT / 'demo' / 'vendor' / 'app.bundle.js'), result.stdout)

    def test_boss_failure_stops_pipeline_and_returns_nonzero(self):
        """Boss 失败时打印失败步骤并以非零退出，后续步骤（DA/Builder/站点自检）不再执行。"""
        for index, (path, role) in enumerate(zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker'))):
            self.write_stub(path, role, failing=(index == 0), exit_code=5)
        result = subprocess.run(self.command(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: Boss', result.stderr)
        self.assertIn('退出码 5', result.stderr)
        # Boss 之后的步骤（DA、Builder、站点自检）不再执行
        self.assertEqual([call[0] for call in self.calls()], ['boss'])
        for step in ('DA', 'Builder', '检查'):
            self.assertNotIn(f'[{step}] 命令', result.stdout)

    def test_failure_stops_pipeline_and_returns_nonzero(self):
        for index, (path, role) in enumerate(zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker'))):
            self.write_stub(path, role, failing=(index == 1))
        result = subprocess.run(self.command(), capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('步骤失败: DA', result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da'])

    def test_builder_failure_stops_pipeline_and_returns_nonzero(self):
        """Builder 失败时打印失败步骤并以非零退出，站点自检不再执行。"""
        for index, (path, role) in enumerate(zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker'))):
            self.write_stub(path, role, failing=(index == 2), exit_code=3)
        result = subprocess.run(self.command(), capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('步骤失败: Builder', result.stderr)
        self.assertIn('退出码 3', result.stderr)
        # 后续步骤（站点自检）不再执行
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da', 'builder'])
        self.assertNotIn('[检查] 命令', result.stdout)

    def test_checker_failure_stops_pipeline_and_skips_review(self):
        """Checker 失败时打印失败步骤并以非零退出，独立审查步骤跳过不执行。"""
        for index, (path, role) in enumerate(zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer'))):
            self.write_stub(path, role, failing=(index == 3), exit_code=5)
        command = self.command() + ['--review', '--reviewer', str(self.scripts[4])]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('步骤失败: 检查', result.stderr)
        self.assertIn('退出码 5', result.stderr)
        # 站点检查失败后，后续独立审查步骤不再执行
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da', 'builder', 'checker'])
        self.assertNotIn('[审查] 命令', result.stdout)
        self.assertNotIn('reviewer', [call[0] for call in self.calls()])
        self.assertFalse((self.workdir / REVIEW_REPORT).is_file())

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

    def test_review_failure_keeps_report_and_is_not_reported_as_success(self):
        """审查失败时 e2e 以非零退出，review.md 仍写出审查者输出，失败不误报为成功。"""
        for index, (path, role) in enumerate(zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer'))):
            self.write_stub(path, role, failing=(index == 4), exit_code=4, echo='审查桩输出')
        result = subprocess.run(self.command() + ['--review', '--reviewer', str(self.scripts[4])],
                                capture_output=True, text=True)
        # 失败结果明确：退出码非 0，且报出失败步骤与审查者的退出码
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: 审查', result.stderr)
        self.assertIn('退出码 4', result.stderr)
        # 审查记录仍被创建，并保留审查者的输出
        report = self.workdir / REVIEW_REPORT
        self.assertTrue(report.is_file(), '审查失败时未写出审查记录')
        self.assertIn('审查桩输出', report.read_text(encoding='utf-8'))
        # 失败不误报为成功：不再打印成功路径才有的预览命令
        self.assertNotIn('预览命令', result.stdout)


class RunE2EReviewContractTest(unittest.TestCase):
    """交叉审查产物契约：审查失败时退出码非 0，且仍写出审查记录。

    用真实的 `review/cross_check.py` 当审查者，各角色仍是临时桩，
    只让桩产出互相不一致的任务书、快照与站点数据。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workdir = self.root / 'out'
        self.raw = self.root / 'raw'
        self.raw.mkdir()
        self.bundle = self.root / 'fake.bundle.js'
        self.bundle.write_text('fake', encoding='utf-8')
        self.scripts = {role: self.root / f'{role}.py' for role in ('boss', 'da', 'builder', 'checker')}

    def write_json_stub(self, role, payload):
        """写一个只产出固定 JSON 的桩脚本。"""
        body = repr(json.dumps(payload, ensure_ascii=False))
        source = (
            'import json, pathlib, sys\n'
            'args = sys.argv[1:]\n'
            f'payload = json.loads({body})\n'
            f'if {role!r} == "builder":\n'
            "    out = pathlib.Path(args[args.index('--out') + 1])\n"
            "    (out / 'data').mkdir(parents=True, exist_ok=True)\n"
            "    (out / 'index.html').write_text('ok', encoding='utf-8')\n"
            "    (out / 'data' / 'diagnosis.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')\n"
            'else:\n'
            "    pathlib.Path(args[args.index('--out') + 1]).write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')\n"
        )
        self.scripts[role].write_text(source, encoding='utf-8')

    def write_checker_stub(self):
        """写一个恒成功的站点自检桩。"""
        self.scripts['checker'].write_text('import sys\nsys.exit(0)\n', encoding='utf-8')

    def command(self):
        return [sys.executable, str(RUNNER), '--workdir', str(self.workdir), '--raw', str(self.raw),
                '--bundle', str(self.bundle), '--boss', str(self.scripts['boss']), '--da', str(self.scripts['da']),
                '--builder', str(self.scripts['builder']), '--checker', str(self.scripts['checker']),
                '--review', '--reviewer', str(REVIEWER)]

    def record(self):
        """返回写出的审查记录内容。"""
        path = self.workdir / REVIEW_REPORT
        self.assertTrue(path.is_file(), '审查记录未写出')
        return path.read_text(encoding='utf-8')

    def test_review_record_written_when_site_data_inconsistent(self):
        """站点数据与快照不一致时审查非 0，且仍写出记录并标出 FAIL 项。"""
        self.write_json_stub('boss', BRIEF)
        self.write_json_stub('da', DIAGNOSIS)
        stale = copy.deepcopy(DIAGNOSIS)
        stale['insights'][0]['内容'] = '99 万元/月。'
        self.write_json_stub('builder', stale)
        self.write_checker_stub()
        result = subprocess.run(self.command(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: 审查', result.stderr)
        content = self.record()
        self.assertIn('FAIL 站点数据一致', content)
        self.assertIn('共 5 项检查：4 项通过，1 项失败', content)

    def test_review_record_written_when_brief_inconsistent(self):
        """任务书与快照不一致时审查非 0，且仍写出记录并标出 FAIL 项。"""
        brief = copy.deepcopy(BRIEF)
        brief['观察期间']['截止期次'] = '2026-12'
        self.write_json_stub('boss', brief)
        self.write_json_stub('da', DIAGNOSIS)
        self.write_json_stub('builder', DIAGNOSIS)
        self.write_checker_stub()
        result = subprocess.run(self.command(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: 审查', result.stderr)
        content = self.record()
        self.assertIn('FAIL 期间一致', content)
        self.assertIn('共 5 项检查：4 项通过，1 项失败', content)

    def test_review_record_written_when_consistent(self):
        """三方一致时审查为 0，审查记录写出 PASS 行。"""
        self.write_json_stub('boss', BRIEF)
        self.write_json_stub('da', DIAGNOSIS)
        self.write_json_stub('builder', DIAGNOSIS)
        self.write_checker_stub()
        result = subprocess.run(self.command(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        content = self.record()
        self.assertIn('PASS 期间一致', content)
        self.assertIn('共 5 项检查：5 项通过，0 项失败', content)


if __name__ == '__main__':
    unittest.main()
