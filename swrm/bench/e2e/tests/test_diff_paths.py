"""Regression tests for literal paths in the benchmark diff inventory."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_diff_inventory_preserves_controls_and_deduplicates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "bench-e2e@example.test")
    git(repo, "config", "user.name", "bench-e2e")
    names = [" leading trailing ", "tab\tname", "line\nname", "carriage\rname"]
    for name in names:
        (repo / name).write_text("before\n")
    git(repo, "add", "--all")
    git(repo, "commit", "-qm", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    for name in names:
        (repo / name).write_text("after\n")
    # A cached deletion whose file remains is listed by both diff and ls-files.
    git(repo, "rm", "--cached", "--", names[0])
    nested = "new dir/nested\nfile"
    (repo / "new dir").mkdir()
    (repo / nested).write_text("new\n")
    (repo / ".gitignore").write_text("ignored\n")
    (repo / "ignored").write_text("ignored\n")
    files, _ = diff_against(repo, baseline)
    assert set(files) == {*names, nested, ".gitignore"}
    assert len(files) == len(set(files))


def test_diff_inventory_round_trips_non_utf8_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "-c", "user.email=bench@example.test", "-c", "user.name=bench",
        "commit", "--allow-empty", "-qm", "baseline")
    raw_name = b"invalid-\xff-name"
    try:
        with open(os.fsencode(repo) + b"/" + raw_name, "wb") as handle:
            handle.write(b"untracked")
    except OSError as exc:
        if exc.errno != errno.EILSEQ:
            raise
        pytest.skip("filesystem does not support non-UTF-8 filenames")
    files, _ = diff_against(repo, "HEAD")
    assert [os.fsencode(name) for name in files] == [raw_name]
