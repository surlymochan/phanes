#!/usr/bin/env python3
"""Run review tests and capture replayable evidence for a fixed checkout."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys

EVIDENCE = Path(__file__).with_name('evidence.py')


def execute(args, cwd, timeout, log):
    try:
        proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            output, _ = proc.communicate()
            log.write_bytes(output + b'\nTIMEOUT\n')
            return None
        log.write_bytes(output)
        return proc.returncode
    except OSError as exc:
        log.write_text(str(exc) + '\n')
        return None


def git(root, *args):
    result = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError('git command failed')
    return result.stdout.strip()


def main():
    p = argparse.ArgumentParser()
    for name in ('project', 'evalid', 'evidence', 'change-id', 'cmd-visible', 'cmd-holdout'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--phase', required=True, choices=('red', 'green', 'posthoc'))
    p.add_argument('--timeout', type=float, default=600)
    a = p.parse_args()
    root, ev, out = (Path(x).resolve() for x in (a.project, a.evalid, a.evidence))
    result = {'change_id': a.change_id, 'commit': None, 'evalid_commit': None,
              'visible': {'exit': None, 'log_path': None}, 'holdout': {'exit': None, 'log_path': None},
              'replay': {'ok': False}, 'classification': 'posthoc' if a.phase == 'posthoc' else 'tdd',
              'verdict': 'INFRA'}
    try:
        if a.timeout <= 0 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', a.change_id):
            raise ValueError('invalid timeout or change id')
        if out == root or root in out.parents or out == ev or ev in out.parents:
            raise ValueError('evidence must be outside project and evalid')
        out.mkdir(parents=True, exist_ok=True)
        result['commit'] = git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
        result['evalid_commit'] = git(ev, 'rev-parse', '--verify', 'HEAD^{commit}')
        if git(ev, 'status', '--porcelain=v1', '--untracked-files=all'):
            raise ValueError('dirty evalid checkout')
        for atom in ('i6a_tdd_run.py', 'i6b_tdd_proof_validate.py'):
            if not (ev / 'xflow' / 'atoms' / atom).is_file():
                raise ValueError('evalid atom missing')
        status = git(root, 'status', '--porcelain=v1', '--untracked-files=all')
        prefix = f'specs/changes/{a.change_id}/tdd/'
        for line in status.splitlines():
            if not (a.phase == 'green' and line.startswith('?? ' + prefix)):
                raise ValueError('dirty project checkout')
        # Require a concrete external file in the command; isolation and permissions
        # of that file and the evidence directory are supplied by the caller.
        if a.phase != 'red':
            paths = [Path(s) for s in shlex.split(a.cmd_holdout) if Path(s).is_absolute()]
            if not paths or not any(p.is_file() and root not in p.resolve().parents and p.resolve() != root
                                    and out not in p.resolve().parents for p in paths):
                raise ValueError('holdout command must reference an external file')
        def test(label, command):
            log = out / (a.phase + '-' + label + '.log')
            result[label]['log_path'] = str(log)
            code = execute(['/bin/sh', '-c', command], root, a.timeout, log)
            result[label]['exit'] = code
            if code is None or code in (126, 127) or code < 0:
                raise RuntimeError('test command failed to start or timed out')
            return code
        visible = test('visible', a.cmd_visible)
        if (a.phase == 'red' and visible == 0) or (a.phase != 'red' and visible != 0):
            result['verdict'] = 'FAIL'
            return result
        if a.phase != 'red' and test('holdout', a.cmd_holdout) != 0:
            result['verdict'] = 'FAIL'
            return result
        base = [sys.executable, str(EVIDENCE), '--project', str(root), '--evalid', str(ev),
                '--evidence', str(out), '--change-id', a.change_id, '--command', a.cmd_visible]
        def evidence(action, phase=None):
            log = out / (a.phase + '-' + action + '.log')
            args = base[:2] + [action] + base[2:]
            if phase:
                args += ['--phase', phase]
            code = execute(args, root, a.timeout, log)
            if code is None:
                raise RuntimeError('evidence command timed out or could not start')
            if code:
                raise ValueError('evidence ' + action + ' failed; see ' + log.name)
        evidence('capture', a.phase)
        if a.phase != 'red':
            evidence('verify')
            result['replay']['ok'] = True
        result['verdict'] = 'PASS'
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result['error'] = str(exc)
    finally:
        try:
            # Never write into the project even when validation fails.
            if out != root and root not in out.parents and out != ev and ev not in out.parents:
                out.mkdir(parents=True, exist_ok=True)
                tmp = out / 'result.json.tmp'
                tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
                tmp.replace(out / 'result.json')
        except OSError as exc:
            result['verdict'] = 'INFRA'
            result['error'] = str(exc)
    return result


if __name__ == '__main__':
    result = main()
    print(json.dumps(result, sort_keys=True))
    sys.exit(0 if result['verdict'] == 'PASS' else 1)
