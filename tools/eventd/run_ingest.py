"""Follow swrm runs and repair missing terminal records in the aggregate log."""
import fcntl
import json
import os
import re
import selectors
import subprocess
import time
from pathlib import Path


def done_runs(log):
    if not log.exists():
        return set()
    found = set()
    active = {}
    for line in log.read_text(errors='replace').splitlines():
        match = re.match(r'^(?:\d\d:\d\d:\d\d\s+)?\[([^\]]+?)(?: \d\d:\d\d)?\]\s+(?:START\s+(\S+)|DONE:)', line)
        if match:
            label, run_id = match.groups()
            if run_id:
                active[label] = run_id
            else:
                found.add(active.get(label, label))
    return found


def append(log, line):
    with log.open('a', encoding='utf-8') as stream:
        stream.write(line.rstrip('\r\n') + '\n')
        stream.flush()


def recover(runs, log):
    """Reconcile terminal results against DONE lines, including on restarts."""
    done = done_runs(log)
    for run in sorted(runs.iterdir()):
        if not run.is_dir() or run.name in done:
            continue
        try:
            result = json.loads((run / 'result.json').read_text())
            if not isinstance(result, dict) or not isinstance(result.get('status'), str):
                continue
        except (OSError, ValueError):
            continue
        status = re.sub(r'\s+', '_', result['status'])
        append(log, f'[{run.name}] DONE: status={status} (recovered from result.json)')
        done.add(run.name)
    return done


def follow_runs(runs, log, command):
    runs = Path(runs)
    log.parent.mkdir(parents=True, exist_ok=True)
    # A single writer owns the log and its watcher set.
    with (log.parent / (log.name + '.ingest.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        selector = selectors.DefaultSelector()
        watchers = {}
        buffers = {}
        try:
            while True:
                done = recover(runs, log)
                for run in sorted(runs.iterdir()):
                    if not run.is_dir() or run.name in done or run.name in watchers:
                        continue
                    env = os.environ.copy()
                    env['OPENSWARM_HOME'] = str(runs.parent)
                    proc = subprocess.Popen([command, 'watch', run.name, '--label', run.name, '--max-minutes', '0'], stdout=subprocess.PIPE, env=env)
                    watchers[run.name] = proc
                    buffers[run.name] = b''
                    selector.register(proc.stdout, selectors.EVENT_READ, run.name)
                for key, _ in selector.select(timeout=1):
                    name = key.data
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        watchers.pop(name).wait()
                        tail = buffers.pop(name)
                        if tail:
                            text = tail.decode('utf-8', errors='replace')
                            if not (re.search(r'\]\s+DONE:', text) and name in done_runs(log)):
                                append(log, text)
                        continue
                    data = buffers[name] + chunk
                    lines = data.split(b'\n')
                    buffers[name] = lines.pop()
                    for line in lines:
                        text = line.decode('utf-8', errors='replace')
                        if re.search(r'\]\s+DONE:', text) and name in done_runs(log):
                            continue
                        append(log, text)
        finally:
            for proc in watchers.values():
                proc.terminate()
            for proc in watchers.values():
                proc.wait()
            selector.close()
