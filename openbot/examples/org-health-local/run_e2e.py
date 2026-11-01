#!/usr/bin/env python3
"""Run the synthetic Boss to DA to Builder pipeline.

失败时保留上游产物，并写出 timings.json 与 status.json 供核对：
status.json 记录每步状态、失败步骤及其错误输出；
步骤失败时另在 step-errors/ 写出该步的命令、退出码与 stdout/stderr 尾部。
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 交叉审查产物路径：审查失败时同样写出，便于核对失败原因。
REVIEW_REPORT = 'review.md'
# 步骤状态文件：写出每步状态、失败步骤与错误，不必翻日志也能定位失败原因。
STATUS_FILE = 'status.json'
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
                        help='审查脚本路径（相对示例目录解析；默认使用示例目录内的绝对路径）')
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
    path = workdir / 'timings.json'
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


def resolve_script(script):
    """角色脚本路径解析：绝对路径原样使用，相对路径以示例目录为基准。

    boss/da/builder/checker/reviewer 共用这条规则：无论从哪个目录调用，
    相对路径都按本文件所在的示例目录解析；脚本缺失时按同一基准给出可读提示。
    """
    return script if script.is_absolute() else ROOT / script


def resolve_example_path(path):
    """解析角色使用的输入路径：相对路径以示例目录为基准，绝对路径原样使用。"""
    return path if path.is_absolute() else ROOT / path


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
        script = Path(command[1]) if command[0] == sys.executable and len(command) > 1 else None
        if script is not None:
            # 角色脚本按示例目录解析：相对路径先落到示例目录再检查，
            # 提示里给出实际查找的路径。
            resolved = resolve_script(script)
            if not resolved.is_file():
                raise FileNotFoundError(2, 'No such file or directory', str(resolved))
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
        print(f'步骤失败: {name} (文件不存在: {error.filename})', file=sys.stderr, flush=True)
        if error_path is not None:
            write_step_error(error_path, command, 2, stdout_capture, stderr_capture)
        return False, failure_reason(
            f'步骤失败: {name} (文件不存在: {error.filename})', stderr_capture), False
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
    dist = workdir / 'dist'
    # 角色脚本与原始数据路径按示例目录解析，避免依赖调用时的 cwd；
    # 绝对路径（含默认值）原样使用；
    # boss/da/builder/checker/reviewer 共用这条解析规则；
    # 产物一律写到工作目录，与角色脚本路径所在目录无关。
    commands = [
        ('Boss', [sys.executable, str(scripts['boss']), '--out', str(brief)]),
        ('DA', [sys.executable, str(scripts['da']), '--brief', str(brief), '--raw', str(raw), '--out', str(diagnosis)]),
    ]
    build = [sys.executable, str(scripts['builder']), '--data', str(diagnosis), '--out', str(dist)]
    commit = source_commit()
    if commit is not None:
        build.extend(['--source-commit', commit])
    if bundle:
        build.extend(['--bundle', str(bundle)])
    commands.extend([
        ('Builder', build),
        ('检查', [sys.executable, str(scripts['checker']), str(dist)]),
    ])
    status = 0
    failed = []
    step_errors = []
    step_error_files = []
    problems = []
    reviewer_called = False
    try:
        for name, command in commands:
            error_path = workdir / STEP_ERROR_DIR / f'{STEP_ROLES.get(name, name)}.json'
            ok, error, _started = run_step(name, command, timings, timeout=args.timeout,
                                           error_path=error_path)
            if not ok:
                failed.append(name)
                step_errors.append(error)
                step_error_files.append(str(error_path))
                status = 1
                break
        if status == 0 and args.review:
            review = [sys.executable, str(scripts['reviewer']), '--brief', str(brief), '--data', str(diagnosis), '--dist', str(dist)]
            report = open_report(report_path)
            if report is None:
                status = 1
                problems.append(f'审查报告无法写入: {report_path}')
            else:
                with report:
                    # 审查脚本的 stdout 与 stderr 都写进报告：参数失败等信息留在产物里，便于核对。
                    error_path = workdir / STEP_ERROR_DIR / 'review.json'
                    ok, error, started = run_step('审查', review, timings, stdout=report, stderr=subprocess.STDOUT,
                                                 output_path=report_path, timeout=args.timeout,
                                                 error_path=error_path)
                    # 审查进程真的起来了才算调用过审查角色。
                    reviewer_called = started
                    if not ok:
                        failed.append('审查')
                        step_errors.append(f'{error}\n审查报告: {report_path}'.strip())
                        step_error_files.append(str(error_path))
                        status = 1
                        # 同时标出稳定的角色名，便于调用方不依赖本地化步骤名定位 review 失败。
                        print('review 角色失败：审查报告已保留，请检查该报告和 timings.json',
                              file=sys.stderr, flush=True)
                        print(f'审查报告: {report_path}', file=sys.stderr, flush=True)
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
