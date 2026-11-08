"""Regression tests for literal paths in the benchmark diff inventory."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from run import diff_against


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def test_diff_inventory_preserves_literal_filenames(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "bench-e2e@example.test")
    git(repo, "config", "user.name", "bench-e2e")

    tracked = repo / "tracked file.txt"
    tracked.write_text("before\n", encoding="utf-8")
    git(repo, "add", "--", tracked.name)
    git(repo, "commit", "-qm", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    tracked.write_text("after\n", encoding="utf-8")

    untracked_names = [
        "space name.txt",
        'quote"name.txt',
        "back\\slash.txt",
        "非ASCII.txt",
    ]
    for name in untracked_names:
        (repo / name).write_text("new\n", encoding="utf-8")

    files, _ = diff_against(repo, baseline)

    expected = {tracked.name, *untracked_names}
    assert set(files) == expected
    assert len(files) == len(expected)
