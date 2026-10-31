#!/usr/bin/env python3
"""Run the synthetic Boss to DA to Builder pipeline."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 交叉审查产物路径：审查失败时同样写出，便于核对失败原因。
REVIEW_REPORT = 'review.md'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', required=True, type=Path)
    parser.add_argument('--raw', type=Path, default=ROOT / 'da' / 'sample-raw')
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--boss', type=Path, default=ROOT / 'boss' / 'make_brief.py')
    parser.add_argument('--da', type=Path, default=ROOT / 'da' / 'build_diagnosis.py')
    parser.add_argument('--builder', type=Path, default=ROOT / 'builder' / 'build_site.py')
    parser.add_argument('--checker', type=Path, default=ROOT / 'builder' / 'check_site.py')
    parser.add_argument('--reviewer', type=Path, default=ROOT / 'review' / 'cross_check.py')
    parser.add_argument('--review', action='store_true', help=f'检查后运行交叉审阅并写入 {REVIEW_REPORT}')
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
        return path.open('w', encoding='utf-8')
    except OSError as error:
        print(f'审查报告无法写入: {path} ({error.strerror or str(error)})', file=sys.stderr, flush=True)
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


def run_step(name, command, timings, stdout=None):
    print(f'[{name}] 命令: {" ".join(map(str, command))}', flush=True)
    started = time.monotonic()
    try:
        # Python 将缺失的脚本作为退出码报告，而不是抛出异常；先检查本地角色脚本，
        # 这样错误能归属到具体步骤，避免只留下子进程的错误输出。
        script = Path(command[1]) if command[0] == sys.executable and len(command) > 1 else None
        if script is not None and not script.is_file():
            raise FileNotFoundError(2, 'No such file or directory', str(script))
        result = subprocess.run(command, stdout=stdout)
    except FileNotFoundError as error:
        elapsed = time.monotonic() - started
        timings.append({'name': name, 'command': list(map(str, command)),
                        'duration_seconds': elapsed, 'exit_code': 2})
        print(f'[{name}] 耗时: {elapsed:.2f}s', flush=True)
        print(f'步骤失败: {name} (文件不存在: {error.filename})', file=sys.stderr, flush=True)
        return False
    elapsed = time.monotonic() - started
    timings.append({'name': name, 'command': list(map(str, command)),
                    'duration_seconds': elapsed, 'exit_code': result.returncode})
    print(f'[{name}] 耗时: {elapsed:.2f}s', flush=True)
    if result.returncode:
        print(f'步骤失败: {name} (退出码 {result.returncode})', file=sys.stderr, flush=True)
        return False
    return True


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
    # 工作目录被占用或不可写时先给出可读提示，避免只留下裸 Traceback。
    problem = prepare_workdir(workdir)
    if problem is not None:
        print(problem, file=sys.stderr, flush=True)
        return 1
    timings = []
    brief = workdir / 'brief.json'
    diagnosis = workdir / 'diagnosis.json'
    dist = workdir / 'dist'
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
    try:
        for name, command in commands:
            if not run_step(name, command, timings):
                status = 1
                break
        if status == 0 and args.review:
            review = [sys.executable, str(args.reviewer), '--brief', str(brief), '--data', str(diagnosis), '--dist', str(dist)]
            report = open_report(workdir / REVIEW_REPORT)
            if report is None:
                status = 1
            else:
                with report:
                    if not run_step('审查', review, timings, stdout=report):
                        status = 1
    finally:
        # 耗时记录写不进去时整体判失败，不误报成功。
        if not write_timings(workdir, timings):
            status = 1
    if status == 0:
        print(f'预览命令: python3 {ROOT / "serve.py"} --root {dist}', flush=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
