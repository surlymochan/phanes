#!/usr/bin/env python3
"""Replace IP literals with documentation addresses; --check fails on residuals."""

import argparse
import gzip
import ipaddress
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
IP_LITERAL = re.compile(
    rb"(?<![\w.:])(?:[0-9A-Fa-f]*:){2,}[0-9A-Fa-f:.]*"
    rb"|(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])"
)
DOCUMENTATION = tuple(map(ipaddress.ip_network, (
    '192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24', '2001:db8::/32',
)))
# These four-part numbers are version strings, not network endpoints.
VERSION_CONTEXTS = {
    'docs/benchmark-openflow-vs-gstack-superpowers-openspec-2026-05-25.md':
        re.compile(rb'package `([0-9.]+)`'),
    'src/agent_team/atoms/visual/page-evidence-capture.js':
        re.compile(rb'Chrome/([0-9.]+)'),
}


def redact(data, relative_path):
    version_pattern = VERSION_CONTEXTS.get(relative_path)
    versions = {match.span(1) for match in version_pattern.finditer(data)} if version_pattern else set()
    count = 0

    def replace(match):
        nonlocal count
        if match.span() in versions:
            return match.group()
        # Bare double colons are source-code separators, not endpoint literals.
        if match.group() == b'::':
            return match.group()
        try:
            address = ipaddress.ip_address(match.group().decode('ascii'))
        except ValueError:
            return match.group()
        if str(address) in {'127.0.0.1', '::1'} or any(address in network for network in DOCUMENTATION):
            return match.group()
        count += 1
        # Preserve host bits, including network alignment in CIDR fixtures.
        base = DOCUMENTATION[0] if address.version == 4 else DOCUMENTATION[3]
        host_bits = int(address) & int(base.hostmask)
        return str(ipaddress.ip_address(int(base.network_address) | host_bits)).encode('ascii')

    return IP_LITERAL.sub(replace, data), count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    files = replacements = scanned = 0
    for path in sorted(ROOT.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in {'.git', 'node_modules', '__pycache__'} for part in relative.parts):
            continue
        scanned += 1
        original = path.read_bytes()
        compressed = original.startswith(b'\x1f\x8b')
        data = gzip.decompress(original) if compressed else original
        try:
            data.decode('utf-8')
        except UnicodeDecodeError:
            # Binary image data can coincidentally match short IPv6 tokens.
            continue
        redacted, count = redact(data, relative.as_posix())
        if count:
            files += 1
            replacements += count
            print(f'{relative}: {count}')
            if not args.check:
                path.write_bytes(gzip.compress(redacted, mtime=0) if compressed else redacted)
    print(f"{'Found' if args.check else 'Redacted'} {replacements} IP literals in {files} files ({scanned} files scanned)")
    return 1 if args.check and replacements else 0


if __name__ == '__main__':
    raise SystemExit(main())
