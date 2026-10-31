#!/usr/bin/env python3
"""Run the synthetic Boss to DA to Builder pipeline."""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


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
    parser.add_argument('--review', action='store_true', help='检查后运行交叉审阅并写入 review.md')
    return parser.parse_args(argv)


def run_step(name, command, stdout=None):
    print(f'[{name}] 命令: {" ".join(map(str, command))}', flush=True)
    started = time.monotonic()
    result = subprocess.run(command, stdout=stdout)
    elapsed = time.monotonic() - started
    print(f'[{name}] 耗时: {elapsed:.2f}s', flush=True)
    if result.returncode:
        print(f'步骤失败: {name} (退出码 {result.returncode})', file=sys.stderr, flush=True)
        return False
    return True


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
    workdir.mkdir(parents=True, exist_ok=True)
    brief = workdir / 'brief.json'
    diagnosis = workdir / 'diagnosis.json'
    dist = workdir / 'dist'
    commands = [
        ('Boss', [sys.executable, str(args.boss), '--out', str(brief)]),
        ('DA', [sys.executable, str(args.da), '--brief', str(brief), '--raw', str(args.raw), '--out', str(diagnosis)]),
    ]
    build = [sys.executable, str(args.builder), '--data', str(diagnosis), '--out', str(dist)]
    if bundle:
        build.extend(['--bundle', str(bundle)])
    commands.extend([
        ('Builder', build),
        ('检查', [sys.executable, str(args.checker), str(dist)]),
    ])
    for name, command in commands:
        if not run_step(name, command):
            return 1
    if args.review:
        review = [sys.executable, str(args.reviewer), '--brief', str(brief), '--data', str(diagnosis), '--dist', str(dist)]
        with (workdir / 'review.md').open('w', encoding='utf-8') as output:
            if not run_step('审查', review, stdout=output):
                return 1
    print(f'预览命令: python3 {ROOT / "serve.py"} --root {dist}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
