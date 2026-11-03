"""Lane-review verdict landing: did the verdict file reach the dispatcher? (Refs #1667).

A lane review ends with one verdict file (``.lane-review/PR-<n>.md``) committed on the
lane's branch.  The dispatcher harvests it from the lane's git ref.  When the file is not
there, the harvest used to log a single generic line ("no verdict file") and count one
failure, which hid *why* the verdict was missing:

``no_commit``
    The run produced no commit at all, so there is no ref to read a verdict from.  The lane
    may have written the file without committing it, or the runtime discarded it.
``file_missing``
    The commit exists, its tree reads fine, and the tree does not carry the verdict file:
    the lane committed something else, or wrote the file under another path.
``no_marker``
    The verdict file is there but carries no ``VERDICT:`` line: the lane wrote a review the
    dispatcher cannot read as an answer.  The text that was read is kept in the evidence,
    so the next reviewer sees what the lane actually wrote.
``unreadable``
    The lane ref itself could not be read (the fetch never landed): a transport problem,
    not a lane failure.
``git_error``
    The ref was readable but git could not read the object (a damaged object, a permission
    problem, a broken repository): the dispatcher cannot tell whether the lane delivered a
    verdict, so the miss is kept as evidence but is **not** a lane failure.

``landed`` is the one class that carries an answer.

Only ``no_commit``, ``file_missing`` and ``no_marker`` are the lane's fault
(``DELIVERY_CLASSES``).  ``unreadable`` and ``git_error`` are the transport's fault
(``TRANSPORT_CLASSES``): they say nothing about the lane and must never consume the
per-head failure budget, otherwise a broken repository parks a PR for a human after a
few read failures.

The module is pure: it classifies what the harvest saw and builds the evidence record that
is kept before the PR is handed to the next lane.  Standard library only, Python 3.9+.
"""

# The verdict file landed and carries a verdict.
LAND_LANDED = "landed"
# The run committed nothing: there is no ref a verdict could be read from.
LAND_NO_COMMIT = "no_commit"
# The commit exists, its tree reads fine, and the tree does not carry the verdict file.
LAND_FILE_MISSING = "file_missing"
# The verdict file exists but carries no VERDICT line.
LAND_NO_MARKER = "no_marker"
# The lane ref could not be read at all.
LAND_UNREADABLE = "unreadable"
# The ref was readable but git could not read the object behind it.
LAND_GIT_ERROR = "git_error"

# The classes that mean "the lane did not deliver a verdict": they count as lane failures.
DELIVERY_CLASSES = (LAND_NO_COMMIT, LAND_FILE_MISSING, LAND_NO_MARKER)

# The classes that mean "the dispatcher could not read": they are kept as evidence but
# never counted as a lane failure.
TRANSPORT_CLASSES = (LAND_UNREADABLE, LAND_GIT_ERROR)

# How many evidence records of one PR are kept.
EVIDENCE_KEEP = 5

# How much of the verdict file is kept in the evidence (the lane's own words).
VERDICT_TEXT_KEEP = 4000


def classify(has_commit, ref_present, show_rc, verdict, path_missing=None):
    """Return the landing class of one review run.

    ``has_commit`` says whether the run committed anything, ``ref_present`` whether the
    lane ref could be read, ``show_rc`` the exit code of reading the verdict file,
    ``verdict`` the parsed verdict (``None`` when the file carries no verdict line) and
    ``path_missing`` whether the tree of the commit was readable and did not carry the
    verdict path.

    The order matters: a run without a commit is reported as such even when the ref is
    gone, because that is the cause the lane has to fix.  A failed read of the file is
    only a ``file_missing`` when the tree could be read and did not carry the path: any
    other git failure is a ``git_error``, which says nothing about the lane.
    """
    if not has_commit:
        return LAND_NO_COMMIT
    if not ref_present:
        return LAND_UNREADABLE
    if show_rc:
        # `git show` failed: either the tree reads fine and does not carry the path
        # (the lane committed something else), or git itself is broken (a damaged
        # object, a permission problem).  Only the first one is the lane's fault.
        return LAND_FILE_MISSING if path_missing else LAND_GIT_ERROR
    if not verdict:
        return LAND_NO_MARKER
    return LAND_LANDED


def is_failure(landing):
    """Return True when a landing class counts as a lane failure."""
    return landing in DELIVERY_CLASSES


def failure_class(landing, error_class=""):
    """Return the class of one missed verdict.

    The run's own error class explains a run that ended badly (a crashed executor, a
    rate limit); when the run reported no error, the landing class says what is missing
    instead of the useless "unknown".
    """
    return str(error_class or "").strip() or str(landing or "").strip() or "unknown"


def describe(landing):
    """Return a short human sentence about one landing class."""
    return {
        LAND_LANDED: "the verdict file landed",
        LAND_NO_COMMIT: "the run committed nothing, so no verdict file exists",
        LAND_FILE_MISSING: "the commit does not carry the verdict file",
        LAND_NO_MARKER: "the verdict file carries no VERDICT line",
        LAND_UNREADABLE: "the lane ref could not be read",
        LAND_GIT_ERROR: "git could not read the object behind the lane ref",
    }.get(landing, "unknown landing")


def evidence(pr, lane, run, info, landing):
    """Return the evidence record of one review run: the scene kept before a re-dispatch.

    The record answers "what did the lane do, and where did the verdict go?" without the
    lane's run directory: the run status, its error and summary, the branch and commit it
    produced, the ref the dispatcher read, the git error it got and - when the file was
    read but carries no verdict - the text the lane actually wrote.
    """
    info = info or {}
    landing = landing or {}
    return {
        "pr": pr,
        "lane": lane,
        "run": run,
        "landing": landing.get("land"),
        "status": info.get("status"),
        "error_class": info.get("error_class"),
        "error": str(info.get("error") or "")[:400],
        "summary": str(info.get("summary") or "")[:600],
        "files": str(info.get("files") or "")[:400],
        "verify_rc": info.get("verify_rc"),
        "backend": info.get("backend"),
        "model": info.get("model"),
        "branch": info.get("branch"),
        "commit": info.get("commit"),
        "ref": landing.get("ref"),
        "verdict_path": landing.get("path"),
        "show_rc": landing.get("show_rc"),
        "show_error": landing.get("show_err"),
        "tree_rc": landing.get("ls_tree_rc"),
        "tree_error": landing.get("ls_tree_err"),
        "path_missing": landing.get("path_missing"),
        "verdict": landing.get("verdict"),
        # The lane's own words: a file without a VERDICT line is kept verbatim (cut),
        # so the next reviewer sees what the lane wrote instead of a bare class.
        "verdict_text": str(landing.get("text") or "")[:VERDICT_TEXT_KEEP],
    }


__all__ = [
    "DELIVERY_CLASSES",
    "EVIDENCE_KEEP",
    "LAND_FILE_MISSING",
    "LAND_GIT_ERROR",
    "LAND_LANDED",
    "LAND_NO_COMMIT",
    "LAND_NO_MARKER",
    "LAND_UNREADABLE",
    "TRANSPORT_CLASSES",
    "VERDICT_TEXT_KEEP",
    "classify",
    "describe",
    "evidence",
    "failure_class",
    "is_failure",
]
