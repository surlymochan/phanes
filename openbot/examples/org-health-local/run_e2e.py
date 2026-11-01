#!/usr/bin/env python3
"""Run the synthetic Boss to DA to Builder pipeline."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 交叉审查产物路径：审查失败时同样写出，便于核对失败原因。
REVIEW_REPORT = 'review.md'


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
        help='原始数据目录路径（相对当前工作目录解析，透传给 DA；默认使用 sample-raw）',
    )
    parser.add_argument(
        '--bundle',
        type=Path,
        help='前端 bundle 文件路径（相对当前工作目录解析，透传给 Builder；默认优先使用 demo bundle）',
    )
    # 角色脚本路径按当前工作目录解析：相对路径以调用时的 cwd 为基准，
    # 默认值是示例目录内的绝对路径，不随 cwd 变化。
    parser.add_argument('--boss', type=Path, default=ROOT / 'boss' / 'make_brief.py',
                        help='Boss 角色脚本路径（相对当前工作目录解析；默认使用示例目录内的绝对路径）')
    parser.add_argument('--da', type=Path, default=ROOT / 'da' / 'build_diagnosis.py',
                        help='DA 角色脚本路径（相对当前工作目录解析；默认使用示例目录内的绝对路径）')
    parser.add_argument('--builder', type=Path, default=ROOT / 'builder' / 'build_site.py',
                        help='Builder 角色脚本路径（相对当前工作目录解析；默认使用示例目录内的绝对路径）')
    parser.add_argument('--checker', type=Path, default=ROOT / 'builder' / 'check_site.py',
                        help='站点自检脚本路径（相对当前工作目录解析；默认使用示例目录内的绝对路径）')
    parser.add_argument('--reviewer', type=Path, default=ROOT / 'review' / 'cross_check.py',
                        help='审查脚本路径（相对当前工作目录解析；默认使用示例目录内的绝对路径）')
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
    """写出耗时记录；写不进去时给出可读提示并返回 False。"""
    path = workdir / 'timings.json'
    try:
        path.write_text(json.dumps(timings, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except OSError as error:
        print(f'耗时记录写入失败: {path} ({error.strerror or str(error)})', file=sys.stderr, flush=True)
        return False
    return True


def resolve_script(script):
    """角色脚本路径解析：绝对路径原样使用，相对路径以调用时的当前工作目录为基准。

    boss/da/builder/checker/reviewer 共用这条规则：无论从哪个目录调用，
    相对路径都按调用时的 cwd 解析；脚本缺失时按同一基准给出可读提示。
    """
    return script if script.is_absolute() else Path.cwd() / script


def run_step(name, command, timings, stdout=None, stderr=None, output_path=None, timeout=None):
    print(f'[{name}] 命令: {" ".join(map(str, command))}', flush=True)
    started = time.monotonic()
    try:
        # Python 将缺失的脚本作为退出码报告，而不是抛出异常；先检查本地角色脚本，
        # 这样错误能归属到具体步骤，避免只留下子进程的错误输出。
        script = Path(command[1]) if command[0] == sys.executable and len(command) > 1 else None
        if script is not None:
            # 角色脚本按调用时的 cwd 解析：相对路径先落到当前工作目录再检查，
            # 提示里给出实际查找的路径。
            resolved = resolve_script(script)
            if not resolved.is_file():
                raise FileNotFoundError(2, 'No such file or directory', str(resolved))
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True)
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
            return False
        result = process
    except FileNotFoundError as error:
        elapsed = time.monotonic() - started
        timings.append({'name': name, 'command': list(map(str, command)),
                        'duration_seconds': elapsed, 'exit_code': 2})
        print(f'[{name}] 耗时: {elapsed:.2f}s', flush=True)
        print(f'步骤失败: {name} (文件不存在: {error.filename})', file=sys.stderr, flush=True)
        return False
    elapsed = time.monotonic() - started
    # 步骤输出写进报告时校验编码：审查器输出不可解码时也算这一步失败，
    # 免得报告已损坏却仍然报成功。
    reason = undecodable_reason(output_path) if output_path is not None else None
    exit_code = result.returncode
    if not exit_code and reason is not None:
        # 步骤本身返回 0 但产物已损坏：按数据错误记 2，耗时记录里不留成功。
        exit_code = 2
    timings.append({'name': name, 'command': list(map(str, command)),
                    'duration_seconds': elapsed, 'exit_code': exit_code})
    print(f'[{name}] 耗时: {elapsed:.2f}s', flush=True)
    if result.returncode:
        print(f'步骤失败: {name} (退出码 {result.returncode})', file=sys.stderr, flush=True)
    if reason is not None:
        print(f'步骤失败: {name} (输出含非 UTF-8 字节: {output_path}, {reason})', file=sys.stderr, flush=True)
    return result.returncode == 0 and reason is None


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
    bundle = args.bundle
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
    # 角色脚本路径原样传给子进程：子进程继承调用时的 cwd，相对路径按同一基准解析
    # （与 resolve_script 的检查规则一致），绝对路径（含默认值）原样使用；
    # boss/da/builder/checker/reviewer 共用这条解析规则；
    # 产物一律写到工作目录，与角色脚本路径所在目录无关。
    commands = [
        ('Boss', [sys.executable, str(args.boss), '--out', str(brief)]),
        ('DA', [sys.executable, str(args.da), '--brief', str(brief), '--raw', str(args.raw), '--out', str(diagnosis)]),
    ]
    build = [sys.executable, str(args.builder), '--data', str(diagnosis), '--out', str(dist)]
    commit = source_commit()
    if commit is not None:
        build.extend(['--source-commit', commit])
    if bundle:
        build.extend(['--bundle', str(bundle)])
    commands.extend([
        ('Builder', build),
        ('检查', [sys.executable, str(args.checker), str(dist)]),
    ])
    status = 0
    failed = []
    try:
        for name, command in commands:
            if not run_step(name, command, timings, timeout=args.timeout):
                failed.append(name)
                status = 1
                break
        if status == 0 and args.review:
            review = [sys.executable, str(args.reviewer), '--brief', str(brief), '--data', str(diagnosis), '--dist', str(dist)]
            report = open_report(report_path)
            if report is None:
                status = 1
            else:
                with report:
                    # 审查脚本的 stdout 与 stderr 都写进报告：参数失败等信息留在产物里，便于核对。
                    if not run_step('审查', review, timings, stdout=report, stderr=subprocess.STDOUT,
                                   output_path=report_path, timeout=args.timeout):
                        failed.append('审查')
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
        # 耗时记录写不进去时整体判失败，不误报成功。
        if not write_timings(workdir, timings):
            status = 1
    if status == 0:
        print(f'预览命令: python3 {ROOT / "serve.py"} --root {dist}', flush=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
