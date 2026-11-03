"""Content check of a pull request: does it deliver the card? (Refs #1814)

PR #1799 was merged with green checks, but the merged head carried only the
review record ``.lane-review/PR-1799.md``: the functional commits stayed on a
side branch, so the merge was not a delivery ("merge != delivery").  The merge
gate checked the review verdict, the CI state and the reviewed head sha, but
never the *content* of the pull request, so a pull request that only carries
review or evidence artifacts passed the gate.

This module owns that decision and knows nothing about GitHub:

``declared_paths(issue_body)``
    The artifact paths a card declares in its issue.

``classify_paths(files, expected_paths)``
    Which changed files are a delivery (a code/test/tool path, or a path the
    card declares) and which are only review/evidence artifacts.

``judge(files, expected_paths)``
    The verdict of one pull request: ``delivery`` or ``empty`` (a suspected
    empty delivery).

A card declares the paths it delivers in its issue, for example::

    产物路径：docs/architecture-review-brief.md
    Expected-Paths: `tools/check_evidence.py`

A changed file that matches a declared path counts as the delivery of the
card, so a documentation card is not killed by the check.  The declaration
wins over the artifact classification: a card may deliver an evidence file
when it says so.

``openswarm/lib/merge/`` is a namespace package without an ``__init__.py``,
so this module is loaded by path (see ``openswarm/bin/openswarm``).
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# The paths that carry a review or evidence artifact, not the delivery of a
# card.  A pull request that changes nothing else does not deliver anything.
DEFAULT_ARTIFACT_DIRS: Tuple[str, ...] = (".lane-review/", "docs/evidence/")

# The suffixes of the source files: a change of one of them changes the
# product, not a review artifact.
DEFAULT_CODE_EXTS: Tuple[str, ...] = (
    ".bash", ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
    ".jsx", ".kt", ".lua", ".mjs", ".cjs", ".php", ".pl", ".py", ".pyi", ".r",
    ".rb", ".rs", ".scala", ".sh", ".sql", ".swift", ".ts", ".tsx", ".zsh",
)

# The directories that hold code, tests or tools: every file in them counts.
# A directory of the repository root counts ("bin/openswarm"), and so does a
# directory of the same name deeper in the tree ("openswarm/bin/openswarm"):
# the tool of this repository is a delivery, not a review artifact.
DEFAULT_CODE_DIRS: Tuple[str, ...] = (
    "bin/", "lib/", "scripts/", "src/", "test/", "tests/", "tools/",
    ".github/workflows/",
)

# The configuration files that change the product when they change.
DEFAULT_CONFIG_FILES: Tuple[str, ...] = (
    "Cargo.toml", "Makefile", "go.mod", "package.json", "pyproject.toml",
    "requirements.txt", "setup.cfg", "setup.py", "tsconfig.json",
)

# The keys of an issue line that declares the expected artifact paths.  The
# keys are compared normalized (lower case, without spaces and dashes).
DECLARATION_KEYS: Tuple[str, ...] = (
    "产物", "产物文件", "产物路径", "交付物", "交付路径",
    "artifactpaths", "artifacts", "deliverables", "expectedpaths",
)

# One issue line: "key: value".
_DECLARATION_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]*)?(?P<key>[^:：\n]{1,40}?)[ \t]*[:：][ \t]*(?P<value>.+)$",
    re.M,
)


def normalize_path(path: Any) -> str:
    """The repository relative form of a path (no leading "./", no backslash)."""
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _normalize_key(text: Any) -> str:
    """The comparable form of a declaration key."""
    return re.sub(r"[\s\-_`*]+", "", str(text or "").strip().lower())


def extract_paths(text: Any) -> List[str]:
    """The path-like tokens of one declaration line.

    A backticked token is a path by itself; otherwise the line is split on
    whitespace and commas.  A bare word is only a path when it is the only
    token of the line ("产物路径：docs"), so a prose line never invents paths.
    """
    raw = str(text or "")
    quoted = re.findall(r"`([^`]+)`", raw)
    tokens = quoted or [token for token in re.split(r"[\s,，、;；]+", raw) if token]
    paths: List[str] = []
    for token in tokens:
        token = token.strip().strip("`\"'").rstrip(".,;")
        if not token or " " in token:
            continue
        if token.startswith(("http://", "https://")):
            continue
        if "/" not in token and "." not in token and len(tokens) > 1:
            continue
        paths.append(normalize_path(token))
    return paths


def declared_paths(issue_body: Any) -> List[str]:
    """The artifact paths the card declares in its issue body.

    The declaration lines look like ``产物路径：docs/x.md`` or
    ``Expected-Paths: `tools/y.py```; every path of every declaration line is
    returned, in the order of the lines, without duplicates.
    """
    paths: List[str] = []
    for match in _DECLARATION_LINE_RE.finditer(str(issue_body or "")):
        if _normalize_key(match.group("key")) not in DECLARATION_KEYS:
            continue
        for path in extract_paths(match.group("value")):
            if path and path not in paths:
                paths.append(path)
    return paths


def path_matches(changed: Any, declared: Any) -> bool:
    """Whether a changed file is the path a card declared.

    An exact match counts, a declared directory matches everything below it,
    and a declared glob is matched with ``fnmatch``.
    """
    left = normalize_path(changed)
    right = normalize_path(declared)
    if not left or not right:
        return False
    if left == right:
        return True
    if any(char in right for char in "*?["):
        return fnmatch.fnmatch(left, right)
    return left.startswith(right.rstrip("/") + "/")


def is_artifact_path(path: Any, artifact_dirs: Sequence[str] = DEFAULT_ARTIFACT_DIRS) -> bool:
    """Whether a path carries a review or evidence artifact."""
    text = normalize_path(path)
    if not text:
        return False
    for directory in artifact_dirs or ():
        prefix = normalize_path(directory).rstrip("/")
        if not prefix:
            continue
        if text == prefix or text.startswith(prefix + "/"):
            return True
    return False


def in_code_dir(text: str, code_dirs: Sequence[str] = DEFAULT_CODE_DIRS) -> bool:
    """Whether a path lies in a code, test or tool directory.

    The directory of the repository root counts ("tools/x.py"), and so does a
    directory of the same name deeper in the tree: the tool of this
    repository (``openswarm/bin/openswarm``) has no file extension, so only
    the directory tells that it is a delivery.
    """
    parents = text.split("/")[:-1]
    for directory in code_dirs or ():
        prefix = normalize_path(directory).rstrip("/")
        if not prefix:
            continue
        if text == prefix or text.startswith(prefix + "/"):
            return True
        if "/" not in prefix and prefix in parents:
            return True
    return False


def is_code_path(path: Any,
                 code_exts: Sequence[str] = DEFAULT_CODE_EXTS,
                 code_dirs: Sequence[str] = DEFAULT_CODE_DIRS,
                 config_files: Sequence[str] = DEFAULT_CONFIG_FILES) -> bool:
    """Whether a path is a code, test or tool path."""
    text = normalize_path(path)
    if not text:
        return False
    name = text.rsplit("/", 1)[-1]
    if name in (config_files or ()):
        return True
    lowered = name.lower()
    if any(lowered.endswith(ext) for ext in (code_exts or ())):
        return True
    return in_code_dir(text, code_dirs)


def classify_paths(files: Iterable[Any],
                   expected_paths: Optional[Iterable[Any]] = None,
                   artifact_dirs: Sequence[str] = DEFAULT_ARTIFACT_DIRS,
                   code_exts: Sequence[str] = DEFAULT_CODE_EXTS,
                   code_dirs: Sequence[str] = DEFAULT_CODE_DIRS,
                   config_files: Sequence[str] = DEFAULT_CONFIG_FILES) -> Dict[str, List[str]]:
    """Split the changed files into delivery, artifact and other paths.

    A declared path is a delivery even when it lives in an artifact
    directory: the declaration of the card wins.
    """
    expected = [normalize_path(path) for path in (expected_paths or []) if normalize_path(path)]
    delivery: List[str] = []
    artifacts: List[str] = []
    other: List[str] = []
    for path in files or []:
        text = normalize_path(path)
        if not text:
            continue
        if any(path_matches(text, declared) for declared in expected):
            delivery.append(text)
        elif is_artifact_path(text, artifact_dirs):
            artifacts.append(text)
        elif is_code_path(text, code_exts, code_dirs, config_files):
            delivery.append(text)
        else:
            other.append(text)
    return {"delivery": delivery, "artifacts": artifacts, "other": other}


def judge(files: Iterable[Any],
          expected_paths: Optional[Iterable[Any]] = None,
          **kwargs: Any) -> Dict[str, Any]:
    """The verdict of one pull request: ``delivery`` or ``empty``.

    ``empty`` means a suspected empty delivery: the pull request changes no
    code, test or tool path and none of the paths the card declared.
    """
    paths = [normalize_path(path) for path in (files or []) if normalize_path(path)]
    expected = list(expected_paths or [])
    groups = classify_paths(paths, expected, **kwargs)
    verdict: Dict[str, Any] = {
        "status": "delivery" if groups["delivery"] else "empty",
        "files": paths,
        "expected": expected,
    }
    verdict.update(groups)
    return verdict
