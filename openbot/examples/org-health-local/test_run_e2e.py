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

from run_e2e import REVIEW_REPORT, STATUS_FILE, parse_args, source_commit  # noqa: E402  (需要先让示例目录可被导入)

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
                       '--reviewer', '--review', '--timeout'):
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

    def test_help_describes_role_script_paths_relative_to_example(self):
        """--help 里说明角色脚本路径按示例目录解析。"""
        result = subprocess.run([sys.executable, str(RUNNER), '--help'], capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        # 帮助文本按终端宽度折行，比较前去掉空白再匹配。
        compact = ''.join(result.stdout.split())
        for option, metavar in (('--boss', 'BOSS'), ('--da', 'DA'), ('--builder', 'BUILDER'),
                                ('--checker', 'CHECKER')):
            # 选项说明出现在 usage 之后，取最后一次出现的位置。
            index = compact.rindex(f'{option}{metavar}')
            self.assertIn('相对示例目录解析', compact[index:index + 60])

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

    def write_stub(self, path, role, failing=False, probe=False, exit_code=7, echo=None,
                   report_path=None, report_body=None, error_text=None, raw_output=None,
                   fail_after_output=False):
        if report_path is not None and report_body is None:
            report_body = '{}'
        source = '''import json, pathlib, sys
role = ROLE
args = sys.argv[1:]
with open(LOG, 'a', encoding='utf-8') as stream:
    stream.write(json.dumps([role, args]) + "\\n")
if ECHO is not None:
    print(ECHO, flush=True)
if RPT_PATH is not None:
    pathlib.Path(RPT_PATH).write_text(RPT_BODY, encoding='utf-8')
if ERR_TEXT is not None:
    print(ERR_TEXT, file=sys.stderr, flush=True)
if FAIL and not FAIL_AFTER_OUTPUT:
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
    if RAW_OUTPUT is not None:
        sys.stdout.buffer.write(RAW_OUTPUT)
        sys.stdout.buffer.flush()
    elif PROBE:
        brief = pathlib.Path(args[args.index('--brief') + 1])
        data = pathlib.Path(args[args.index('--data') + 1])
        dist = pathlib.Path(args[args.index('--dist') + 1])
        print(f"# 组织诊断交叉审查报告\\n- brief: {brief.name}\\n- data: {data.name}\\n- dist: {dist.name}\\nPASS", flush=True)
    else:
        print("# 组织诊断交叉审查报告\\nPASS", flush=True)
if FAIL:
    raise SystemExit(EXIT_CODE)
'''.replace('ROLE', repr(role)).replace('LOG', repr(str(self.log)))
        source = source.replace('FAIL_AFTER_OUTPUT', repr(fail_after_output))
        source = source.replace('FAIL', repr(failing)).replace('PROBE', repr(probe))
        source = source.replace('EXIT_CODE', repr(exit_code))
        source = source.replace('ECHO', repr(echo))
        source = source.replace('RPT_PATH', repr(report_path))
        source = source.replace('RPT_BODY', repr(report_body))
        source = source.replace('ERR_TEXT', repr(error_text))
        source = source.replace('RAW_OUTPUT', repr(raw_output))
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

    def test_timeout_kills_role_group_and_preserves_real_upstream_artifacts(self):
        """真实 Boss/DA/Builder 完成后，阻塞检查器超时仍保留上游产物并写完整 timings。"""
        blocker = self.root / 'blocking_checker.py'
        blocker.write_text('''import pathlib, sys, time
pathlib.Path(sys.argv[1], "checker-started").write_text("started", encoding="utf-8")
time.sleep(30)
''', encoding='utf-8')
        command = [
            sys.executable, str(RUNNER), '--workdir', str(self.workdir),
            '--raw', str(ROOT / 'da' / 'sample-raw'),
            '--bundle', str(ROOT / 'demo' / 'vendor' / 'app.bundle.js'),
            '--boss', str(ROOT / 'boss' / 'make_brief.py'),
            '--da', str(ROOT / 'da' / 'build_diagnosis.py'),
            '--builder', str(ROOT / 'builder' / 'build_site.py'),
            '--checker', str(blocker), '--timeout', '0.2',
        ]

        result = subprocess.run(command, capture_output=True, text=True, timeout=10)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue((self.workdir / 'brief.json').is_file())
        self.assertTrue((self.workdir / 'diagnosis.json').is_file())
        self.assertTrue((self.workdir / 'dist' / 'index.html').is_file())
        self.assertTrue((self.workdir / 'dist' / 'checker-started').is_file())
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder', '检查'])
        self.assertTrue(timings[-1]['timeout'])
        self.assertNotEqual(timings[-1]['exit_code'], 0)
        self.assertIn('超时', timings[-1]['reason'])
        self.assertNotIn('[审查] 命令', result.stdout)

    def test_timeout_stops_following_roles_and_records_failed_role(self):
        """中间角色超时后不启动后续角色，并在 timings/status 中记录失败角色与耗时。"""
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        slow_da = self.scripts[1]
        slow_da.write_text(f'''import json, pathlib, sys, time
with open({str(self.log)!r}, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(["da", sys.argv[1:]]) + "\\n")
pathlib.Path(sys.argv[sys.argv.index("--out") + 1]).write_text("partial", encoding="utf-8")
time.sleep(30)
''', encoding='utf-8')

        result = subprocess.run(self.command() + ['--timeout', '0.2'],
                                capture_output=True, text=True, timeout=10)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ['boss', 'da'])
        self.assertNotIn('[Builder] 命令', result.stdout)
        self.assertNotIn('[检查] 命令', result.stdout)

        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA'])
        self.assertTrue(timings[-1]['timeout'])
        self.assertGreaterEqual(timings[-1]['duration_seconds'], 0)
        self.assertIn('超时', timings[-1]['reason'])

        status = json.loads((self.workdir / STATUS_FILE).read_text(encoding='utf-8'))
        self.assertEqual(status['failed_step'], 'da')
        self.assertEqual([item['name'] for item in status['timings']], ['Boss', 'DA'])
        self.assertEqual(status['timings'][-1]['duration_seconds'], timings[-1]['duration_seconds'])

    def test_timings_are_written_when_a_step_fails(self):
        for index, (path, role) in enumerate(zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker'))):
            self.write_stub(path, role, failing=(index == 1), exit_code=9)

        result = subprocess.run(self.command(), capture_output=True, text=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA'])
        self.assertEqual(timings[-1]['exit_code'], 9)
        self.assertGreaterEqual(timings[-1]['duration_seconds'], 0)

    def test_signal_killed_role_is_named_and_written_to_timings(self):
        """角色被 kill -9 终止时，失败角色、SIGKILL 和 timings 都必须可核对。"""
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        killer = self.scripts[1]
        killer.write_text(
            "import os\nos.execl('/bin/sh', 'sh', '-c', 'kill -9 $$')\n",
            encoding='utf-8',
        )

        result = subprocess.run(self.command(), capture_output=True, text=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: DA', result.stderr)
        self.assertIn('SIGKILL', result.stderr)
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA'])
        self.assertEqual(timings[-1]['exit_code'], -9)
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

    def test_relative_role_paths_resolve_against_example_from_repo_root(self):
        """从仓库根目录调用时，相对角色路径按示例目录解析，产物仍写入 --workdir。"""
        roles = ('boss', 'da', 'builder', 'checker')
        role_dir = self.root / 'roles'
        role_dir.mkdir()
        stubs = {role: role_dir / f'{role}.py' for role in roles}
        for role, path in stubs.items():
            self.write_stub(path, role)
        rel_workdir = os.path.relpath(self.workdir, REPO_ROOT)
        rel_roles = {role: os.path.relpath(path, ROOT) for role, path in stubs.items()}
        for path in rel_roles.values():
            self.assertFalse(os.path.isabs(path), '角色路径应当是相对路径')
        command = [
            sys.executable, str(RUNNER),
            '--workdir', rel_workdir,
            '--raw', str(self.raw),
            '--bundle', str(self.bundle),
            '--boss', rel_roles['boss'],
            '--da', rel_roles['da'],
            '--builder', rel_roles['builder'],
            '--checker', rel_roles['checker'],
        ]

        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # 相对角色路径按示例目录找到脚本执行。
        for role in roles:
            self.assertIn(rel_roles[role], result.stdout)
        self.assertEqual([call[0] for call in self.calls()], list(roles))
        # 产物仍写入 --workdir 指向的临时目录。
        expected_workdir = (REPO_ROOT / rel_workdir).resolve()
        self.assertTrue((expected_workdir / 'brief.json').is_file())
        self.assertTrue((expected_workdir / 'diagnosis.json').is_file())
        self.assertTrue((expected_workdir / 'dist' / 'index.html').is_file())
        self.assertTrue((expected_workdir / 'timings.json').is_file())
        # 仓库根目录不留下产物。
        for name in ('brief.json', 'diagnosis.json', 'timings.json', 'dist'):
            self.assertFalse((REPO_ROOT / name).exists(), f'产物写到了仓库根目录: {name}')

    def test_relative_role_path_resolves_against_script_directory(self):
        """相对角色路径按示例目录解析，即使 cwd 是仓库根目录也能找到。"""
        for path, role in zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker')):
            self.write_stub(path, role)
        command = [
            sys.executable, str(RUNNER),
            '--workdir', str(self.workdir),
            '--raw', str(self.raw),
            '--bundle', str(self.bundle),
            '--boss', 'boss/make_brief.py',
            '--da', str(self.scripts[1]),
            '--builder', str(self.scripts[2]),
            '--checker', str(self.scripts[3]),
        ]

        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ['da', 'builder', 'checker'])

    def test_relative_role_paths_are_independent_of_cwd_outside_repo_root(self):
        """在仓库外的任意 cwd 调用时，相对角色路径仍按示例目录解析。"""
        work_cwd = self.root / 'elsewhere'
        work_cwd.mkdir()
        roles = ('boss', 'da', 'builder', 'checker')
        for role in roles:
            self.write_stub(work_cwd / f'{role}.py', role)
        relative_roles = {
            role: os.path.relpath(work_cwd / f'{role}.py', ROOT) for role in roles
        }
        command = [
            sys.executable, str(RUNNER),
            '--workdir', 'out',
            '--raw', str(self.raw),
            '--bundle', str(self.bundle),
            '--boss', relative_roles['boss'],
            '--da', relative_roles['da'],
            '--builder', relative_roles['builder'],
            '--checker', relative_roles['checker'],
        ]

        result = subprocess.run(command, cwd=work_cwd, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call[0] for call in self.calls()], list(roles))
        # 产物写入 cwd 下的相对工作目录，而不是脚本所在目录。
        expected_workdir = work_cwd / 'out'
        self.assertTrue((expected_workdir / 'brief.json').is_file())
        self.assertTrue((expected_workdir / 'diagnosis.json').is_file())
        self.assertTrue((expected_workdir / 'dist' / 'index.html').is_file())
        self.assertFalse((work_cwd / 'brief.json').exists())

    def test_relative_role_paths_resolve_after_chdir_to_temp_dir(self):
        """chdir 到临时目录再跑：相对角色脚本与 raw 路径仍按示例目录解析。"""
        original_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, original_cwd)
        command = [
            sys.executable, str(RUNNER),
            '--workdir', 'out',
            '--raw', 'da/sample-raw',
            '--bundle', 'demo/vendor/app.bundle.js',
            '--boss', 'boss/make_brief.py',
            '--da', 'da/build_diagnosis.py',
            '--builder', 'builder/build_site.py',
            '--checker', 'builder/check_site.py',
        ]

        result = subprocess.run(command, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # cwd 在临时目录时，四个角色仍按示例目录里的相对路径找到并执行。
        # 产物写入 cwd 下的相对工作目录。
        expected_workdir = self.root / 'out'
        self.assertTrue((expected_workdir / 'brief.json').is_file())
        self.assertTrue((expected_workdir / 'diagnosis.json').is_file())
        self.assertTrue((expected_workdir / 'dist' / 'index.html').is_file())
        self.assertTrue((expected_workdir / 'timings.json').is_file())
        self.assertFalse((self.root / 'brief.json').exists())

    def test_default_role_paths_stay_absolute(self):
        """默认角色脚本路径保持绝对路径，不随 cwd 变化。"""
        args = parse_args(['--workdir', 'out'])
        for name, expected in (('boss', ROOT / 'boss' / 'make_brief.py'),
                               ('da', ROOT / 'da' / 'build_diagnosis.py'),
                               ('builder', ROOT / 'builder' / 'build_site.py'),
                               ('checker', ROOT / 'builder' / 'check_site.py'),
                               ('reviewer', ROOT / 'review' / 'cross_check.py'),
                               ('raw', ROOT / 'da' / 'sample-raw')):
            value = getattr(args, name)
            self.assertTrue(value.is_absolute(), f'--{name} 默认路径不是绝对路径')
            self.assertEqual(value, expected)

    def test_default_role_path_is_used_when_flag_is_omitted(self):
        """省略 --boss 时仍用示例目录里的默认绝对路径，与 cwd 无关。

        本用例只核对命令行里的路径，不依赖角色实现是否成功。
        """
        for path, role in zip(self.scripts[1:4], ('da', 'builder', 'checker')):
            self.write_stub(path, role)
        command = [
            sys.executable, str(RUNNER),
            '--workdir', str(self.workdir),
            '--raw', str(self.raw),
            '--bundle', str(self.bundle),
            '--da', str(self.scripts[1]),
            '--builder', str(self.scripts[2]),
            '--checker', str(self.scripts[3]),
        ]

        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)

        # 命令行里出现的是示例目录里的默认绝对路径，不随 cwd 变化。
        self.assertIn(f'[Boss] 命令: {sys.executable} {ROOT / "boss" / "make_brief.py"}', result.stdout)

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

    def test_custom_role_scripts_receive_only_their_own_arguments(self):
        """自定义五个角色脚本时，路径与参数分别透传且前四步不收到 Reviewer 参数。"""
        roles = ('boss', 'da', 'builder', 'checker', 'reviewer')
        for path, role in zip(self.scripts, roles):
            self.write_stub(path, role)

        command = [
            sys.executable, str(RUNNER), '--workdir', str(self.workdir),
            '--raw', str(self.raw), '--bundle', str(self.bundle),
            '--boss', str(self.scripts[0]), '--da', str(self.scripts[1]),
            '--builder', str(self.scripts[2]), '--checker', str(self.scripts[3]),
            '--reviewer', str(self.scripts[4]), '--review',
        ]
        result = subprocess.run(command, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call[0] for call in calls], list(roles))
        expected = {
            'boss': ['--out', str(self.workdir / 'brief.json')],
            'da': ['--brief', str(self.workdir / 'brief.json'), '--raw', str(self.raw),
                   '--out', str(self.workdir / 'diagnosis.json')],
            'builder': ['--data', str(self.workdir / 'diagnosis.json'), '--out', str(self.workdir / 'dist'),
                        '--source-commit', SOURCE_COMMIT, '--bundle', str(self.bundle)],
            'checker': [str(self.workdir / 'dist')],
            'reviewer': ['--brief', str(self.workdir / 'brief.json'), '--data',
                         str(self.workdir / 'diagnosis.json'), '--dist', str(self.workdir / 'dist')],
        }
        for role, args in calls:
            self.assertEqual(args, expected[role])

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

    def test_da_failure_preserves_completed_artifacts_and_timings(self):
        """DA 写出诊断后失败时，任务书、诊断快照和失败步骤耗时都保留。"""
        for index, (path, role) in enumerate(zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker'))):
            self.write_stub(path, role, failing=(index == 1), fail_after_output=(index == 1))

        result = subprocess.run(self.command(), capture_output=True, text=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('失败步骤: DA', result.stderr)
        self.assertTrue((self.workdir / 'brief.json').is_file())
        self.assertTrue((self.workdir / 'diagnosis.json').is_file())
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA'])
        self.assertEqual([item['exit_code'] for item in timings], [0, 7])

    def test_builder_failure_preserves_completed_artifacts_and_timings(self):
        """Builder 写出站点后失败时，上游 JSON 和已执行步骤耗时都保留。"""
        for index, (path, role) in enumerate(zip(self.scripts[:4], ('boss', 'da', 'builder', 'checker'))):
            self.write_stub(path, role, failing=(index == 2), fail_after_output=(index == 2))

        result = subprocess.run(self.command(), capture_output=True, text=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('失败步骤: Builder', result.stderr)
        self.assertTrue((self.workdir / 'brief.json').is_file())
        self.assertTrue((self.workdir / 'diagnosis.json').is_file())
        self.assertTrue((self.workdir / 'dist' / 'index.html').is_file())
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder'])
        self.assertEqual([item['exit_code'] for item in timings], [0, 0, 7])

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

    def test_review_report_kept_when_reviewer_fails_after_writing_report(self):
        """审查脚本写出报告后返回非零：运行器非零退出，报告保留，不误报成功。

        报告写到临时路径（--review-report），角色全是临时桩，不依赖真实实现。
        """
        report = self.root / 'review.json'
        body = json.dumps({'结论': '不通过', 'findings': ['期间一致']}, ensure_ascii=False)
        for index, (path, role) in enumerate(zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer'))):
            self.write_stub(path, role, failing=(index == 4), exit_code=2,
                            report_path=(str(report) if role == 'reviewer' else None),
                            report_body=(body if role == 'reviewer' else None))
        command = self.command() + ['--review', '--reviewer', str(self.scripts[4]),
                                    '--review-report', str(report)]

        result = subprocess.run(command, capture_output=True, text=True)

        # 失败结果明确：退出码非 0，并报出失败步骤与审查脚本的退出码
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: 审查', result.stderr)
        self.assertIn('退出码 2', result.stderr)
        # 失败不误报为成功：不再打印成功路径才有的预览命令
        self.assertNotIn('预览命令', result.stdout)
        # 审查脚本写出的报告保留在临时报告路径上，内容不被清空
        self.assertTrue(report.is_file(), '审查失败时报告被删除')
        self.assertEqual(json.loads(report.read_text(encoding='utf-8')), json.loads(body))
        # 耗时记录结构不变：审查步骤在列，退出码与审查脚本一致
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder', '检查', '审查'])
        self.assertEqual(set(timings[-1]), {'name', 'command', 'duration_seconds', 'exit_code'})
        self.assertEqual(timings[-1]['exit_code'], 2)

    def test_review_partial_report_failure_is_observable_in_timings_and_error(self):
        """审查器写出部分报告后失败：报告保留，timings 含失败审查段且错误明确指向 review。"""
        for index, (path, role) in enumerate(zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer'))):
            self.write_stub(path, role, failing=(index == 4), exit_code=6,
                            echo='部分审查报告')
        result = subprocess.run(self.command() + ['--review', '--reviewer', str(self.scripts[4])],
                                capture_output=True, text=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('review 角色失败', result.stderr)
        self.assertIn('步骤失败: 审查', result.stderr)
        report = self.workdir / REVIEW_REPORT
        self.assertTrue(report.is_file())
        self.assertIn('部分审查报告', report.read_text(encoding='utf-8'))
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder', '检查', '审查'])
        self.assertEqual(timings[-1]['exit_code'], 6)
        self.assertEqual(set(timings[-1]), {'name', 'command', 'duration_seconds', 'exit_code'})

    def test_review_failure_keeps_reviewer_error_in_default_report(self):
        """审查脚本参数失败时，默认报告里保留脚本的错误输出，不留下空报告。"""
        for index, (path, role) in enumerate(zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer'))):
            self.write_stub(path, role, failing=(index == 4), exit_code=2,
                            error_text='审查参数错误：缺少 --data')
        command = self.command() + ['--review', '--reviewer', str(self.scripts[4])]

        result = subprocess.run(command, capture_output=True, text=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: 审查', result.stderr)
        self.assertNotIn('预览命令', result.stdout)
        # 失败原因留在报告产物里，便于事后核对
        report = self.workdir / REVIEW_REPORT
        self.assertTrue(report.is_file(), '审查失败时未写出审查记录')
        self.assertIn('审查参数错误', report.read_text(encoding='utf-8'))

    def test_review_output_with_undecodable_bytes_reports_readable_failure(self):
        """审查器输出不可解码字节时给出可读失败：非零退出、报告仍在、耗时记录写明失败步骤。

        审查器输出原始字节（非法 UTF-8），运行器不能把它当成功，也不该只留下裸 Traceback。
        """
        for path, role in zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer')):
            self.write_stub(path, role, raw_output=(b'\xff\xfe \xe5\xae\xa1\xe6\x9f\xa5\xe6\xa1\xa9\xe8\xbe\x93\xe5\x87\xba\n'
                                                   if role == 'reviewer' else None))
        command = self.command() + ['--review', '--reviewer', str(self.scripts[4])]

        result = subprocess.run(command, capture_output=True, text=True)

        # 失败结果明确：非零退出并点名失败步骤，不裸抛 Traceback
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: 审查', result.stderr)
        self.assertIn('非 UTF-8', result.stderr)
        self.assertIn('失败步骤: 审查', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        # 失败不误报为成功：不再打印成功路径才有的预览命令
        self.assertNotIn('预览命令', result.stdout)
        # 审查记录仍写出，原始字节保留在报告里，便于事后核对
        report = self.workdir / REVIEW_REPORT
        self.assertTrue(report.is_file(), '审查输出不可解码时未写出审查记录')
        self.assertIn(b'\xff', report.read_bytes())
        # 耗时记录写出失败步骤，退出码非 0
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder', '检查', '审查'])
        self.assertNotEqual(timings[-1]['exit_code'], 0)

    def test_review_report_keeps_utf8_content_and_path_when_output_is_decodable(self):
        """审查器输出可解码时：报告路径与 UTF-8 内容不变，整体仍返回 0。"""
        for path, role in zip(self.scripts, ('boss', 'da', 'builder', 'checker', 'reviewer')):
            self.write_stub(path, role)
        command = self.command() + ['--review', '--reviewer', str(self.scripts[4])]

        result = subprocess.run(command, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = self.workdir / REVIEW_REPORT
        self.assertTrue(report.is_file())
        # 报告仍是可解码的 UTF-8 文本，内容与路径不受影响
        content = report.read_text(encoding='utf-8')
        self.assertIn('# 组织诊断交叉审查报告', content)
        self.assertIn('PASS', content)
        timings = json.loads((self.workdir / 'timings.json').read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in timings], ['Boss', 'DA', 'Builder', '检查', '审查'])
        self.assertEqual(timings[-1]['exit_code'], 0)

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


class RunE2ERoleScriptTest(unittest.TestCase):
    """真实角色脚本回归：用真实子进程跑完整流水线，不靠空桩绕过角色脚本。

    两条路径都覆盖：Builder 失败时保留上游产物、写出步骤状态且不调用 Reviewer；
    Builder 成功时进入 Reviewer 并生成最终报告。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workdir = self.root / 'out'

    def command(self, bundle):
        """真实角色脚本的调用命令：只换 bundle，其余角色都用示例目录里的真实脚本。"""
        return [
            sys.executable, str(RUNNER),
            '--workdir', str(self.workdir),
            '--raw', str(ROOT / 'da' / 'sample-raw'),
            '--bundle', str(bundle),
            '--boss', str(ROOT / 'boss' / 'make_brief.py'),
            '--da', str(ROOT / 'da' / 'build_diagnosis.py'),
            '--builder', str(ROOT / 'builder' / 'build_site.py'),
            '--checker', str(ROOT / 'builder' / 'check_site.py'),
            '--reviewer', str(REVIEWER),
            '--review',
        ]

    def status(self):
        """读出步骤状态文件。"""
        path = self.workdir / STATUS_FILE
        self.assertTrue(path.is_file(), '步骤状态文件未写出')
        return json.loads(path.read_text(encoding='utf-8'))

    def test_builder_failure_keeps_upstream_artifacts_and_skips_reviewer(self):
        """真实 Builder 失败：保留 Boss/DA 产物，写出状态文件，Reviewer 不被调用。"""
        missing_bundle = self.root / 'missing.bundle.js'

        result = subprocess.run(self.command(missing_bundle), capture_output=True, text=True, timeout=120)

        # 失败结果明确：非零退出并点名失败步骤
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('步骤失败: Builder', result.stderr)
        self.assertNotIn('预览命令', result.stdout)
        # Boss/DA 已完成产物保留在工作目录，任务书不是空的
        brief = json.loads((self.workdir / 'brief.json').read_text(encoding='utf-8'))
        self.assertTrue(brief['目标'], '任务书为空：不能只测空输入')
        self.assertTrue(brief['必答问题'], '任务书为空：不能只测空输入')
        self.assertTrue((self.workdir / 'diagnosis.json').is_file(), '诊断快照被删除')
        # Builder 失败时不生成站点产物
        self.assertFalse((self.workdir / 'dist').exists(), 'Builder 失败后仍生成了站点产物')
        # 状态文件写出失败步骤、错误与耗时
        status = self.status()
        self.assertEqual(status['status'], 'failed')
        self.assertEqual(status['failed_step'], 'builder')
        self.assertIn('bundle 文件不存在或不是有效文件', status['error'])
        self.assertEqual([item['name'] for item in status['timings']], ['Boss', 'DA', 'Builder'])
        self.assertNotEqual(status['timings'][-1]['exit_code'], 0)
        self.assertEqual([item['role'] for item in status['steps']], ['boss', 'da', 'builder'])
        # Boss/DA 两步在状态文件里都是完成状态
        self.assertEqual([item['exit_code'] for item in status['steps'][:2]], [0, 0])
        # Reviewer 未被调用：没有审查产物，也没有审查步骤
        self.assertFalse(status['reviewer_called'])
        self.assertFalse((self.workdir / REVIEW_REPORT).exists())
        self.assertNotIn('[审查] 命令', result.stdout)
        self.assertIn('审查角色未调用', result.stderr)

    def test_builder_success_runs_reviewer_and_writes_final_report(self):
        """真实 Builder 成功后进入 Reviewer，生成最终报告并写出成功状态。"""
        bundle = ROOT / 'demo' / 'vendor' / 'app.bundle.js'

        result = subprocess.run(self.command(bundle), capture_output=True, text=True, timeout=120)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # 站点产物与最终审查报告都在
        self.assertTrue((self.workdir / 'dist' / 'index.html').is_file())
        self.assertTrue((self.workdir / 'dist' / 'build-report.json').is_file())
        report = self.workdir / REVIEW_REPORT
        self.assertTrue(report.is_file(), '最终报告未生成')
        content = report.read_text(encoding='utf-8')
        self.assertIn('PASS', content)
        self.assertIn('PASS 站点数据一致', content)
        self.assertIn('项通过，0 项失败', content)
        # 状态文件记录成功与审查已调用
        status = self.status()
        self.assertEqual(status['status'], 'ok')
        self.assertIsNone(status['failed_step'])
        self.assertTrue(status['reviewer_called'])
        self.assertEqual([item['name'] for item in status['timings']],
                         ['Boss', 'DA', 'Builder', '检查', '审查'])
        self.assertEqual(status['timings'][-1]['exit_code'], 0)


if __name__ == '__main__':
    unittest.main()
