#!/usr/bin/env python3
"""Tester shadow verdict on one reviewed pull request head (Refs #1789, Epic #1785).

The merge gate of Epic #1785 may only switch from the lane review to the tester
verdict once both sides have been measured side by side for a while.  This tool
is the shadow side of that measurement: it takes a real reviewed head, runs the
machine checks that can decide a head on their own, and writes down one
verdict - PASS, FAIL or INSUFFICIENT - next to the evidence that produced it.

The checks are deliberately narrow, because a verdict that cannot be explained
is worse than no verdict at all:

  diff hygiene   the diff must not carry binary or build artifacts (#1729)
  python syntax  every changed Python file must compile
  unit tests     the tests that cover the changed modules must be green
  cli smoke      every changed entry point must still start (``--help``)

A head that cannot be inspected (its object is not in the local object store)
and a diff that no check applies to are both INSUFFICIENT: the evidence is
missing, that is not a failure.  The verdict never replaces the lane review,
it only sits next to it in the divergence log.

Usage:
  review-shadow-tester.py --pr 1751 --head SHA [--base SHA] [--json-out FILE]
  review-shadow-tester.py --self-test

Exit code 0 means PASS, 1 means FAIL, 3 means INSUFFICIENT and 2 means the
input was rejected.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone


SCHEMA = "phanes.review-shadow-tester/1"
VERDICTS = ("PASS", "FAIL", "INSUFFICIENT")

# Build artifacts and binaries never belong in a pull request diff (#1729).
BINARY_SUFFIXES = (".so", ".o", ".a", ".dylib", ".dll", ".exe", ".class", ".jar",
                   ".wasm", ".whl", ".pyc", ".pyo")
# The trees whose Python files this tester can decide on its own.
SOURCE_PREFIXES = ("openswarm/lib/", "openswarm/bin/", "openswarm/sdk/", "tools/")
TEST_PREFIXES = ("openswarm/tests/", "tools/tests/")
DEFAULT_ARCHIVE_PATHS = ("openswarm", "tools")

HEAD_RE = re.compile(r"^[0-9a-f]{7,40}$")
ENTRY_POINT_RE = re.compile(r"^if\s+__name__\s*==\s*[\"']__main__[\"']\s*:", re.M)


def now_iso() -> str:
    """Return the current instant as an ISO-8601 UTC stamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command inside the repository and return the result."""
    return subprocess.run(["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)


def git_text(repo: Path, *args: str) -> str:
    """Return the stdout of one git command; an empty string when git failed."""
    result = run_git(repo, *args)
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else ""


def has_object(repo: Path, sha: str) -> bool:
    """Return whether the commit object of the sha is in the local object store."""
    return run_git(repo, "cat-file", "-e", "%s^{commit}" % sha).returncode == 0


def first_parent(repo: Path, sha: str) -> str | None:
    """Return the first parent of a commit, or None when it has none."""
    text = git_text(repo, "rev-parse", "--verify", "%s^" % sha).strip()
    return text or None


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    """Return the files the diff adds, changes or renames; deletions are dropped."""
    text = git_text(repo, "diff", "--name-only", "--diff-filter=ACMR", "%s..%s" % (base, head))
    return [line.strip() for line in text.splitlines() if line.strip()]


def blob_text(repo: Path, head: str, path: str) -> str:
    """Return the content of one file at the head."""
    result = run_git(repo, "show", "%s:%s" % (head, path))
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", "replace")


def materialize(repo: Path, head: str, dest: Path, paths: list[str]) -> None:
    """Write the tree of the head into dest so the checks can run on it."""
    wanted = [path for path in paths
              if git_text(repo, "ls-tree", "--name-only", head, path).strip()]
    if not wanted:
        return
    result = run_git(repo, "archive", "--format=tar", head, "--", *wanted)
    if result.returncode != 0:
        return
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        try:
            archive.extractall(dest, filter="data")
        except TypeError:  # Python without the extraction filter
            archive.extractall(dest)


def test_files(tree: Path) -> list[str]:
    """Return the test files of the tree, relative to it."""
    found = []
    for prefix in TEST_PREFIXES:
        root = tree / prefix
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("test_*.py")):
            found.append(str(path.relative_to(tree)))
    return found


def mentions(text: str, source: str) -> bool:
    """Return whether a test file talks about the changed source file.

    A test covers a source file when it names the file, names the module the
    file provides, or imports the module under another path.
    """
    name = source.rsplit("/", 1)[-1]
    stem = name[:-3] if name.endswith(".py") else name
    if source in text or name in text:
        return True
    return bool(stem) and re.search(r"\b%s\b" % re.escape(stem), text) is not None


def compile_source(text: str, path: str) -> tuple[bool, str]:
    """Return whether one Python source compiles, with the error message."""
    try:
        compile(text, path, "exec")
    except SyntaxError as exc:
        return False, "%s: %s" % (path, exc)
    except ValueError as exc:  # source with null bytes
        return False, "%s: %s" % (path, exc)
    return True, ""


def run_tests(tree: Path, test_path: str, timeout: int) -> tuple[bool, str]:
    """Run one test file in the tree and return whether it is green."""
    directory = str(Path(test_path).parent)
    pattern = Path(test_path).name
    try:
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", directory,
                                 "-p", pattern],
                                cwd=str(tree), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "%s timed out after %ds" % (test_path, timeout)
    output = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode == 0:
        return True, "%s passed" % test_path
    tail = " | ".join(line for line in output.splitlines()[-4:] if line.strip())
    return False, "%s failed: %s" % (test_path, tail)


def run_cli_smoke(tree: Path, path: str, timeout: int) -> tuple[bool, str]:
    """Run the entry point of a script with --help and return whether it starts."""
    script = tree / path
    if not script.is_file():
        return False, "%s is missing in the tree" % path
    try:
        result = subprocess.run([sys.executable, str(script), "--help"], cwd=str(tree),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "%s timed out after %ds" % (path, timeout)
    if result.returncode == 0:
        return True, "%s starts" % path
    tail = result.stdout.decode("utf-8", "replace").strip().splitlines()
    return False, "%s exits %d: %s" % (path, result.returncode,
                                       tail[-1] if tail else "no output")


def is_source(path: str) -> bool:
    """Return whether a path is a source file this tester can decide on."""
    return path.startswith(SOURCE_PREFIXES) and path.endswith(".py")


def is_test(path: str) -> bool:
    """Return whether a path is a test file of the tree."""
    return path.startswith(TEST_PREFIXES) and path.endswith(".py")


def decide(checks: list[dict], uncovered: list[str]) -> tuple[str, str]:
    """Return the verdict and the reason of one shadow run.

    A check that fails vetoes the head.  A check that only looked at the diff
    (``diff-hygiene``) is a gate and never evidence on its own: a diff without
    a source file has nothing a machine can decide, so the verdict stays
    INSUFFICIENT instead of pretending the diff is good.
    """
    failed = [check for check in checks if check["status"] == "fail"]
    if failed:
        return "FAIL", "; ".join(check["detail"] for check in failed)
    if uncovered:
        return "INSUFFICIENT", ("no machine check covers %s" % ", ".join(sorted(uncovered)))
    green = [check for check in checks
             if check.get("role") == "evidence" and check["status"] == "pass"
             and check.get("subjects")]
    if not green:
        return "INSUFFICIENT", "no machine check applies to this diff"
    return "PASS", "%d check(s) green" % len(green)


def shadow_verdict(repo: Path, pr: int, head: str, base: str | None, archive_paths: list[str],
                   timeout: int) -> dict:
    """Run every machine check on the head and return the shadow verdict."""
    report = {"schema": SCHEMA, "pr": pr, "head": head, "base": base, "checks": [],
              "changed_files": [], "uncovered": [], "recorded_at": now_iso()}
    if not has_object(repo, head):
        report["verdict"] = "INSUFFICIENT"
        report["reason"] = ("the head object is not in the local object store, so the shadow "
                            "run cannot inspect the reviewed head")
        return report
    if base is None:
        base = first_parent(repo, head)
    if base is None:
        report["verdict"] = "INSUFFICIENT"
        report["reason"] = "the head has no parent commit, so the diff cannot be taken"
        return report
    report["base"] = base
    changed = changed_files(repo, base, head)
    report["changed_files"] = changed
    checks = report["checks"]

    artifacts = [path for path in changed if path.lower().endswith(BINARY_SUFFIXES)]
    checks.append({"name": "diff-hygiene", "role": "gate", "subjects": len(changed),
                   "status": "fail" if artifacts else "pass",
                   "detail": "binary or build artifacts in the diff: %s" % ", ".join(artifacts)
                   if artifacts else "no binary or build artifact in the diff"})

    sources = [path for path in changed if is_source(path)]
    broken = []
    for path in sources:
        ok, detail = compile_source(blob_text(repo, head, path), path)
        if not ok:
            broken.append(detail)
    checks.append({"name": "python-syntax", "role": "evidence", "subjects": len(sources),
                   "status": "fail" if broken else ("pass" if sources else "skipped"),
                   "detail": "; ".join(broken) if broken else
                   ("%d changed Python file(s) compile" % len(sources) if sources
                    else "no changed Python file to compile")})

    with tempfile.TemporaryDirectory(prefix="review-shadow-tester-") as workdir:
        tree = Path(workdir)
        materialize(repo, head, tree, archive_paths)
        candidates = []
        for path in test_files(tree):
            text = (tree / path).read_text(encoding="utf-8", errors="replace")
            if any(mentions(text, source) for source in sources):
                candidates.append(path)
        green = []
        red = []
        for path in candidates:
            ok, detail = run_tests(tree, path, timeout)
            (green if ok else red).append((path, detail))
        checks.append({"name": "unit-tests", "role": "evidence", "subjects": len(candidates),
                       "status": "fail" if red else ("pass" if green else "skipped"),
                       "detail": "; ".join(detail for _, detail in red) if red else
                       ("%d test file(s) green: %s"
                        % (len(green), ", ".join(path for path, _ in green)) if green
                        else "no test file covers the changed modules")})

        entry_points = [path for path in sources
                        if ENTRY_POINT_RE.search(blob_text(repo, head, path))
                        and "argparse" in blob_text(repo, head, path)]
        started = []
        broken_entries = []
        for path in entry_points:
            ok, detail = run_cli_smoke(tree, path, timeout)
            (started if ok else broken_entries).append((path, detail))
        checks.append({"name": "cli-smoke", "role": "evidence", "subjects": len(entry_points),
                       "status": "fail" if broken_entries else ("pass" if started else "skipped"),
                       "detail": "; ".join(detail for _, detail in broken_entries) if broken_entries else
                       ("%d entry point(s) start: %s"
                        % (len(started), ", ".join(detail for _, detail in started)) if started
                        else "no changed entry point to start")})
        smoke_ok = {path for path, _ in started}

        # A source file is covered when a green test file talks about it or when
        # its entry point started.
        covered_sources = set()
        for source in sources:
            if source in smoke_ok:
                covered_sources.add(source)
                continue
            for path, _ in green:
                text = (tree / path).read_text(encoding="utf-8", errors="replace")
                if mentions(text, source):
                    covered_sources.add(source)
                    break
    report["uncovered"] = sorted(set(sources) - covered_sources)
    verdict, reason = decide(checks, report["uncovered"])
    report["verdict"] = verdict
    report["reason"] = reason
    return report


def self_test() -> None:
    """Run a tiny built-in check of the rules the verdict depends on."""
    assert is_source("openswarm/lib/dispatch/fleet_status.py")
    assert not is_source("docs/fleet-status-page.md")
    assert is_test("openswarm/tests/test_fleet_status.py")
    assert mentions("import openswarm.lib.dispatch.fleet_status", "openswarm/lib/dispatch/fleet_status.py")
    assert mentions("tools/fleet-status-page.py", "tools/fleet-status-page.py")
    assert not mentions("nothing here", "openswarm/lib/dispatch/fleet_status.py")
    assert not mentions("engineering notes", "openswarm/lib/dispatch/engine.py")
    assert compile_source("x = 1\n", "x.py") == (True, "")
    assert compile_source("def (\n", "x.py")[0] is False
    assert decide([{"name": "a", "role": "evidence", "subjects": 1, "status": "pass", "detail": ""}],
                  []) == ("PASS", "1 check(s) green")
    assert decide([{"name": "a", "role": "gate", "subjects": 1, "status": "pass", "detail": ""}],
                  [])[0] == "INSUFFICIENT"
    assert decide([{"name": "a", "role": "evidence", "subjects": 1, "status": "fail", "detail": "boom"}],
                  [])[0] == "FAIL"
    assert decide([], ["x.py"])[0] == "INSUFFICIENT"
    assert decide([{"name": "a", "status": "skipped", "detail": ""}], [])[0] == "INSUFFICIENT"
    print("OK: review shadow tester self-test")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tester shadow verdict on a reviewed head.")
    parser.add_argument("--pr", type=int, help="pull request number of the reviewed head")
    parser.add_argument("--head", help="reviewed head sha")
    parser.add_argument("--base", help="base commit of the diff (default: the first parent)")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent),
                        help="repository the head lives in")
    parser.add_argument("--archive-path", action="append", dest="archive_paths",
                        help="tree to materialize for the checks (default: openswarm tools)")
    parser.add_argument("--timeout", type=int, default=300, help="seconds per check")
    parser.add_argument("--json-out", help="write the report to this file as well")
    parser.add_argument("--self-test", action="store_true", help="run the built-in check and exit")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if not args.head:
        parser.error("--head is required")
    head = str(args.head).strip().lower()
    if not HEAD_RE.match(head):
        print("review-shadow-tester: head must be 7 to 40 hex characters, got %r" % args.head,
              file=sys.stderr)
        return 2
    pr = args.pr if args.pr is not None else 0
    if pr < 0:
        print("review-shadow-tester: pr must not be negative", file=sys.stderr)
        return 2
    base = str(args.base).strip().lower() if args.base else None
    if base and not HEAD_RE.match(base):
        print("review-shadow-tester: base must be 7 to 40 hex characters, got %r" % args.base,
              file=sys.stderr)
        return 2
    repo = Path(args.repo).resolve()
    archive_paths = args.archive_paths or list(DEFAULT_ARCHIVE_PATHS)
    report = shadow_verdict(repo, pr, head, base, archive_paths, args.timeout)
    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2))
    print("review-shadow-tester: verdict %s (%s)" % (report["verdict"], report["reason"]))
    print("append with: python3 tools/review-divergence-log.py append --pr %d --head %s "
          "--lane <LANE_VERDICT> --tester %s --evidence %s"
          % (pr, head, report["verdict"], args.json_out or "<report file>"))
    return {"PASS": 0, "FAIL": 1, "INSUFFICIENT": 3}[report["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
