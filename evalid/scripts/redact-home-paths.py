#!/usr/bin/env python3
"""Redact user home paths in the imported tree; --check fails on residuals."""

import argparse
import gzip
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
# Work on bytes to preserve encoding, newlines, and unrelated content.
HOME_PATH = re.compile(
    rb"/(?:Users|home)/[^/\s\x00\"'<>`\\:;,\)\]\}]+(?:/)?"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = replacements = 0
    for path in sorted(ROOT.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if any(part in {".git", "node_modules", "__pycache__"} for part in path.relative_to(ROOT).parts):
            continue
        original = path.read_bytes()
        compressed = original.startswith(b"\x1f\x8b")
        data = gzip.decompress(original) if compressed else original
        redacted, count = HOME_PATH.subn(b"~/", data)
        if count:
            files += 1
            replacements += count
            if args.check:
                print(path.relative_to(ROOT))
            else:
                path.write_bytes(gzip.compress(redacted, mtime=0) if compressed else redacted)
    print(f"{'Found' if args.check else 'Redacted'} {replacements} home paths in {files} files")
    return 1 if args.check and replacements else 0


if __name__ == "__main__":
    raise SystemExit(main())
