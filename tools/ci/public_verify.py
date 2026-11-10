#!/usr/bin/env python3
"""Scan public-facing Git objects and text without printing matched content."""
import argparse
import hashlib
import ipaddress
import json
import re
import subprocess
import sys

if __package__:
    from .public_verify_denylist import DenylistError, load
else:
    from public_verify_denylist import DenylistError, load


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
    matcher = load(denylist_path=args.denylist, generic_only=args.generic_only)
    return [(rule.category, rule.pattern) for rule in matcher.rules]


def scan(value, location, private_rules, findings, categories=None):
    def add(category, match):
        if categories is not None and category not in categories:
            return
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
            if match.group():
                add(category, match.group())


def git(*args):
    result = subprocess.run(['git', *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError('git operation failed')
    return result.stdout


def decode(raw):
    return raw.decode('utf-8', 'surrogateescape')


def tree(head, private, findings, seen_blobs=None, seen_paths=None):
    process = subprocess.Popen(['git', 'ls-tree', '-rz', '--full-tree', head],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    # ls-tree records are NUL-delimited, so paths with newlines remain intact.
    pending = b''
    index = 0
    label = hashlib.sha256(head.encode('utf-8', 'surrogateescape')).hexdigest()[:8]
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
            if seen_paths is None or path not in seen_paths:
                scan(decode(path), 'tree:path:' + label + ':' + str(index), private, findings)
                if seen_paths is not None:
                    seen_paths.add(path)
            if kind == b'blob' and (seen_blobs is None or oid not in seen_blobs):
                if seen_blobs is not None:
                    seen_blobs.add(oid)
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
    base_type = git('cat-file', '-t', base).strip() if base is not None else None
    revision = '--all' if base is None else (head if base_type == b'tree' else base + '..' + head)
    process = subprocess.Popen(
        ['git', 'log', '--format=%H%x00%B%x00%an%x00%ae%x00%cn%x00%ce%x00', revision, '--'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pending = b''
    fields = []
    with process.stdout:
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            records = (pending + chunk).split(b'\0')
            pending = records.pop()
            for record in records:
                fields.append(record)
                if len(fields) != 6:
                    continue
                oid = fields[0].strip().decode('ascii')
                for number, line in enumerate(decode(fields[1]).splitlines(), 1):
                    scan(line, 'commit:' + oid + ':' + str(number), private, findings)
                for field, value in zip(('author-name', 'author-email', 'committer-name', 'committer-email'), fields[2:]):
                    scan(decode(value), 'commit:' + oid + ':' + field, private, findings,
                         categories=('email', 'account'))
                fields = []
    if process.wait() or fields or pending.strip():
        raise ValueError('git operation failed')


def history(private, findings):
    # Retain only object IDs and paths: shared blob contents are scanned once,
    # while a clean blob introduced under a sensitive path is still checked.
    seen_blobs, seen_paths = set(), set()
    process = subprocess.Popen(['git', 'rev-list', '--all'], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    with process.stdout:
        for raw in process.stdout:
            tree(raw.strip().decode('ascii'), private, findings, seen_blobs, seen_paths)
    if process.wait():
        raise ValueError('git operation failed')
    commits(None, None, private, findings)


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
            history(private, findings)
        findings.finish()
        return int(bool(findings.count))
    except (DenylistError, OSError, ValueError, UnicodeError, StopIteration):
        print('public-verify: configuration or input error', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
