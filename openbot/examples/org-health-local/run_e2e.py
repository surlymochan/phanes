#!/usr/bin/env python3
"""Run the synthetic Boss to DA to Builder pipeline.

失败时保留上游产物，并写出 timings.json 与 status.json 供核对：
status.json 记录每步状态、失败步骤及其错误输出；
步骤失败时另在 step-errors/ 写出该步的命令、退出码与 stdout/stderr 尾部。
审查步骤的结论汇总写入 status.json 的 review 字段：发现数、各严重级计数与是否阻断；
存在阻断级发现时整体非零退出。
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 交叉审查产物路径：审查失败时同样写出，便于核对失败原因。
REVIEW_REPORT = 'review.md'
# 交叉审查的 JSON 结果文件：审查结论的结构化来源，供运行器汇总发现数与严重级。
REVIEW_RESULTS = 'review-results.json'
# 审查结论的严重级：error 阻断流水线，其余级别只记录不阻断。
BLOCKING_SEVERITIES = frozenset({'error'})
# 审查结论缺少严重级时按阻断级处理：结论不明时不放过。
DEFAULT_SEVERITY = 'error'
# stderr 里最多列出的阻断级发现条数：只给要点，明细留在审查报告与结果文件里。
BLOCKING_DETAIL_LIMIT = 3
# 单条阻断级发现说明的长度上限：避免长文本淹没提示。
BLOCKING_DETAIL_LENGTH = 120
# 步骤状态文件：写出每步状态、失败步骤与错误，不必翻日志也能定位失败原因。
STATUS_FILE = 'status.json'
# Soak 观测读取的耗时产物名：成功与失败路径都使用同一个稳定名称。
TIMINGS_FILE = 'timings.json'
# 失败步骤的原始输出快照目录：便于诊断角色输出损坏 JSON 时仍能核对现场。
STEP_ERROR_DIR = 'step-errors'
# 步骤名到稳定角色名的映射：状态文件用稳定的英文角色名，
# 调用方不依赖本地化的步骤名定位失败步骤。
STEP_ROLES = {
    'Boss': 'boss',
    'DA': 'da',
    'Builder': 'builder',
    '检查': 'checker',
    '审查': 'review',
}
# 角色脚本缺失时错误里点名的角色名：步骤名 → 角色名。
# 中文步骤名补上英文角色名，其余步骤名本身就是角色名。
ROLE_NAMES = {'检查': 'Checker', '审查': 'Reviewer'}
# 错误文件里保留的子进程输出字节上限：只留最近的一段，避免输出过大撑爆产物。
CAPTURE_LIMIT = 8192


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--workdir',
        required=True,
        type=Path,
        help='工作目录路径（相对当前工作目录解析，存放 brief、diagnosis、dist 等产物）',
    )
    parser.add_argument(
        '--raw',
        type=Path,
        default=ROOT / 'da' / 'sample-raw',
        help='原始数据目录路径（相对示例目录解析，透传给 DA；默认使用 sample-raw）',
    )
    parser.add_argument(
        '--bundle',
        type=Path,
        help='前端 bundle 文件路径（相对示例目录解析，透传给 Builder；默认优先使用 demo bundle）',
    )
    # 角色脚本路径相对示例目录解析，默认值是示例目录内的绝对路径。
    parser.add_argument('--boss', type=Path, default=ROOT / 'boss' / 'make_brief.py',
                        help='Boss 角色脚本路径（相对示例目录解析；默认使用示例目录内的绝对路径）')
    parser.add_argument('--da', type=Path, default=ROOT / 'da' / 'build_diagnosis.py',
                        help='DA 角色脚本路径（相对示例目录解析；默认使用示例目录内的绝对路径）')
    parser.add_argument('--builder', type=Path, default=ROOT / 'builder' / 'build_site.py',
                        help='Builder 角色脚本路径（相对示例目录解析；默认使用示例目录内的绝对路径）')
    parser.add_argument('--checker', type=Path, default=ROOT / 'builder' / 'check_site.py',
                        help='站点自检脚本路径（相对示例目录解析；默认使用示例目录内的绝对路径）')
    parser.add_argument('--reviewer', type=Path, default=ROOT / 'review' / 'cross_check.py',
                        help='审查脚本路径（相对示例目录解析；默认使用示例目录内的绝对路径；'
                             '脚本需支持 --json-out 写出 JSON 结论供运行器汇总）')
    parser.add_argument('--review', action='store_true', help=f'检查后运行交叉审阅并写入 {REVIEW_REPORT}')
    parser.add_argument(
        '--timeout',
        type=float,
        default=None,
        help='每个角色子进程的超时时间（秒；省略表示不限制）',
    )
    parser.add_argument(
        '--review-report',
        type=Path,
        default=None,
        help=f'审查报告路径（相对当前工作目录解析；默认写到 <workdir>/{REVIEW_REPORT}）',
    )
    return parser.parse_args(argv)


def prepare_workdir(workdir):
    """确保工作目录可用；不可用时返回可读的失败提示，可用时返回 None。"""
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        # 目标路径被同名文件占用时 mkdir 会抛异常，转成可读提示。
        return f'工作目录不可用: {workdir} (路径已被文件占用)'
    except OSError as error:
        return f'工作目录不可用: {workdir} ({error.strerror or str(error)})'
    if not os.access(workdir, os.W_OK):
        return f'工作目录不可写: {workdir} (权限不足，无法写入产物)'
    return None


def open_report(path):
    """打开审查报告文件；被占用或不可写时给出可读提示并返回 None。"""
    try:
        # 报告路径可以指到临时目录，父目录不存在时先建出来。
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open('w', encoding='utf-8')
    except OSError as error:
        print(f'审查报告无法写入: {path} ({error.strerror or str(error)})', file=sys.stderr, flush=True)
        return None


def staged_path(path, label):
    """返回与目标同目录、每次运行唯一的暂存路径。"""
    return path.with_name(f'.{path.name}.{label}.{uuid.uuid4().hex}.tmp')


def commit_artifact(source, target):
    """以替换方式提交产物；替换失败时恢复原目标。"""
    backup = staged_path(target, 'backup')
    moved_old = False
    try:
        if target.exists():
            os.replace(target, backup)
            moved_old = True
        os.replace(source, target)
    except OSError:
        if moved_old:
            os.replace(backup, target)
        raise
    else:
        if moved_old:
            if backup.is_dir():
                import shutil
                shutil.rmtree(backup)
            else:
                backup.unlink()


def undecodable_reason(path):
    """返回报告不可按 UTF-8 解码的原因；报告可解码（或读不出来）时返回 None。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        data.decode('utf-8')
    except UnicodeDecodeError as error:
        return f'偏移 {error.start} 处无法解码'
    return None


def write_timings(workdir, timings):
    """写出已执行步骤的耗时记录；失败路径也由调用方在 finally 中调用。

    写不进去时点名没能记录的步骤耗时：只看到「写入失败」无法判断缺了哪几步。
    """
    path = workdir / TIMINGS_FILE
    try:
        path.write_text(json.dumps(timings, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except OSError as error:
        message = f'耗时记录写入失败: {path} ({error.strerror or str(error)})'
        missing = '、'.join(str(item.get('name', '')) for item in timings)
        if missing:
            message += f'；缺失的步骤耗时: {missing}'
        print(message, file=sys.stderr, flush=True)
        return message
    return None


def write_status(workdir, payload):
    """写出步骤状态文件；写不进去时给出可读提示并返回提示文本，可用时返回 None。"""
    path = workdir / STATUS_FILE
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except OSError as error:
        message = f'状态文件写入失败: {path} ({error.strerror or str(error)})'
        print(message, file=sys.stderr, flush=True)
        return message
    return None


def status_steps(timings):
    """把耗时记录整理成每步状态：步骤名、稳定角色名、退出码与耗时。"""
    return [{
        'name': item['name'],
        'role': STEP_ROLES.get(item['name'], item['name']),
        'exit_code': item['exit_code'],
        'duration_seconds': item['duration_seconds'],
    } for item in timings]


def finding_severity(finding):
    """返回一条审查发现的严重级；缺少或非法时按阻断级处理。"""
    if isinstance(finding, dict) and isinstance(finding.get('severity'), str):
        severity = finding['severity'].strip()
        if severity:
            return severity
    return DEFAULT_SEVERITY


def finding_detail(finding):
    """返回一条审查发现的简短说明：路径与消息。"""
    if not isinstance(finding, dict):
        return str(finding)
    path = finding.get('path')
    message = finding.get('message')
    parts = [str(part) for part in (path, message) if part]
    text = '：'.join(parts) if parts else str(finding)
    if len(text) > BLOCKING_DETAIL_LENGTH:
        text = text[:BLOCKING_DETAIL_LENGTH] + '…'
    return text


def summarize_review(path):
    """汇总交叉审查结论：发现数、各严重级计数与是否阻断。

    结果文件不可读或结构不符时返回 None：审查步骤本身的成败已由退出码判定，
    汇总读不出来只说明结论无法结构化，不改写步骤结果。
    """
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    findings = data.get('findings')
    if not isinstance(findings, list):
        return None
    by_severity: dict[str, int] = {}
    blocking_findings = 0
    details = []
    for finding in findings:
        severity = finding_severity(finding)
        by_severity[severity] = by_severity.get(severity, 0) + 1
        if severity in BLOCKING_SEVERITIES:
            blocking_findings += 1
            if len(details) < BLOCKING_DETAIL_LIMIT:
                details.append(finding_detail(finding))
    return {
        'findings': len(findings),
        'blocking_findings': blocking_findings,
        'by_severity': {key: by_severity[key] for key in sorted(by_severity)},
        'blocking': blocking_findings > 0,
        'details': details,
    }


def describe_blocking(summary):
    """拼出阻断级发现的说明文本：数量、严重级计数与前几条明细。"""
    counts = '、'.join(f'{key} {value}' for key, value in summary['by_severity'].items())
    text = (f'审查发现阻断级问题：{summary["blocking_findings"]} 项'
            f'（共 {summary["findings"]} 项发现')
    if counts:
        text += f'；严重级计数：{counts}'
    text += '）'
    if summary['details']:
        text += '：' + '；'.join(summary['details'])
    return text


def review_status(requested, called, results_path, summary):
    """整理审查结论字段：请求与调用状态、发现数、各严重级计数与是否阻断。"""
    payload = {
        'requested': bool(requested),
        'called': bool(called),
        'results_path': str(results_path) if requested else None,
        # 汇总是否真的读到了：读不到时下面几项只是缺省值，不代表没有发现。
        'summarized': summary is not None,
        'findings': None,
        'blocking_findings': 0,
        'by_severity': {},
        'blocking': False,
    }
    if summary is not None:
        payload['findings'] = summary['findings']
        payload['blocking_findings'] = summary['blocking_findings']
        payload['by_severity'] = summary['by_severity']
        payload['blocking'] = summary['blocking']
    return payload


def resolve_script(script):
    """角色脚本路径解析：绝对路径原样使用，相对路径以示例目录为基准。

    boss/da/builder/checker/reviewer 共用这条规则：无论从哪个目录调用，
    相对路径都按本文件所在的示例目录解析；脚本缺失时按同一基准给出可读提示。
    """
    return script if script.is_absolute() else ROOT / script


def resolve_example_path(path):
    """解析角色使用的输入路径：相对路径以示例目录为基准，绝对路径原样使用。"""
    return path if path.is_absolute() else ROOT / path


def missing_script(command):
    """返回命令里缺失的角色脚本路径；脚本存在或不是本地脚本时返回 None。

    角色脚本按示例目录解析：相对路径先落到示例目录再检查，
    提示里给出实际查找的路径。
    """
    script = Path(command[1]) if command[0] == sys.executable and len(command) > 1 else None
    if script is None:
        return None
    resolved = resolve_script(script)
    return None if resolved.is_file() else resolved


def missing_script_label(name):
    """返回角色脚本缺失时的错误标签：中文步骤名补上英文角色名。"""
    return f'{ROLE_NAMES.get(name, name)} 脚本文件不存在'


def forward_output(source, sink, buffer):
    """转发子进程输出：一边写回当前流，一边留最近的一段副本。

    副本供失败步骤错误文件记录；管道被关闭时安静退出，
    转发失败不该盖掉真正的步骤失败原因。
    """
    size = 0
    try:
        for chunk in iter(source.readline, b''):
            buffer.append(chunk)
            size += len(chunk)
            while size > CAPTURE_LIMIT and len(buffer) > 1:
                # 只留最近的一段：错误信息通常在输出末尾。
                size -= len(buffer.pop(0))
            target = getattr(sink, 'buffer', None)
            if target is None:
                sink.write(chunk.decode('utf-8', 'replace'))
            else:
                target.write(chunk)
            sink.flush()
    except (OSError, ValueError):
        pass


def output_text(captured):
    """把捕获的输出按 UTF-8 替换解码，并去掉首尾空白。"""
    return b''.join(captured).decode('utf-8', 'replace').strip()


def failure_reason(text, captured):
    """拼出失败步骤的原因文本：步骤自身的原因加上子进程 stderr 副本。"""
    detail = output_text(captured)
    return f'{text}\n{detail}' if detail else text


def exit_reason(exit_code):
    """返回子进程退出原因；被信号终止时同时给出稳定的信号名。"""
    if exit_code is not None and exit_code < 0:
        signal_number = -exit_code
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = f'SIG{signal_number}'
        return f'被信号终止 ({signal_name}, 信号 {signal_number})'
    return f'退出码 {exit_code}'


def write_step_error(path, command, exit_code, stdout_capture, stderr_capture):
    """写出失败步骤的命令、退出码及 stdout/stderr 尾部。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'command': list(map(str, command)),
            'exit_code': exit_code,
            'stdout_tail': output_text(stdout_capture),
            'stderr_tail': output_text(stderr_capture),
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except OSError as error:
        print(f'步骤错误文件写入失败: {path} ({error.strerror or str(error)})',
              file=sys.stderr, flush=True)


def run_step(name, command, timings, stdout=None, stderr=None, output_path=None,
             timeout=None, error_path=None):
    """运行一个角色步骤；返回 (成功与否, 失败原因, 子进程是否启动)。

    未指定的 stdout/stderr 一边转发到当前流（保持实时输出），一边留存尾部副本。
    失败时副本写入步骤错误文件；已指定流（审查步骤写进报告）时不重复捕获。
    """
    print(f'[{name}] 命令: {" ".join(map(str, command))}', flush=True)
    started = time.monotonic()
    capture_stdout = stdout is None
    capture_stderr = stderr is None
    stdout_capture = []
    stderr_capture = []
    pumps = []
    try:
        # Python 将缺失的脚本作为退出码报告，而不是抛出异常；先检查本地角色脚本，
        # 这样错误能归属到具体步骤，避免只留下子进程的错误输出。
        missing = missing_script(command)
        if missing is not None:
            raise FileNotFoundError(2, 'No such file or directory', str(missing))
        process = subprocess.Popen(command,
                                   stdout=(subprocess.PIPE if capture_stdout else stdout),
                                   stderr=(subprocess.PIPE if capture_stderr else stderr),
                                   start_new_session=True)
        for stream, sink, buffer in ((process.stdout, sys.stdout, stdout_capture),
                                     (process.stderr, sys.stderr, stderr_capture)):
            if stream is not None:
                pump = threading.Thread(target=forward_output,
                                        args=(stream, sink, buffer), daemon=True)
                pump.start()
                pumps.append(pump)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # 每个角色都是新会话的组长；杀整个进程组，避免角色再派生的进程继续
            # 持有报告/管道或让编排器在超时后假成功。
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            elapsed = time.monotonic() - started
            timings.append({'name': name, 'command': list(map(str, command)),
                            'duration_seconds': elapsed, 'exit_code': process.returncode,
                            'timeout': True,
                            'reason': f'超时（超过 {timeout:g} 秒）'})
            print(f'[{name}] 耗时: {elapsed:.2f}s', flush=True)
            print(f'步骤失败: {name} (超过 {timeout:g} 秒，已回收进程组)',
                  file=sys.stderr, flush=True)
            if error_path is not None:
                write_step_error(error_path, command, process.returncode,
                                 stdout_capture, stderr_capture)
            return False, failure_reason(
                f'步骤失败: {name} (超过 {timeout:g} 秒，已回收进程组)', stderr_capture), True
        finally:
            for pump in pumps:
                # 子进程退出后管道写端关闭，转发线程自行结束。
                pump.join(timeout=2)
            for stream in (process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
        result = process
    except FileNotFoundError as error:
        elapsed = time.monotonic() - started
        timings.append({'name': name, 'command': list(map(str, command)),
                        'duration_seconds': elapsed, 'exit_code': 2})
        print(f'[{name}] 耗时: {elapsed:.2f}s', flush=True)
        # 角色脚本缺失时点名角色与脚本路径：调用方不必再猜是哪一步的角色没跑起来。
        text = f'步骤失败: {name} ({missing_script_label(name)}: {error.filename})'
        print(text, file=sys.stderr, flush=True)
        if error_path is not None:
            write_step_error(error_path, command, 2, stdout_capture, stderr_capture)
        return False, failure_reason(text, stderr_capture), False
    elapsed = time.monotonic() - started
    # 步骤输出写进报告时校验编码：审查器输出不可解码时也算这一步失败，
    # 免得报告已损坏却仍然报成功。
    reason = undecodable_reason(output_path) if output_path is not None else None
    exit_code = result.returncode
    if not exit_code and reason is not None:
        # 步骤本身返回 0 但产物已损坏：按数据错误记 2，耗时记录里不留成功。
        exit_code = 2
    entry = {'name': name, 'command': list(map(str, command)),
             'duration_seconds': elapsed, 'exit_code': exit_code}
    if exit_code is not None and exit_code < 0:
        # 被信号终止：把稳定的信号名一并记进耗时记录，
        # 失败原因不必再翻 stderr 或日志才能核对。
        entry['reason'] = exit_reason(exit_code)
    timings.append(entry)
    print(f'[{name}] 耗时: {elapsed:.2f}s', flush=True)
    if result.returncode:
        print(f'步骤失败: {name} ({exit_reason(result.returncode)})', file=sys.stderr, flush=True)
    if reason is not None:
        print(f'步骤失败: {name} (输出含非 UTF-8 字节: {output_path}, {reason})', file=sys.stderr, flush=True)
        text = f'步骤失败: {name} (输出含非 UTF-8 字节: {output_path}, {reason})'
    elif exit_code:
        text = f'步骤失败: {name} ({exit_reason(exit_code)})'
    else:
        text = None
    if text is not None and error_path is not None:
        write_step_error(error_path, command, exit_code, stdout_capture, stderr_capture)
    error_text = failure_reason(text, stderr_capture) if text is not None else None
    return result.returncode == 0 and reason is None, error_text, True


def source_commit():
    """返回当前 git 仓库提交；不在仓库内时返回 None。"""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def main(argv=None):
    args = parse_args(argv)
    raw = resolve_example_path(args.raw)
    bundle = resolve_example_path(args.bundle) if args.bundle is not None else None
    scripts = {
        name: resolve_script(getattr(args, name))
        for name in ('boss', 'da', 'builder', 'checker', 'reviewer')
    }
    if bundle is None:
        bundled_demo = ROOT / 'demo' / 'vendor' / 'app.bundle.js'
        if bundled_demo.is_file():
            bundle = bundled_demo
    if bundle is not None:
        print(f'使用 bundle: {bundle}', flush=True)
    workdir = args.workdir.resolve()
    # 审查报告默认落在工作目录，产物命名不变；可显式指到别的路径（如临时目录）。
    report_path = args.review_report.resolve() if args.review_report is not None else workdir / REVIEW_REPORT
    # 工作目录被占用或不可写时先给出可读提示，避免只留下裸 Traceback。
    problem = prepare_workdir(workdir)
    if problem is not None:
        print(problem, file=sys.stderr, flush=True)
        return 1
    timings = []
    brief = workdir / 'brief.json'
    diagnosis = workdir / 'diagnosis.json'
    brief_stage = staged_path(brief, 'boss')
    diagnosis_stage = staged_path(diagnosis, 'da')
    dist = workdir / 'dist'
    # 审查结论的结构化结果文件：审查脚本按 --json-out 写出，运行器据此汇总。
    review_results = workdir / REVIEW_RESULTS
    results_stage = staged_path(review_results, 'review')
    # 角色脚本与原始数据路径按示例目录解析，避免依赖调用时的 cwd；
    # 绝对路径（含默认值）原样使用；
    # boss/da/builder/checker/reviewer 共用这条解析规则；
    # 产物一律写到工作目录，与角色脚本路径所在目录无关。
    commands = [
        ('Boss', [sys.executable, str(scripts['boss']), '--out', str(brief_stage)]),
        ('DA', [sys.executable, str(scripts['da']), '--brief', str(brief), '--raw', str(raw), '--out', str(diagnosis_stage)]),
    ]
    dist_stage = staged_path(dist, 'builder')
    build = [sys.executable, str(scripts['builder']), '--data', str(diagnosis), '--out', str(dist_stage)]
    commit = source_commit()
    if commit is not None:
        build.extend(['--source-commit', commit])
    if bundle:
        build.extend(['--bundle', str(bundle)])
    commands.extend([
        ('Builder', build),
        ('检查', [sys.executable, str(scripts['checker']), str(dist_stage)]),
    ])
    status = 0
    failed = []
    step_errors = []
    step_error_files = []
    problems = []
    reviewer_called = False
    review_summary = None
    try:
        for name, command in commands:
            error_path = workdir / STEP_ERROR_DIR / f'{STEP_ROLES.get(name, name)}.json'
            ok, error, _started = run_step(name, command, timings, timeout=args.timeout,
                                           error_path=error_path)
            if not ok:
                failed.append(name)
                stage_path = {'Boss': brief_stage, 'DA': diagnosis_stage,
                              'Builder': dist_stage, '检查': dist_stage}.get(name)
                if stage_path is not None and stage_path.exists():
                    error = f'{error}\n诊断产物暂存路径: {stage_path}'
                step_errors.append(error)
                step_error_files.append(str(error_path))
                status = 1
                break
            if name in ('Boss', 'DA', '检查'):
                try:
                    stage, target = {'Boss': (brief_stage, brief), 'DA': (diagnosis_stage, diagnosis),
                                     '检查': (dist_stage, dist)}[name]
                    commit_artifact(stage, target)
                except OSError as error:
                    failed.append(name)
                    step_errors.append(f'{name} 产物提交失败: {error}')
                    status = 1
                    break
        if status == 0 and args.review:
            review = [sys.executable, str(scripts['reviewer']), '--brief', str(brief), '--data', str(diagnosis), '--dist', str(dist),
                      '--json-out', str(results_stage)]
            # 上一次运行留下的结论不能当成本次的：先清掉结果文件。
            try:
                results_stage.unlink(missing_ok=True)
            except OSError:
                pass
            error_path = workdir / STEP_ERROR_DIR / 'review.json'
            # 审查脚本缺失：审查从未启动，不写出空的审查报告，只点名角色与脚本路径。
            missing = missing_script(review)
            if missing is not None:
                ok, error, started = run_step('审查', review, timings, timeout=args.timeout,
                                              error_path=error_path)
                failed.append('审查')
                step_errors.append(error)
                step_error_files.append(str(error_path))
                status = 1
                print('review 角色失败：审查脚本不存在，未写出审查报告',
                      file=sys.stderr, flush=True)
                print(f'审查脚本: {missing}', file=sys.stderr, flush=True)
            else:
                if report_path.is_dir():
                    status = 1
                    problems.append(f'审查报告无法写入: {report_path} (路径被目录占用)')
                    report_stage = None
                else:
                    report_stage = staged_path(report_path, 'review')
                report = open_report(report_stage) if report_stage is not None else None
                if report is None:
                    status = 1
                    message = f'审查报告无法写入: {report_path}'
                    problems.append(message)
                    print(message, file=sys.stderr, flush=True)
                else:
                    with report:
                        # 审查脚本的 stdout 与 stderr 都写进报告：参数失败等信息留在产物里，便于核对。
                        ok, error, started = run_step('审查', review, timings, stdout=report, stderr=subprocess.STDOUT,
                                                     output_path=report_stage, timeout=args.timeout,
                                                     error_path=error_path)
                        # 审查进程真的起来了才算调用过审查角色。
                        reviewer_called = started
                        if not ok:
                            failed.append('审查')
                            step_errors.append(f'{error}\n审查报告: {report_stage}'.strip())
                            status = 1
                            # 同时标出稳定的角色名，便于调用方不依赖本地化步骤名定位 review 失败。
                            print('review 角色失败：本次审查报告已保留，请检查报告路径和 timings.json',
                                  file=sys.stderr, flush=True)
                            print(f'审查报告: {report_stage}', file=sys.stderr, flush=True)
                        else:
                            try:
                                commit_artifact(report_stage, report_path)
                            except OSError as error:
                                status = 1
                                failed.append('审查')
                                step_errors.append(f'审查报告提交失败: {error}; 审查报告: {report_stage}')
                        has_current_results = results_stage.is_file()
                        current_results_path = results_stage if has_current_results else None
                        if ok and has_current_results:
                            try:
                                commit_artifact(results_stage, review_results)
                                current_results_path = review_results
                            except OSError as error:
                                status = 1
                                problems.append(f'审查结果提交失败: {error}')
                    # 只汇总本次审查生成的暂存或已提交结果，绝不读取旧 JSON。
                    current_results = current_results_path if ok and has_current_results else None
                    review_summary = summarize_review(current_results) if current_results else None
                    if review_summary is not None and review_summary['blocking']:
                        message = describe_blocking(review_summary)
                        print(message, file=sys.stderr, flush=True)
                        print(f'审查报告: {report_path}', file=sys.stderr, flush=True)
                        print(f'审查结果: {review_results}', file=sys.stderr, flush=True)
                        if status == 0:
                            # 审查角色自身返回 0 但结论里有阻断级发现：同样判失败，不误报成功。
                            status = 1
                            failed.append('审查')
                            step_errors.append(message)
    finally:
        if failed:
            # 每步耗时可能缺失（耗时记录写不出来），先点名失败步骤，
            # 避免只看到「写入失败」却不知道是哪一步挂了。
            print(f'失败步骤: {"、".join(failed)}', file=sys.stderr, flush=True)
        if args.review and failed and not reviewer_called and failed[-1] != '审查':
            # 上游步骤失败时审查角色不会被调用：明确说出来，避免误以为审查已经跑过。
            print('审查角色未调用：上游步骤失败', file=sys.stderr, flush=True)
        # 耗时记录写不进去时整体判失败，不误报成功。
        problem = write_timings(workdir, timings)
        if problem is not None:
            status = 1
            problems.append(problem)
        # 步骤状态文件：失败步骤、错误与耗时都写进去，失败后不必翻日志。
        payload = {
            'status': 'ok' if status == 0 else 'failed',
            'failed_step': STEP_ROLES.get(failed[-1], failed[-1]) if failed else None,
            'error': step_errors[-1] if step_errors else None,
            'error_file': step_error_files[-1] if step_error_files else None,
            'steps': status_steps(timings),
            'review_requested': bool(args.review),
            'reviewer_called': reviewer_called,
            # 审查结论汇总：发现数、各严重级计数与是否阻断。
            'review': review_status(bool(args.review), reviewer_called,
                                    review_results if review_summary is not None else None, review_summary),
            'problems': problems,
            'timings': timings,
        }
        problem = write_status(workdir, payload)
        if problem is not None:
            status = 1
    if status == 0:
        print(f'预览命令: python3 {ROOT / "serve.py"} --root {dist}', flush=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
