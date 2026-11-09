#!/usr/bin/env python3
"""Scan public-facing Git objects and text without printing matched content."""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys


IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
IPV6 = re.compile(r'(?<![\w:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:]{0,4}(?![\w:])', re.I)
NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    '10.' + '0.0.0/8', '172.' + '16.0.0/12', '192.' + '168.0.0/16',
    '100.' + '64.0.0/10', '198.' + '18.0.0/15', 'fc' + '00::/7'))
PATH = re.compile(r'(?i)(?:/Us' r'ers/[^/\s]+|/ho' r'me/[^/\s]+|/ro' r'ot/|/private/' r'tmp/|/var/' r'folders/|[a-z]:\\Us' r'ers\\[^\\\s]+)')
EMAIL = re.compile(r'(?i)\b[\w.+%-]+@[\w.-]+\.[a-z]{2,}\b')
RULES = (
    ('domain', re.compile(r'(?i)(?<![\w.-])[\w-]+(?:\.[\w-]+)*\.(?:ts\.net|internal|lan|corp|home\.arpa)\b')),
    ('hold' + 'out', re.compile(r'(?i)(?<![\w])hold' r'out(?![\w])')),
    ('token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b')),
    ('key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----')),
)


def rules_from_config(args):
    if args.generic_only:
        return []
    if args.denylist:
        try:
            with open(args.denylist, encoding='utf-8') as stream:
                content = stream.read()
        except (OSError, UnicodeError):
            raise ValueError('invalid denylist')
    else:
        content = os.environ.get('PUBLIC_VERIFY_DENYLIST', '')
    rules = []
    for line in content.splitlines():
        if not line.strip():
            continue
        fields = line.split('\t', 1)
        if len(fields) != 2 or fields[0] not in ('host', 'domain', 'bus', 'account', 'hold' + 'out') or not fields[1]:
            raise ValueError('invalid denylist')
        term = fields[1]
        expression = term[3:] if term.startswith('re:') else re.escape(term)
        if not expression:
            raise ValueError('invalid denylist')
        try:
            pattern = re.compile(r'(?<!\w)(?:' + expression + r')(?!\w)', re.I)
        except re.error:
            raise ValueError('invalid denylist')
        if pattern.search(''):
            raise ValueError('invalid denylist')
        rules.append((fields[0], pattern))
    if not rules:
        raise ValueError('missing denylist')
    return rules


def scan(value, location, private_rules, findings):
    def add(category, match):
        digest = hashlib.sha256(match.encode('utf-8', 'surrogateescape')).hexdigest()[:8]
        findings.append({'category': category, 'location': location, 'hash': digest})

    for regex in (IPV4, IPV6):
        for match in regex.finditer(value):
            try:
                address = ipaddress.ip_address(match.group())
                if any(address.version == net.version and address in net for net in NETWORKS):
                    add('ip', match.group())
            except ValueError:
                pass
    for match in PATH.finditer(value):
        part = re.split(r'[/\\]', match.group().rstrip('/\\'))[-1].lower()
        if part not in ('runner', 'runneradmin', 'name', 'username', 'user', '<name>', '<user>', '<username>', '$user', '${user}', '~') and not (part.startswith('{') and part.endswith('}')):
            add('path', match.group())
    for match in EMAIL.finditer(value):
        local, domain = match.group().lower().rsplit('@', 1)
        if not re.search(r'(?:^|[+._-])no-?reply$', local) and 'noreply' not in domain.split('.') and not (domain in ('example.com', 'example.org', 'example.net') or domain.endswith(('.invalid', '.example', '.test', '.localhost'))):
            add('email', match.group())
    for category, pattern in RULES + tuple(private_rules):
        for match in pattern.finditer(value):
            add(category, match.group())


def git(*args):
    result = subprocess.run(['git', *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError('git operation failed')
    return result.stdout


def decode(raw):
    return raw.decode('utf-8', 'surrogateescape')


def tree(head, private, findings):
    process = subprocess.Popen(['git', 'ls-tree', '-rz', '--full-tree', head],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    # ls-tree records are NUL-delimited, so paths with newlines remain intact.
    pending = b''
    index = 0
    while True:
        chunk = process.stdout.read(65536)
        if not chunk:
            break
        pending += chunk
        records = pending.split(b'\0')
        pending = records.pop()
        for entry in records:
            index += 1
            meta, path = entry.split(b'\t', 1)
            mode, kind, oid = meta.split()
            scan(decode(path), 'tree:path:' + str(index), private, findings)
            if kind == b'blob':
                blob = subprocess.Popen(['git', 'cat-file', 'blob', oid.decode()],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                for line_no, line in enumerate(blob.stdout, 1):
                    scan(decode(line), 'tree:blob:' + oid.decode() + ':' + str(line_no), private, findings)
                if blob.wait():
                    raise ValueError('git operation failed')
    if process.wait() or pending:
        raise ValueError('git operation failed')


def diff(base, head, private, findings):
    fields = iter(git('diff', '--no-ext-diff', '--name-status', '-z', '--find-renames', '--find-copies', '--find-copies-harder', base, head, '--').split(b'\0'))
    for status in fields:
        if not status:
            break
        path = next(fields)
        if status[:1] in (b'C', b'R'):
            path = next(fields)
        if status[:1] in (b'A', b'C', b'T'):
            scan(decode(path), 'diff:path:' + hashlib.sha256(path).hexdigest()[:8], private, findings)
    patch = git('diff', '--no-ext-diff', '--find-renames', '--find-copies', '--find-copies-harder', '--text', '--unified=0', base, head, '--')
    file_no = line_no = 0
    in_hunk = False
    for line in decode(patch).splitlines():
        if line.startswith('diff --git '):
            file_no += 1
            in_hunk = False
        elif line.startswith('@@ '):
            match = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)', line)
            if match:
                line_no = int(match.group(1))
                in_hunk = True
        elif in_hunk and line.startswith('+'):
            scan(line[1:], 'diff:file:' + str(file_no) + ':' + str(line_no), private, findings)
            line_no += 1
        elif in_hunk and line.startswith(' '):
            line_no += 1


def commits(base, head, private, findings):
    base_type = git('cat-file', '-t', base).strip()
    revision = head if base_type == b'tree' else base + '..' + head
    data = git('log', '--format=%H%x00%B%x00', revision, '--')
    fields = data.split(b'\0')
    for i in range(0, len(fields) - 1, 2):
        oid = fields[i].strip().decode('ascii')
        for number, line in enumerate(decode(fields[i + 1]).splitlines(), 1):
            scan(line, 'commit:' + oid + ':' + str(number), private, findings)


class Findings:
    def __init__(self, output_format):
        self.format = output_format
        self.count = 0
        if output_format == 'json':
            print('[', end='')

    def append(self, item):
        if self.format == 'json':
            print((',' if self.count else '') + json.dumps(item, ensure_ascii=True), end='')
        else:
            print('{category} {location} {hash}'.format(**item))
        self.count += 1

    def finish(self):
        if self.format == 'json':
            print(']')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('diff', 'commits', 'text', 'history', 'tree'):
        p = sub.add_parser(name)
        p.add_argument('--generic-only', action='store_true')
        p.add_argument('--denylist')
        p.add_argument('--format', choices=('text', 'json'), default='text')
        if name in ('diff', 'commits'):
            p.add_argument('--base', required=True)
            p.add_argument('--head', required=True)
        if name == 'tree':
            p.add_argument('--head', default='HEAD')
        if name == 'text':
            p.add_argument('--label', required=True)
            p.add_argument('--file', default='-')
    args = parser.parse_args()
    try:
        private = rules_from_config(args)
        findings = Findings(args.format)
        if args.command == 'text':
            if args.file == '-':
                data = sys.stdin.buffer.read()
            else:
                with open(args.file, 'rb') as stream:
                    data = stream.read()
            label = hashlib.sha256(args.label.encode()).hexdigest()[:8]
            for number, line in enumerate(decode(data).splitlines(), 1):
                scan(line, 'text:' + label + ':' + str(number), private, findings)
        elif args.command == 'diff':
            diff(args.base, args.head, private, findings)
        elif args.command == 'commits':
            commits(args.base, args.head, private, findings)
        elif args.command == 'tree':
            tree(args.head, private, findings)
        else:
            process = subprocess.Popen(['git', 'rev-list', '--all'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            for raw in process.stdout:
                oid = raw.strip().decode('ascii')
                tree(oid, private, findings)
                message = git('show', '-s', '--format=%B', oid)
                for number, line in enumerate(decode(message).splitlines(), 1):
                    scan(line, 'commit:' + oid + ':' + str(number), private, findings)
            if process.wait():
                raise ValueError('git operation failed')
        findings.finish()
        return int(bool(findings.count))
    except (OSError, ValueError, UnicodeError, StopIteration):
        print('public-verify: configuration or input error', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
