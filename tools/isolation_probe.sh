#!/bin/sh
# Read-only isolation checks. Python is optional; without it no check is claimed.
if ! command -v python3 >/dev/null 2>&1; then
    for check in identifiers forbid-path proc unix-socket deny-host allow-host net-iface secrets; do
        printf '{"check":"%s","status":"SKIP","detail":"python3 unavailable"}\n' "$check"
    done
    printf '%s\n' '{"check":"summary","status":"SKIP","detail":"python3 unavailable"}'
    exit 2
fi
exec python3 - "$@" <<'PY'
import argparse
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--identifiers', required=True)
for name in ('forbid-path', 'allow-host', 'deny-host', 'search-root', 'allow-socket'):
    parser.add_argument('--' + name, action='append', default=[])
parser.add_argument('--no-network', action='store_true')
args = parser.parse_args()
roots = args.search_root or ['/home', '/opt', '/root', '/srv', '/tmp', '/etc', '/run', '/workspace']
results = []
summary_results = []
def emit(check, status, detail):
    results.append(status)
    if not (args.no_network and check in ('deny-host', 'allow-host', 'net-iface') and status == 'SKIP'):
        summary_results.append(status)
    print(json.dumps({'check': check, 'status': status, 'detail': detail}, ensure_ascii=True))

def excluded(path):
    path = os.path.abspath(path)
    return any(path == p or path.startswith(p + '/') for p in ('/proc', '/sys'))

def walk(roots):
    errors = []
    def error(exc):
        errors.append(exc)
    for root in roots:
        if excluded(root):
            continue
        if not os.path.lexists(root):
            errors.append(('missing search root: ' if root in args.search_root else 'skipped missing default root: ') + root)
            continue
        if not os.path.isdir(root) or os.path.islink(root):
            yield root, errors
            continue
        for base, dirs, files in os.walk(root, onerror=error, followlinks=False):
            dirs[:] = [d for d in dirs if not excluded(os.path.join(base, d))]
            yield base, errors
            for name in dirs + files:
                yield os.path.join(base, name), errors
    yield None, errors

try:
    with open(args.identifiers, 'rb') as f:
        identifiers = [line for line in f.read().splitlines() if line]
    if not identifiers:
        raise ValueError('empty identifier list')
    matches = 0
    errors = []
    for path, errors in walk(roots):
        if path is None:
            break
        if any(value in os.fsencode(path) for value in identifiers):
            matches += 1
            continue
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or os.path.islink(path):
                continue
            # Stream with overlap so identifiers spanning chunks are found.
            overlap = max(map(len, identifiers)) - 1
            tail = b''
            with open(path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    data = tail + chunk
                    if any(value in data for value in identifiers):
                        matches += 1
                        break
                    tail = data[-overlap:] if overlap else b''
        except OSError as exc:
            errors.append(exc)
    failures = [e for e in errors if not str(e).startswith('skipped missing default root: ')]
    emit('identifiers', 'FAIL' if matches or failures else 'PASS',
         'matches: %d; traversal details: %s' % (matches, '; '.join(map(str, errors)) or 'none'))
except (OSError, ValueError) as exc:
    emit('identifiers', 'SKIP', 'identifier input unavailable or empty')

if not args.forbid_path:
    emit('forbid-path', 'SKIP', 'no paths specified')
for path in args.forbid_path:
    try:
        if not os.path.lexists(path):
            status = 'PASS'
        elif os.path.isdir(path) and not os.path.islink(path):
            with os.scandir(path) as entries:
                status = 'FAIL' if next(entries, None) else 'PASS'
        else:
            status = 'FAIL'
        emit('forbid-path', status, 'path absent or empty directory' if status == 'PASS' else 'path exists and is not an empty directory')
    except OSError:
        emit('forbid-path', 'SKIP', 'path could not be inspected')

# NSpid with multiple levels proves PID 1 is nested; a single level is
# inconclusive because procfs can be mounted from within the namespace.
# A different PID 1 root proves it can see outside this process's root.
try:
    same_root = os.path.samefile('/proc/1/root', '/')
    with open('/proc/1/status') as f:
        nspid = next((line.split()[1:] for line in f if line.startswith('NSpid:')), [])
    if not same_root:
        emit('proc', 'FAIL', 'PID 1 root differs from sandbox root; outside root is visible')
    elif len(nspid) > 1:
        emit('proc', 'SKIP', 'nested PID namespace indicated; host root cannot be independently identified')
    else:
        emit('proc', 'SKIP', 'PID 1 root matches sandbox root; host root and PID namespace isolation cannot be independently established')
except OSError:
    emit('proc', 'SKIP', 'procfs evidence unavailable')

allowed = {os.path.abspath(p) for p in args.allow_socket}
connected = 0
errors = []
for path, errors in walk(['/run'] + roots):
    if path is None:
        break
    try:
        if os.path.abspath(path) in allowed or not stat.S_ISSOCK(os.lstat(path).st_mode):
            continue
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            s = socket.socket(socket.AF_UNIX, kind)
            s.settimeout(0.5)
            try:
                s.connect(path)
                connected += 1
                break
            except OSError:
                pass
            finally:
                s.close()
    except OSError as exc:
        errors.append(exc)
failures = [e for e in errors if not str(e).startswith('skipped missing default root: ')]
emit('unix-socket', 'FAIL' if connected or failures else 'PASS',
     'connectable sockets: %d; traversal details: %s' % (connected, '; '.join(map(str, errors)) or 'none'))

def host_check(host, proxy, deny=False):
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or not host.strip('.'):
        return None
    env = os.environ.copy()
    if proxy:
        proxy_url = env.get('https_proxy') or env.get('HTTPS_PROXY') or env.get('all_proxy') or env.get('ALL_PROXY')
        if not proxy_url:
            return None
        options = ['--proxy', proxy_url, '--noproxy', '']
    else:
        options = ['--noproxy', '*']
    try:
        result = subprocess.run(['curl', '--silent', '--show-error', '--output', os.devnull,
                                 '--connect-timeout', '5', '--max-time', '10', '--insecure',
                                 '--write-out', '%{http_code} %{http_connect}'] + options +
                                ['--', 'https://' + host + '/'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
        parts = result.stdout.split()
        code, connect = (int(parts[-2]), int(parts[-1])) if len(parts) >= 2 else (0, 0)
        if deny:
            if result.returncode == 28:
                return None
            if connect == 403 or result.returncode in (5, 6, 7):
                return False
            if result.returncode != 0:
                return None
            return True if code > 0 else None
        if result.returncode != 0 or (proxy and connect != 200):
            return False
        return code > 0
    except (OSError, ValueError):
        return None

for check, hosts in (('deny-host', args.deny_host), ('allow-host', args.allow_host)):
    if args.no_network or not hosts or not shutil.which('curl'):
        emit(check, 'SKIP', 'network disabled, no hosts specified, or curl unavailable')
        continue
    for host in hosts:
        if check == 'deny-host':
            direct, proxied = host_check(host, False, True), host_check(host, True, True)
            proxy_configured = any(os.environ.get(k) for k in ('https_proxy', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'))
            outcomes = [direct] + ([proxied] if proxy_configured else [])
            status = 'PASS' if all(value is False for value in outcomes) else 'FAIL'
        else:
            proxied = host_check(host, True)
            status = 'SKIP' if proxied is None else 'PASS' if proxied else 'FAIL'
        emit(check, status, 'connectivity tested; host omitted')

if args.no_network:
    emit('net-iface', 'SKIP', 'network disabled')
else:
    try:
        names = [name for _, name in socket.if_nameindex() if name != 'lo']
        emit('net-iface', 'FAIL' if names else 'PASS', 'non-loopback interfaces: %d' % len(names))
    except OSError:
        emit('net-iface', 'SKIP', 'interface enumeration unavailable')

home = os.environ.get('HOME')
secret_names = sorted(k for k in os.environ if re.search('KEY|TOKEN|SECRET', k, re.I))
if not home:
    emit('secrets', 'SKIP', 'HOME unavailable; environment names: ' + ', '.join(secret_names))
else:
    present = [name for name in ('.ssh', '.aws') if os.path.lexists(os.path.join(home, name))]
    emit('secrets', 'FAIL' if present else 'PASS', 'present: %s; environment names: %s' % (', '.join(present) or 'none', ', '.join(secret_names) or 'none'))

status = 'FAIL' if 'FAIL' in summary_results else 'SKIP' if 'SKIP' in summary_results else 'PASS'
print(json.dumps({'check': 'summary', 'status': status, 'detail': 'PASS=%d FAIL=%d SKIP=%d' % tuple(results.count(s) for s in ('PASS', 'FAIL', 'SKIP'))}))
sys.exit(1 if status == 'FAIL' else 2 if status == 'SKIP' else 0)
PY
