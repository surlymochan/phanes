"""编排器测试：使用临时桩脚本，不依赖各角色实现。

审查产物契约用例改用真实的 `review/cross_check.py` 当审查者，其余角色仍是临时桩，
不依赖 Bun 与网络。
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
sys.path.insert(0, str(ROOT))

from run_e2e import REVIEW_REPORT, source_commit  # noqa: E402  (需要先让示例目录可被导入)

RUNNER = ROOT / 'run_e2e.py'
REVIEWER = ROOT / 'review' / 'cross_check.py'
SOURCE_COMMIT = source_commit()
# root 用户不受目录写权限限制，只读目录的用例在 root 下跳过。
IS_ROOT = hasattr(os, 'geteuid') and os.geteuid() == 0


class RunE2EArgumentContractTest(unittest.TestCase):
    """命令行帮助与必填参数契约。"""

    def test_help_lists_supported_options(self):
        result = subprocess.run([sys.executable, str(RUNNER), '--help'], capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        for option in ('--workdir', '--raw', '--bundle', '--boss', '--da', '--builder', '--checker',
                       '--reviewer', '--review'):
            self.assertIn(option, result.stdout)

    def test_help_describes_custom_path_options_relative_to_cwd(self):
        """直接调用 --help，确保 --raw、--bundle、--workdir 的用途说明与相对当前工作目录解析均出现在帮助中。"""
        result = subprocess.run([sys.executable, str(RUNNER), '--help'], capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        for option in ('--raw', '--bundle', '--workdir'):
            self.assertIn(option, result.stdout)
        self.assertIn('相对当前工作目录解析', result.stdout)
        self.assertIn('工作目录路径', result.stdout)
        self.assertIn('原始数据目录路径', result.stdout)
        self.assertIn('前端 bundle 文件路径', result.stdout)

    def test_workdir_is_required(self):
        result = subprocess.run([sys.executable, str(RUNNER)], capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--workdir', result.stderr)
        self.assertIn('required', result.stderr)

    def test_source_commit_returns_current_commit_in_repository(self):
        self.assertEqual(source_commit(), subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], text=True
        ).strip())

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
        (out / 'build-report.json').write_text(json.dumps({'status': 'ok'}), encoding='utf-8')
    else:
        (out / 'index.html').write_text('ok', encoding='utf-8')
elif role == 'reviewer':
    if PROBE:
        brief = pathlib.Path(args[args.index('--brief') + 1])
        data = pathlib.Path(args[args.index('--data') + 1])
        dist = pathlib.Path(args[args.index('--dist') + 1])
        print(f"# 组织诊断交叉审查报告\\n- brief: {brief.name}\\n- data: {data.name}\\n- dist: {dist.name}\\nPASS", flush=True)
    else:
        print("# 组织诊断交叉审查报告\\nPASS", flush=True)
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
                                       '--source-commit', SOURCE_COMMIT,
                                       '--bundle', str(self.bundle)])
        self.assertEqual(calls[3][1], [str(self.workdir / 'dist')])
        self.assertTrue((self.workdir / 'brief.json').is_file())
        self.assertTrue((self.workdir / 'diagnosis.json').is_file())
        self.assertTrue((self.workdir / 'dist' / 'index.html').is_file())
        self.assertIn('serve.py --root', result.stdout)
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder', '检查'])
        for item in timings:
            # 成功流水线的耗时记录结构固定：每步四个字段都不缺。
            self.assertEqual(set(item), {'name', 'command', 'duration_seconds', 'exit_code'})
            self.assertIsInstance(item['command'], list)
            self.assertGreaterEqual(item['duration_seconds'], 0)
            self.assertEqual(item['exit_code'], 0)

    def test_timings_are_written_when_a_step_fails(self):
        for index, (path, role) in enumerate(zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker'))):
            self.write_stub(path, role, failing=(index == 1), exit_code=9)

        result = subprocess.run(self.command(), capture_output=True, text=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA'])
        self.assertEqual(timings[-1]['exit_code'], 9)
        self.assertGreaterEqual(timings[-1]['duration_seconds'], 0)

    def test_failed_step_is_named_when_timings_cannot_be_written(self):
        """步骤失败且耗时记录写不出来时仍非零退出，并在输出里点名失败步骤。"""
        for index, (path, role) in enumerate(zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker'))):
            self.write_stub(path, role, failing=(index == 1), exit_code=8)
        # 耗时记录路径被目录占用：每步耗时写不出来。
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / 'timings.json').mkdir()

        result = subprocess.run(self.command(), capture_output=True, text=True)

        # 整体判失败，不因耗时记录缺失而误报成功。
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn('预览命令', result.stdout)
        # 失败步骤仍然被点名，即使每步耗时没写出来。
        output = result.stdout + result.stderr
        self.assertIn('步骤失败: DA', output)
        self.assertIn('失败步骤: DA', output)
        self.assertIn('耗时记录写入失败', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        # 阶段顺序不变：DA 失败后 Builder 与站点自检不再执行。
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da'])

    def test_runs_from_repo_root_with_relative_workdir_and_default_raw(self):
        """以仓库根为 cwd、相对路径为 --workdir 运行，产物落在相对工作目录且默认 raw 路径仍指向示例目录。"""
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        try:
            rel_workdir = self.workdir.relative_to(REPO_ROOT)
        except ValueError:
            rel_workdir = Path(os.path.relpath(self.workdir, REPO_ROOT))
        self.assertFalse(rel_workdir.is_absolute())
        command = [
            sys.executable, str(RUNNER),
            '--workdir', str(rel_workdir),
            '--bundle', str(self.bundle),
            '--boss', str(self.scripts[0]),
            '--da', str(self.scripts[1]),
            '--builder', str(self.scripts[2]),
            '--checker', str(self.scripts[3]),
        ]
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call[0] for call in calls], ['boss', 'da', 'builder', 'checker'])
        expected_workdir = (REPO_ROOT / rel_workdir).resolve()
        expected_raw = ROOT / 'da' / 'sample-raw'
        self.assertEqual(calls[0][1], ['--out', str(expected_workdir / 'brief.json')])
        self.assertEqual(calls[1][1], ['--brief', str(expected_workdir / 'brief.json'),
                                       '--raw', str(expected_raw),
                                       '--out', str(expected_workdir / 'diagnosis.json')])
        self.assertEqual(calls[2][1], ['--data', str(expected_workdir / 'diagnosis.json'),
                                       '--out', str(expected_workdir / 'dist'),
                                       '--source-commit', SOURCE_COMMIT,
                                       '--bundle', str(self.bundle)])
        self.assertEqual(calls[3][1], [str(expected_workdir / 'dist')])
        self.assertTrue((expected_workdir / 'brief.json').is_file())
        self.assertTrue((expected_workdir / 'diagnosis.json').is_file())
        self.assertTrue((expected_workdir / 'dist' / 'index.html').is_file())
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
                                            '--source-commit', SOURCE_COMMIT,
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

    def test_paths_with_spaces_are_forwarded_and_artifacts_are_preserved(self):
        """工作目录、raw 与脚本路径含空格时仍完整透传并保留产物。"""
        self.workdir = self.root / 'work dir' / 'output'
        self.raw = self.root / 'raw data'
        self.raw.mkdir()
        script_dir = self.root / 'script dir'
        script_dir.mkdir()
        self.scripts = [script_dir / f'{name} script.py'
                        for name in ('boss', 'da', 'builder', 'checker', 'reviewer')]
        self.bundle = self.root / 'bundle dir' / 'fake bundle.js'
        self.bundle.parent.mkdir()
        self.bundle.write_text('space path bundle', encoding='utf-8')

        for path, role in zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer')):
            self.write_stub(path, role, probe=True)
        (self.raw / 'metrics.csv').write_text('metric,value\nrevenue,1\n', encoding='utf-8')

        command = self.command() + ['--review', '--reviewer', str(self.scripts[4])]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = dict(self.calls())
        self.assertEqual(calls['boss'], ['--out', str(self.workdir / 'brief.json')])
        self.assertEqual(calls['da'], ['--brief', str(self.workdir / 'brief.json'), '--raw', str(self.raw),
                                       '--out', str(self.workdir / 'diagnosis.json')])
        self.assertEqual(calls['builder'], ['--data', str(self.workdir / 'diagnosis.json'),
                                            '--out', str(self.workdir / 'dist'), '--source-commit', SOURCE_COMMIT,
                                            '--bundle', str(self.bundle)])
        self.assertEqual(calls['checker'], [str(self.workdir / 'dist')])
        self.assertEqual(calls['reviewer'], ['--brief', str(self.workdir / 'brief.json'),
                                             '--data', str(self.workdir / 'diagnosis.json'),
                                             '--dist', str(self.workdir / 'dist')])
        # 关键上游 JSON 存在且可解析
        self.assertTrue((self.workdir / 'brief.json').is_file())
        brief = json.loads((self.workdir / 'brief.json').read_text(encoding='utf-8'))
        self.assertEqual(brief, {})
        self.assertTrue((self.workdir / 'diagnosis.json').is_file())
        diagnosis = json.loads((self.workdir / 'diagnosis.json').read_text(encoding='utf-8'))
        self.assertEqual(diagnosis['raw_files'], ['metrics.csv'])
        self.assertEqual((self.workdir / 'dist' / 'index.html').read_text(encoding='utf-8'),
                         'space path bundle')
        # workdir 内的 timings 存在且可解析
        timings_path = self.workdir / 'timings.json'
        self.assertTrue(timings_path.is_file())
        timings = json.loads(timings_path.read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder', '检查', '审查'])
        for item in timings:
            self.assertEqual(set(item), {'name', 'command', 'duration_seconds', 'exit_code'})
            self.assertIsInstance(item['command'], list)
            self.assertGreaterEqual(item['duration_seconds'], 0)
            self.assertEqual(item['exit_code'], 0)
        # 最终报告文件（审查报告）存在且可解析
        report_path = self.workdir / REVIEW_REPORT
        self.assertTrue(report_path.is_file())
        report_text = report_path.read_text(encoding='utf-8')
        self.assertIn('# 组织诊断交叉审查报告', report_text)
        self.assertIn('PASS', report_text)
        self.assertIn('brief.json', report_text)
        self.assertIn('diagnosis.json', report_text)
        # 构建报告同样存在且可解析
        build_report_path = self.workdir / 'dist' / 'build-report.json'
        self.assertTrue(build_report_path.is_file())
        build_report = json.loads(build_report_path.read_text(encoding='utf-8'))
        self.assertEqual(build_report['status'], 'ok')

    def test_uses_demo_bundle_by_default(self):
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        command = [argument for argument in self.command() if argument not in ('--bundle', str(self.bundle))]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        builder_call = self.calls()[2][1]
        self.assertEqual(builder_call[-2:], ['--bundle', str(ROOT / 'demo' / 'vendor' / 'app.bundle.js')])
        self.assertEqual(builder_call[-4:-2], ['--source-commit', SOURCE_COMMIT])
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

    def test_missing_role_script_reports_step_and_stops_pipeline(self):
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        self.scripts[1].unlink()

        result = subprocess.run(self.command(), capture_output=True, text=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: DA', result.stderr)
        self.assertIn(str(self.scripts[1]), result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ['boss'])
        self.assertNotIn('[Builder] 命令', result.stdout)

    def test_missing_raw_stops_at_da_before_build_check_and_review(self):
        """raw 目录缺失时 Boss 产出任务书，DA 失败并阻断后续步骤。"""
        for path, role in zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer')):
            self.write_stub(path, role, probe=(role == 'da'))
        missing_raw = self.root / 'missing-raw'
        command = self.command() + ['--raw', str(missing_raw), '--review', '--reviewer', str(self.scripts[4])]

        result = subprocess.run(command, capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('步骤失败: DA', result.stderr)
        self.assertTrue((self.workdir / 'brief.json').is_file())
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da'])
        for step in ('Builder', '检查', '审查'):
            self.assertNotIn(f'[{step}] 命令', result.stdout)
        self.assertFalse((self.workdir / 'diagnosis.json').exists())
        self.assertFalse((self.workdir / 'dist').exists())

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

    def test_missing_explicit_bundle_stops_at_builder(self):
        """显式 bundle 不存在时，Builder 失败并阻断检查与审查。"""
        for path, role in zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer')):
            self.write_stub(path, role)
        command = self.command()
        command[command.index('--bundle') + 1] = 'nonexistent.bundle.js'
        command[command.index('--builder') + 1] = str(ROOT / 'builder' / 'build_site.py')
        command += ['--review', '--reviewer', str(self.scripts[4])]

        result = subprocess.run(command, capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('步骤失败: Builder', result.stderr)
        self.assertIn('bundle 文件不存在或不是有效文件', result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da'])
        for step in ('检查', '审查'):
            self.assertNotIn(f'[{step}] 命令', result.stdout)

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
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder', '检查', '审查'])

    def test_checker_failure_skips_review_and_returns_nonzero(self):
        """站点自检失败时以非零退出，开启 --review 也不执行审查，且不打印预览命令。"""
        for index, (path, role) in enumerate(zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer'))):
            self.write_stub(path, role, failing=(index == 3), exit_code=5)
        result = subprocess.run(self.command() + ['--review', '--reviewer', str(self.scripts[4])],
                                capture_output=True, text=True)
        # 失败结果明确：退出码非 0，报出失败步骤与检查器的退出码
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: 检查', result.stderr)
        self.assertIn('退出码 5', result.stderr)
        # 阶段顺序不变：自检失败后审查者不被调用，也不写出审查记录
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da', 'builder', 'checker'])
        self.assertFalse((self.workdir / REVIEW_REPORT).exists())
        # 失败不误报为成功：不再打印成功路径才有的预览命令
        self.assertNotIn('预览命令', result.stdout)

    def test_checker_failure_keeps_upstream_artifacts_and_returns_nonzero(self):
        """站点自检失败时整体非 0，但 Boss/DA/Builder 已写出的产物仍保留在工作目录。"""
        for index, (path, role) in enumerate(zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker'))):
            self.write_stub(path, role, probe=(role in ('da', 'builder')), failing=(index == 3), exit_code=6)
        (self.raw / 'metrics.csv').write_text('metric,value\nrevenue,1\n', encoding='utf-8')
        self.bundle.write_text('checker-failure-bundle', encoding='utf-8')

        result = subprocess.run(self.command(), capture_output=True, text=True)

        # 整体结果非 0，并报出失败步骤与检查器的退出码
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: 检查', result.stderr)
        self.assertIn('退出码 6', result.stderr)
        self.assertNotIn('预览命令', result.stdout)
        # 上游步骤都已执行到自检这一步
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da', 'builder', 'checker'])
        # 上游产物未被清理：任务书、诊断快照与站点仍在工作目录
        self.assertTrue((self.workdir / 'brief.json').is_file(), 'Checker 失败后任务书被删除')
        self.assertTrue((self.workdir / 'diagnosis.json').is_file(), 'Checker 失败后诊断快照被删除')
        self.assertTrue((self.workdir / 'dist' / 'index.html').is_file(), 'Checker 失败后站点产物被删除')
        # 产物内容保持原样，便于失败后核对
        self.assertEqual(json.loads((self.workdir / 'brief.json').read_text(encoding='utf-8')), {})
        diagnosis = json.loads((self.workdir / 'diagnosis.json').read_text(encoding='utf-8'))
        self.assertEqual(diagnosis['raw_files'], ['metrics.csv'])
        self.assertEqual((self.workdir / 'dist' / 'index.html').read_text(encoding='utf-8'),
                         'checker-failure-bundle')
        # 失败原因仍可从 timings.json 核对
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder', '检查'])
        self.assertEqual(timings[-1]['exit_code'], 6)

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

    def test_workdir_occupied_by_file_reports_readable_error(self):
        """工作目录被同名文件占用时给出可读提示并非零退出，不裸抛 Traceback。"""
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        occupied = self.root / 'occupied'
        occupied.write_text('占位文件', encoding='utf-8')
        command = self.command()
        command[command.index('--workdir') + 1] = str(occupied)

        result = subprocess.run(command, capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('工作目录不可用', result.stderr)
        self.assertIn('占用', result.stderr)
        self.assertIn(str(occupied.resolve()), result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        # 工作目录不可用时不启动任何角色步骤
        self.assertNotIn('[Boss] 命令', result.stdout)
        self.assertFalse(self.log.exists())

    @unittest.skipIf(IS_ROOT, 'root 用户不受目录写权限限制')
    def test_readonly_workdir_reports_readable_error(self):
        """工作目录不可写时给出可读提示并非零退出，不裸抛 Traceback。"""
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        readonly = self.root / 'readonly'
        readonly.mkdir()
        readonly.chmod(0o500)
        self.addCleanup(readonly.chmod, 0o700)
        command = self.command()
        command[command.index('--workdir') + 1] = str(readonly)

        result = subprocess.run(command, capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('工作目录不可写', result.stderr)
        self.assertIn(str(readonly.resolve()), result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        # 工作目录不可写时不启动任何角色步骤
        self.assertNotIn('[Boss] 命令', result.stdout)
        self.assertFalse(self.log.exists())

    def test_occupied_timings_file_reports_readable_error(self):
        """耗时记录路径被目录占用时给出可读提示并非零退出，不误报成功。"""
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        self.workdir.mkdir(parents=True, exist_ok=True)
        timings = self.workdir / 'timings.json'
        timings.mkdir()

        result = subprocess.run(self.command(), capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('耗时记录写入失败', result.stderr)
        self.assertIn(str(timings.resolve()), result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        # 步骤都跑完了，但耗时记录写不出来就不算成功
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da', 'builder', 'checker'])
        self.assertNotIn('预览命令', result.stdout)

    def test_occupied_review_report_reports_readable_error(self):
        """审查报告路径被目录占用时给出可读提示并非零退出，不裸抛 Traceback。"""
        for path, role in zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer')):
            self.write_stub(path, role)
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / REVIEW_REPORT).mkdir()
        command = self.command() + ['--review', '--reviewer', str(self.scripts[4])]

        result = subprocess.run(command, capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('审查报告无法写入', result.stderr)
        self.assertIn(str((self.workdir / REVIEW_REPORT).resolve()), result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        # 审查记录写不出来时不再调用审查者，也不误报成功
        self.assertNotIn('reviewer', [call[0] for call in self.calls()])
        self.assertNotIn('预览命令', result.stdout)

    def test_occupied_review_report_returns_nonzero_and_keeps_upstream_artifacts(self):
        """审查报告路径被目录占用时整体非 0，报出报告路径与失败原因，上游产物保留。"""
        for path, role in zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer')):
            self.write_stub(path, role, probe=(role in ('da', 'builder')))
        (self.raw / 'metrics.csv').write_text('metric,value\nrevenue,1\n', encoding='utf-8')
        self.workdir.mkdir(parents=True, exist_ok=True)
        report = self.workdir / REVIEW_REPORT
        report.mkdir()
        command = self.command() + ['--review', '--reviewer', str(self.scripts[4])]

        result = subprocess.run(command, capture_output=True, text=True)

        # 返回值回归：报告写不进去时整体非 0，不误报成功
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn('预览命令', result.stdout)
        # 输出给出报告路径与写入失败原因，不裸抛 Traceback
        self.assertIn('审查报告无法写入', result.stderr)
        self.assertIn(str(self.workdir.resolve() / REVIEW_REPORT), result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        # 失败步骤被点名，审查者不再被调用
        self.assertIn('失败步骤: 审查', result.stderr)
        self.assertNotIn('reviewer', [call[0] for call in self.calls()])
        # 上游产物保留：任务书、诊断快照与站点仍在，内容不变
        self.assertTrue((self.workdir / 'brief.json').is_file(), '报告写失败后任务书被删除')
        diagnosis = json.loads((self.workdir / 'diagnosis.json').read_text(encoding='utf-8'))
        self.assertEqual(diagnosis['raw_files'], ['metrics.csv'])
        self.assertTrue((self.workdir / 'dist' / 'index.html').is_file(), '报告写失败后站点产物被删除')
        # 耗时记录照常写出，便于核对失败发生在哪一步
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder', '检查'])

    @unittest.skipIf(IS_ROOT, 'root 用户不受文件写权限限制')
    def test_unwritable_review_report_returns_nonzero_and_keeps_upstream_artifacts(self):
        """审查报告文件不可写时整体非 0，报出报告路径与失败原因，上游产物保留。"""
        for path, role in zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer')):
            self.write_stub(path, role, probe=(role in ('da', 'builder')))
        (self.raw / 'metrics.csv').write_text('metric,value\nrevenue,1\n', encoding='utf-8')
        self.workdir.mkdir(parents=True, exist_ok=True)
        report = self.workdir / REVIEW_REPORT
        report.write_text('上一轮的审查报告', encoding='utf-8')
        report.chmod(0o444)
        self.addCleanup(report.chmod, 0o600)
        command = self.command() + ['--review', '--reviewer', str(self.scripts[4])]

        result = subprocess.run(command, capture_output=True, text=True)

        # 返回值回归：报告写不进去时整体非 0，不误报成功
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn('预览命令', result.stdout)
        # 输出给出报告路径与写入失败原因，不裸抛 Traceback
        self.assertIn('审查报告无法写入', result.stderr)
        self.assertIn(str(self.workdir.resolve() / REVIEW_REPORT), result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        # 失败步骤被点名，审查者不再被调用
        self.assertIn('失败步骤: 审查', result.stderr)
        self.assertNotIn('reviewer', [call[0] for call in self.calls()])
        # 上游产物保留：任务书、诊断快照与站点仍在，内容不变
        self.assertTrue((self.workdir / 'brief.json').is_file(), '报告写失败后任务书被删除')
        diagnosis = json.loads((self.workdir / 'diagnosis.json').read_text(encoding='utf-8'))
        self.assertEqual(diagnosis['raw_files'], ['metrics.csv'])
        self.assertTrue((self.workdir / 'dist' / 'index.html').is_file(), '报告写失败后站点产物被删除')
        # 写不进去时旧报告不被清空，失败原因仍可从输出核对
        self.assertEqual(report.read_text(encoding='utf-8'), '上一轮的审查报告')
        # 耗时记录照常写出，便于核对失败发生在哪一步
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder', '检查'])

    def test_review_report_target_cannot_be_created_returns_nonzero(self):
        """审查报告目标的父目录不存在时整体非 0，报出报告路径与失败原因，上游产物保留。

        报告路径指向不存在的目录，任何用户（含 root）都写不进去，
        因此这条用例不受目录写权限限制影响。
        """
        for path, role in zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer')):
            self.write_stub(path, role, probe=(role in ('da', 'builder')))
        (self.raw / 'metrics.csv').write_text('metric,value\nrevenue,1\n', encoding='utf-8')
        self.workdir.mkdir(parents=True, exist_ok=True)
        report = self.workdir / REVIEW_REPORT
        report.symlink_to(self.workdir / 'missing-dir' / REVIEW_REPORT)
        command = self.command() + ['--review', '--reviewer', str(self.scripts[4])]

        result = subprocess.run(command, capture_output=True, text=True)

        # 返回值回归：报告写不进去时整体非 0，不误报成功
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn('预览命令', result.stdout)
        # 输出给出报告路径与写入失败原因，不裸抛 Traceback
        self.assertIn('审查报告无法写入', result.stderr)
        self.assertIn(str(self.workdir.resolve() / REVIEW_REPORT), result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        # 失败步骤被点名，审查者不再被调用
        self.assertIn('失败步骤: 审查', result.stderr)
        self.assertNotIn('reviewer', [call[0] for call in self.calls()])
        # 上游产物保留：任务书、诊断快照与站点仍在
        self.assertTrue((self.workdir / 'brief.json').is_file(), '报告写失败后任务书被删除')
        self.assertTrue((self.workdir / 'diagnosis.json').is_file(), '报告写失败后诊断快照被删除')
        self.assertTrue((self.workdir / 'dist' / 'index.html').is_file(), '报告写失败后站点产物被删除')


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
