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
    return parser.parse_args(argv)


def run_step(name, command):
    print(f'[{name}] 命令: {" ".join(map(str, command))}', flush=True)
    started = time.monotonic()
    result = subprocess.run(command)
    elapsed = time.monotonic() - started
    print(f'[{name}] 耗时: {elapsed:.2f}s', flush=True)
    if result.returncode:
        print(f'步骤失败: {name} (退出码 {result.returncode})', file=sys.stderr, flush=True)
        return False
    return True


def main(argv=None):
    args = parse_args(argv)
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
    if args.bundle:
        build.extend(['--bundle', str(args.bundle)])
    commands.extend([('Builder', build), ('检查', [sys.executable, str(args.checker), str(dist)])])
    for name, command in commands:
        if not run_step(name, command):
            return 1
    print(f'预览命令: python3 {ROOT / "serve.py"} --root {dist}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
