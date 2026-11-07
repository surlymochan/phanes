#!/usr/bin/env python3
"""Read an upstream Git tree and inventory the vendored Swrm differences."""
import argparse
import ast
import collections
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('upstream', help='read-only upstream repository')
    parser.add_argument('--ref', default='a4ff878')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    def git(*argv):
        return subprocess.check_output(['git', '-C', args.upstream, *argv])
    baseline = git('rev-parse', args.ref + '^{commit}').decode().strip()
    counts = collections.Counter()
    differences, tests, seen = [], [], set()
    for entry in git('ls-tree', '-r', '-z', baseline).split(b'\0'):
        if not entry: continue
        meta, name = entry.split(b'\t', 1)
        mode, kind, sha = meta.split()
        name = name.decode()
        mapped = name if (root / 'swrm' / name).is_file() else name.replace('openswarm', 'swrm')
        path = root / 'swrm' / mapped
        seen.add(mapped)
        before = git('cat-file', 'blob', sha.decode())
        after = path.read_bytes() if path.is_file() else None
        status = ('deleted' if after is None else 'identical' if before == after else
                  'rename-only' if before.replace(b'openswarm', b'swrm') == after else 'modified')
        counts[status] += 1
        if status not in ('identical', 'rename-only'):
            differences.append({'status': status, 'upstream': name, 'current': 'swrm/' + mapped})
        if name.startswith('tests/') and name.endswith('.py') and status == 'modified':
            def methods(source):
                return {node.name for node in ast.walk(ast.parse(source))
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_')}
            old, new = methods(before), methods(after)
            tests.append({'path': 'swrm/' + mapped, 'before': len(old), 'after': len(new),
                          'added': sorted(new - old), 'removed': sorted(old - new)})
    tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', 'swrm/'], text=True)
    for name in tracked.splitlines():
        if name[5:] not in seen and (root / name).is_file():
            differences.append({'status': 'added', 'current': name})
            counts['added'] += 1
    print(json.dumps({'baseline': baseline,
                      'normalization': 'Existing paths retained; renamed paths and content normalize openswarm to swrm.',
                      'counts': dict(counts), 'test_method_differences': tests,
                      'differences': differences}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
