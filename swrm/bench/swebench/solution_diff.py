"""Store and read a run's solution diff.

Diffs over the export cap (5 MB) are gzipped next to a sha256 of the original text, so every
counted run keeps its patch in the repository instead of being dropped from the export
(optimization backlog #4, P0-4).
"""
from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

EXPORT_CAP_BYTES = 5 * 1024 * 1024
DIFF_NAME = "solution.diff"
GZ_NAME = "solution.diff.gz"
SHA_NAME = "solution.diff.sha256"


def store_solution_diff(events: Path, diff: str) -> dict:
    """Write the diff of one run into its raw directory and describe how it was stored."""
    events = Path(events)
    raw = diff.encode("utf-8", errors="replace")
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) <= EXPORT_CAP_BYTES:
        (events / DIFF_NAME).write_bytes(raw)
        return {"file": DIFF_NAME, "bytes": len(raw), "sha256": digest, "compressed": False}
    blob = gzip.compress(raw, 9)
    (events / GZ_NAME).write_bytes(blob)
    (events / SHA_NAME).write_text(f"{digest}  {DIFF_NAME}\n")
    return {"file": GZ_NAME, "bytes": len(raw), "sha256": digest, "compressed": True,
            "stored_bytes": len(blob)}


def solution_diff_path(events: Path) -> Path | None:
    """The stored diff of one run: plain text, or the gzipped form when it exceeded the cap."""
    events = Path(events)
    plain = events / DIFF_NAME
    if plain.is_file():
        return plain
    gz = events / GZ_NAME
    return gz if gz.is_file() else None


def read_solution_diff(events: Path) -> str:
    """The diff of one run as text, from either storage form."""
    path = solution_diff_path(Path(events))
    if path is None:
        return ""
    if path.name == GZ_NAME:
        return gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
    return path.read_text(errors="replace")
