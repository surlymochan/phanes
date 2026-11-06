#!/usr/bin/env python3
"""Run review tests and capture replayable evidence for a fixed checkout."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
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


def holdout_hashes(root):
    if not root.is_dir():
        raise ValueError('holdout directory missing')
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('holdout symlink not supported')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main():
    p = argparse.ArgumentParser()
    for name in ('project', 'evalid', 'evidence', 'change-id', 'cmd-visible', 'cmd-holdout', 'holdout-dir'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--phase', required=True, choices=('red', 'green', 'posthoc'))
    p.add_argument('--timeout', type=float, default=600)
    p.add_argument('--min-holdout-tests', type=int, default=1)
    a = p.parse_args()
    root, ev, out = (Path(x).resolve() for x in (a.project, a.evalid, a.evidence))
    result = {'change_id': a.change_id, 'commit': None, 'evalid_commit': None,
              'visible': {'exit': None, 'log_path': None}, 'holdout': {'exit': None, 'log_path': None, 'files_sha256': None},
              'replay': {'ok': False}, 'classification': 'posthoc' if a.phase == 'posthoc' else 'tdd',
              'verdict': 'INFRA'}
    holdout = Path(a.holdout_dir).resolve()
    holdout_before = None
    archived_holdout = None
    try:
        if holdout == root or root in holdout.parents:
            raise ValueError('holdout directory must be outside project')
        if holdout == out or out in holdout.parents or holdout in out.parents:
            raise ValueError('holdout and evidence must be separate')
        holdout_before = holdout_hashes(holdout)
        result['holdout']['files_sha256'] = holdout_before
        if a.timeout <= 0 or a.min_holdout_tests < 1 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', a.change_id):
            raise ValueError('invalid timeout or change id')
        if out == root or root in out.parents or out == ev or ev in out.parents:
            raise ValueError('evidence must be outside project and evalid')
        out.mkdir(parents=True, exist_ok=True)
        if '{holdout}' not in a.cmd_holdout:
            raise ValueError('holdout command must contain {holdout}')
        holdout_command = a.cmd_holdout
        if a.phase != 'red':
            archived_holdout = out / (a.change_id + '-holdout') / 'holdout-files'
            shutil.copytree(holdout, archived_holdout)
            if holdout_hashes(archived_holdout) != holdout_before:
                raise ValueError('holdout_changed')
            holdout_command = a.cmd_holdout.replace('{holdout}', shlex.quote(str(archived_holdout)))
        result['commit'] = git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
        result['evalid_commit'] = git(ev, 'rev-parse', '--verify', 'HEAD^{commit}')
        if git(ev, 'status', '--porcelain=v1', '--untracked-files=all'):
            raise ValueError('dirty evalid checkout')
        for atom in ('i6a_tdd_run.py', 'i6b_tdd_proof_validate.py'):
            if not (ev / 'xflow' / 'atoms' / atom).is_file():
                raise ValueError('evalid atom missing')
        status = git(root, 'status', '--porcelain=v1', '--untracked-files=all')
        prefixes = tuple(f'specs/changes/{a.change_id}-{label}/tdd/' for label in ('visible', 'holdout'))
        for line in status.splitlines():
            if not (a.phase == 'green' and any(line.startswith('?? ' + prefix) for prefix in prefixes)):
                raise ValueError('dirty project checkout')
        def check_copy():
            if archived_holdout is not None and holdout_hashes(archived_holdout) != holdout_before:
                raise ValueError('holdout_changed')

        def test(label, command):
            check_copy()
            log = out / (a.phase + '-' + label + '.log')
            result[label]['log_path'] = str(log)
            code = execute(['/bin/sh', '-c', command], root, a.timeout, log)
            result[label]['exit'] = code
            check_copy()
            if code is None or code in (126, 127) or code < 0:
                raise RuntimeError('test command failed to start or timed out')
            output = log.read_text(errors='replace')
            counts = [int(n) for n in re.findall(r'\bRan (\d+) tests?\b', output)]
            counts += [sum(int(n) for n in re.findall(r'(\d+) (?:passed|failed|xfailed|xpassed)\b', line))
                       for line in output.splitlines() if re.search(r'\d+ (?:passed|failed|xfailed|xpassed)\b', line)]
            count = max(counts, default=0)
            result[label]['test_count'] = count
            if count < (a.min_holdout_tests if label == 'holdout' else 1):
                raise RuntimeError('no_tests_detected')
            return code
        visible = test('visible', a.cmd_visible)
        if (a.phase == 'red' and visible == 0) or (a.phase != 'red' and visible != 0):
            result['verdict'] = 'FAIL'
            return result
        if a.phase != 'red' and test('holdout', holdout_command) != 0:
            result['verdict'] = 'FAIL'
            return result
        def evidence(label, action, phase=None):
            check_copy()
            change = a.change_id + '-' + label
            log = out / (a.phase + '-' + label + '-' + action + '.log')
            command = a.cmd_visible if label == 'visible' else holdout_command
            args = [sys.executable, str(EVIDENCE), action, '--project', str(root),
                    '--evalid', str(ev), '--evidence', str(out / change),
                    '--change-id', change, '--command', command]
            if phase:
                args += ['--phase', phase]
            code = execute(args, root, a.timeout, log)
            check_copy()
            if code is None:
                raise RuntimeError('evidence command timed out or could not start')
            if code:
                result['verdict'] = 'FAIL'
                result['error'] = 'evidence ' + label + ' ' + action + ' failed; see ' + log.name
                return False
            return True
        if not evidence('visible', 'capture', a.phase):
            return result
        if a.phase != 'red':
            if not evidence('holdout', 'capture', 'posthoc'):
                return result
            if not evidence('visible', 'verify') or not evidence('holdout', 'verify'):
                return result
            result['replay']['ok'] = True
        result['verdict'] = 'PASS'
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result['error'] = str(exc)
    finally:
        if archived_holdout is not None and holdout_before is not None:
            try:
                if holdout_hashes(archived_holdout) != holdout_before:
                    raise ValueError('holdout_changed')
            except (OSError, ValueError):
                result['verdict'] = 'INFRA'
                result['replay']['ok'] = False
                result['error'] = 'holdout_changed'
        if holdout_before is not None:
            try:
                if holdout_hashes(holdout) != holdout_before:
                    result['verdict'] = 'INFRA'
                    result['replay']['ok'] = False
                    result['error'] = 'holdout_changed'
            except (OSError, ValueError):
                result['verdict'] = 'INFRA'
                result['replay']['ok'] = False
                result['error'] = 'holdout_changed'
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
