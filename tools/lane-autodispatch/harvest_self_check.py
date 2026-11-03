"""Pre-PR machine self-check of a harvested lane diff (Refs #1729).

`inspect()` is called by the harvest path (`tools/lane-autodispatch/autodispatch.py`)
before the lane branch is pushed.  Three gates are evaluated on the diff between the
lane base and the harvested head:

* binary files, build artefacts, dependency directories and archives are rejected —
  the harvest is sent back and the file list is published to the harvest log, the
  issue and the applicable PR;
* a diff that changes code but no test file is flagged `no-tests`;
* a control-plane change (dispatch / autodispatch / capacity / pause) whose *changed*
  test content adds no failure-path test is flagged `missing-failure-tests`.

Legitimate binary resources (test fixture images for instance) are exempted through
the allowlist file `.harvest-self-check-allow` in the repository root: one glob per
line, `#` starts a comment.  The diff is computed against the merge base of base and
head so that later base commits do not pollute the judgement, and no diff output is
truncated: an unreadable diff raises, and the caller then fails open.
"""
import fnmatch
import os
import re
import subprocess

# Build artefacts and archives: they must never enter a PR diff.
BINARY_SUFFIXES = (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".whl", ".jar", ".war",
                   ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".class", ".pyc", ".pyo", ".wasm")
# Dependency / build directories: their content is generated, never reviewed.
DEPENDENCY_DIRS = ("node_modules", "dist", "build", "vendor", ".venv", "venv", "__pycache__", "site-packages",
                   "target", ".next", ".tox", ".gradle", ".pytest_cache", ".mypy_cache", ".cache")
# Control-plane paths: they need failure-path tests.
CONTROL_PLANE_MARKERS = ("dispatch", "autodispatch", "capacity", "pause")
# Source files: "new logic" that needs a matching test change.
CODE_SUFFIXES = (".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
                 ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php", ".cs", ".sh", ".bash", ".zsh",
                 ".sql", ".lua", ".tf", ".proto", ".gradle")
# Documentation is not "new logic" and must not trigger the no-tests gate.
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
# Directories whose files are tests.
TEST_DIR_NAMES = ("test", "tests", "testing", "__tests__", "spec", "specs")
# A failure-path assertion in the *changed* test content.
FAILURE_PATH_RE = re.compile(
    r"pytest\.raises|assertRaises|assert_raises|assertThrows|assert_throws|assertRaisesRegex|"
    r"expect\s*\([^()]*\)\s*\.toThrow|toThrowError|should_fail|shouldFail|"
    r"expect_error|expectError|assert_error|raises_error|raisesError",
    re.I)
ALLOWLIST_FILE = ".harvest-self-check-allow"
MAX_DIFF_BYTES = 4 * 1024 * 1024  # a bigger test diff is not judged (fail open on the flag)


def _git_bytes(repo, *args):
    """Run git in `repo` and return stdout as bytes (raises when git fails)."""
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True).stdout


def _git_text(repo, *args):
    """Run git in `repo` and return stdout as text (raises when git fails)."""
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          errors="replace", check=True).stdout


def parse_numstat(raw):
    """Parse `git diff --numstat -z` output into change records.

    With `-z` a rename or copy record is `added\\tdeleted\\t` followed by the old and
    the new path as two extra NUL separated fields — the new path is the one that
    matters for the gates, the old path is kept for the report.
    """
    fields = raw.split(b"\0")
    records = []
    index = 0
    while index < len(fields):
        head = fields[index]
        index += 1
        if not head:
            continue
        parts = head.split(b"\t", 2)
        added = parts[0]
        deleted = parts[1] if len(parts) > 1 else b""
        path = parts[2] if len(parts) > 2 else b""
        old = b""
        if not path:  # rename / copy: the old and the new path follow as their own fields
            old = fields[index] if index < len(fields) else b""
            index += 1
            path = fields[index] if index < len(fields) else b""
            index += 1
        records.append({
            "added": added.decode("utf-8", "replace"),
            "deleted": deleted.decode("utf-8", "replace"),
            "path": path.decode("utf-8", "replace"),
            "old_path": old.decode("utf-8", "replace"),
            "binary": added == b"-" and deleted == b"-",
        })
    return records


def diff_base(repo, base, head):
    """Merge base of `base` and `head`, so later base commits stay out of the diff.

    The dispatch-time base commit may not be in the mirror yet, so `main` and `HEAD`
    are used as fallbacks; the merge base keeps unrelated base commits out of the
    judgement in every case.
    """
    for candidate in (base, "main", "HEAD"):
        try:
            merge_base = _git_text(repo, "merge-base", candidate, head).strip()
        except (subprocess.CalledProcessError, OSError):
            continue
        if merge_base:
            return merge_base
    return base


def is_test_path(path):
    """True for test files (`test_*.py`, `*_test.go`, `tests/…`)."""
    parts = path.split("/")
    if any(part.lower() in TEST_DIR_NAMES for part in parts[:-1]):
        return True
    stem = parts[-1].split(".")[0].lower()
    return stem.startswith("test_") or stem.startswith("test-") or stem.endswith("_test") or stem.endswith("-test")


def is_code_path(path):
    """True for source files — documentation and assets are not "new logic"."""
    low = path.lower()
    if low.endswith(DOC_SUFFIXES):
        return False
    if low.endswith(CODE_SUFFIXES):
        return True
    return not os.path.splitext(path)[1] and low.startswith(("bin/", "tools/", "scripts/"))


def is_control_plane(path):
    """True for dispatch / autodispatch / capacity / pause paths."""
    low = path.lower()
    return any(marker in low for marker in CONTROL_PLANE_MARKERS)


def artifact_reason(path, binary=False):
    """Why the path is a rejected artefact ("" when the path is fine)."""
    if binary:
        return "binary"
    low = path.lower()
    if any(part in DEPENDENCY_DIRS for part in low.split("/")[:-1]):
        return "dependency-dir"
    if low.endswith(BINARY_SUFFIXES):
        return "build-artefact"
    return ""


def load_allowlist(repo):
    """Glob patterns of legitimate binary resources (repo allowlist file + env override)."""
    patterns = []
    try:
        with open(os.path.join(repo, ALLOWLIST_FILE)) as stream:
            for line in stream:
                line = line.split("#", 1)[0].strip()
                if line:
                    patterns.append(line)
    except OSError:
        pass
    for chunk in os.environ.get("AD_SELF_CHECK_ALLOW", "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            patterns.append(chunk)
    return patterns


def is_allowed(path, patterns):
    """True when the path matches an allowlist glob."""
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns)


def added_lines(repo, base, head, paths):
    """Added lines of the given paths without context, or None when the diff is too big."""
    if not paths:
        return []
    args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--unified=0", base, head, "--"]
    args.extend(f":(literal){path}" for path in paths)
    diff = _git_text(repo, *args)
    if len(diff) > MAX_DIFF_BYTES:
        return None
    return [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


def has_failure_path(lines):
    """True when the added lines contain a failure-path assertion."""
    return any(FAILURE_PATH_RE.search(line) for line in lines)


def inspect(repo, base, head="HEAD"):
    """Evaluate the harvest gates on the diff between `base` and `head`.

    Deletions are ignored: removing a binary or a source file never enters a PR diff
    and must not be judged.
    """
    merge_base = diff_base(repo, base, head)
    records = parse_numstat(_git_bytes(repo, "diff", "--numstat", "-z", "--diff-filter=d", merge_base, head))
    paths = [record["path"] for record in records if record["path"]]
    allow = load_allowlist(repo)
    rejected = []
    reasons = {}
    for record in records:
        if not record["path"]:
            continue
        reason = artifact_reason(record["path"], record["binary"])
        if reason and not is_allowed(record["path"], allow):
            rejected.append(record["path"])
            reasons[record["path"]] = reason
    changed = [record for record in records if record["path"] and record["added"] != "0"]
    test_paths = [record["path"] for record in changed if is_test_path(record["path"])]
    code_paths = [record["path"] for record in changed if is_code_path(record["path"])]
    control = any(is_control_plane(path) for path in code_paths)
    warnings = []
    lines = added_lines(repo, merge_base, head, test_paths)
    if lines is None:
        failure_covered = True  # the diff is too large to judge — do not flag it
        warnings.append("diff too large to judge the failure-path coverage")
    else:
        failure_covered = has_failure_path(lines)
    return {
        "paths": paths,
        "rejected": rejected,
        "rejected_reasons": reasons,
        "no_tests": bool(code_paths) and not test_paths,
        "missing_failure_tests": control and not failure_covered,
        "failure_tests": failure_covered,
        "control_plane": control,
        "base": merge_base,
        "warnings": warnings,
    }
