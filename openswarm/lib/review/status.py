"""Lane-review commit status: build the status payload and post it (Refs #1447).

A lane review answers one question: may the reviewed commit be merged?  The answer is
published as a GitHub *commit status* on the reviewed commit, under the ``lane-review``
context, so that a branch protection rule (or a human) can see it without reading the
review text.

Three pieces live here, all of them free of side effects except the last one:

``parse_verdict(text)``
    Reads a review conclusion written in markdown and returns the ``(verdict, summary)``
    pair.  The verdict is the value of the first ``VERDICT: APPROVE`` or
    ``VERDICT: CHANGES`` line; the marker must start the line and is case sensitive.
    A conclusion without a verdict line has no verdict at all (``None``) - it is not a
    rejection, it is a missing answer.

``build_status(verdict, sha, reviewer, backend, model, run_id)``
    Turns a verdict into the status payload: ``APPROVE`` is ``success``, ``CHANGES`` is
    ``failure`` and a missing verdict is ``pending``.

``post_status(repo, payload)``
    Sends the payload with ``gh api``.  It never raises: a missing repository or a
    missing GitHub credential yields ``{"skipped": reason}`` so that a review without a
    status is still a finished review.

``head_changed(reviewed_sha, current_sha)``
    Answers whether the PR head moved away from the reviewed commit.  A status belongs
    to the commit that was reviewed: any path that rewrites the head (rebase, a CI fix
    commit, a union merge) produces a commit nobody reviewed, so no status is sent for
    it and it has to be reviewed again.

The module is loaded by path: ``openswarm/lib/review/`` is a namespace package without
an ``__init__.py``, so it must not be imported as a package.  Standard library only,
Python 3.9+.
"""
import os
import re
import subprocess

# The two verdicts a review conclusion may carry, and the commit status they map to.
VERDICT_APPROVE = "APPROVE"
VERDICT_CHANGES = "CHANGES"
VERDICT_STATES = {VERDICT_APPROVE: "success", VERDICT_CHANGES: "failure"}
# A conclusion without a verdict is neither an approval nor a rejection.
STATE_PENDING = "pending"

# The context the status is published under; one lane review owns exactly one context.
DEFAULT_CONTEXT = "lane-review"
# GitHub truncates a status description at 140 characters.
MAX_DESCRIPTION = 140

# The verdict marker: it has to start the line and its value is case sensitive.
VERDICT_LINE_RE = re.compile(r"^VERDICT:[ \t]*(APPROVE|CHANGES)(?![A-Za-z0-9_])")
# The summary marker: the rest of the line is the summary.
SUMMARY_LINE_RE = re.compile(r"^SUMMARY:[ \t]*(.*)$")
# A commit sha is 40 hex characters; anything else is not a commit.
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def parse_verdict(text):
    """Return the ``(verdict, summary)`` pair of a review conclusion.

    The verdict is the value of the *first* ``VERDICT: APPROVE`` or ``VERDICT: CHANGES``
    line of the text; the marker has to start the line and is case sensitive, so
    ``verdict: approve`` is not a verdict.  The verdict is ``None`` when the text
    carries no verdict line.

    The summary is the text behind the first ``SUMMARY:`` line, ``None`` when there is
    none.
    """
    verdict = None
    summary = None
    for line in str(text or "").splitlines():
        if verdict is None:
            match = VERDICT_LINE_RE.match(line)
            if match:
                verdict = match.group(1)
                continue
        if summary is None:
            match = SUMMARY_LINE_RE.match(line)
            if match:
                summary = match.group(1).strip()
    return verdict, summary


def verdict_state(verdict):
    """Return the commit status of one verdict.

    ``APPROVE`` is ``success``, ``CHANGES`` is ``failure`` and a missing verdict is
    ``pending``.  Any other verdict is a bug in the caller and raises ``ValueError``.
    """
    if verdict is None:
        return STATE_PENDING
    word = str(verdict).strip().upper()
    if word not in VERDICT_STATES:
        raise ValueError("verdict must be APPROVE, CHANGES or None, got %r" % (verdict,))
    return VERDICT_STATES[word]


def verdict_word(verdict):
    """Return the verdict as it is written into a status description."""
    if verdict is None:
        return "PENDING"
    return str(verdict).strip().upper()


def describe(verdict, reviewer, backend, model, run_id):
    """Return the status description of one review.

    The description reads ``<VERDICT> by <reviewer> (<backend>/<model>) run <run_id>``
    and is cut to 140 characters, ending in an ellipsis, because GitHub refuses a
    longer one.
    """
    text = "%s by %s (%s/%s) run %s" % (
        verdict_word(verdict), reviewer, backend, model, run_id)
    if len(text) > MAX_DESCRIPTION:
        text = text[:MAX_DESCRIPTION - 1] + "…"
    return text


def build_status(verdict, sha, reviewer, backend, model, run_id, context=DEFAULT_CONTEXT):
    """Build the commit status payload of one lane review.

    The payload holds the keys ``sha``, ``state``, ``context`` and ``description``;
    ``post_status`` turns it into the ``gh api`` call.  The sha must be a 40 character
    hex commit, anything else raises ``ValueError``: a status that points at no commit
    would be reported on the wrong commit.
    """
    return {
        "sha": require_sha(sha),
        "state": verdict_state(verdict),
        "context": str(context or "").strip() or DEFAULT_CONTEXT,
        "description": describe(verdict, reviewer, backend, model, run_id),
    }


def require_sha(sha):
    """Return the sha when it is a 40 character hex commit, raise ``ValueError`` otherwise."""
    text = str(sha or "").strip()
    if not SHA_RE.match(text):
        raise ValueError("sha must be 40 hex characters, got %r" % (sha,))
    return text


def head_changed(reviewed_sha, current_sha):
    """Return True when the PR head is not the commit that was reviewed.

    A status describes the commit it was sent for.  As soon as the head is rewritten
    (rebase, a CI fix commit, a union merge) the reviewed commit is no longer the head
    and nobody reviewed the new one: the caller must send no status and must not merge.
    A missing sha on either side counts as changed, because an unknown head cannot be
    called unchanged.
    """
    reviewed = str(reviewed_sha or "").strip().lower()
    current = str(current_sha or "").strip().lower()
    if not reviewed or not current:
        return True
    return reviewed != current


def has_token(env=None):
    """Return True when the environment carries a GitHub token."""
    environ = os.environ if env is None else env
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = environ.get(name)
        if value and str(value).strip():
            return True
    return False


def gh_authenticated(runner=subprocess.run, env=os.environ):
    """Return True when ``gh auth status`` reports a usable login."""
    try:
        result = runner(["gh", "auth", "status"], capture_output=True, text=True, env=env)
    except OSError:
        return False
    return getattr(result, "returncode", 1) == 0


def post_status(repo, payload, runner=subprocess.run, env=os.environ):
    """Post one commit status and report what happened.

    The status is sent with::

        gh api -X POST repos/<repo>/statuses/<sha> \\
            -f state=<state> -f context=<context> -f description=<description>

    The call never raises.  A missing repository, a payload without a commit and a
    missing credential (no ``GH_TOKEN``, no ``GITHUB_TOKEN`` and no ``gh`` login) all
    return ``{"skipped": reason}``; a rejected call returns ``{"error": ...}``.  A
    finished review must not fail because its status could not be published.
    """
    repository = str(repo or "").strip()
    if not repository:
        return {"skipped": "no repository configured"}

    body = payload if isinstance(payload, dict) else {}
    sha = str(body.get("sha") or "").strip()
    if not SHA_RE.match(sha):
        return {"skipped": "payload carries no valid sha"}
    state = str(body.get("state") or "").strip()
    if not state:
        return {"skipped": "payload carries no state"}
    context = str(body.get("context") or "").strip() or DEFAULT_CONTEXT
    description = str(body.get("description") or "")

    if not has_token(env) and not gh_authenticated(runner, env):
        return {"skipped": "no github token and gh is not authenticated"}

    command = [
        "gh", "api", "-X", "POST", "repos/%s/statuses/%s" % (repository, sha),
        "-f", "state=%s" % state,
        "-f", "context=%s" % context,
        "-f", "description=%s" % description,
    ]
    try:
        result = runner(command, capture_output=True, text=True, env=env)
    except OSError as error:
        return {"skipped": "gh is not available: %s" % (error,)}
    returncode = getattr(result, "returncode", 0)
    if returncode != 0:
        return {
            "error": "gh api failed with exit code %s" % (returncode,),
            "returncode": returncode,
            "sha": sha,
            "state": state,
            "context": context,
        }
    return {
        "posted": True,
        "returncode": 0,
        "sha": sha,
        "state": state,
        "context": context,
    }


__all__ = [
    "DEFAULT_CONTEXT",
    "MAX_DESCRIPTION",
    "STATE_PENDING",
    "VERDICT_APPROVE",
    "VERDICT_CHANGES",
    "VERDICT_STATES",
    "build_status",
    "describe",
    "gh_authenticated",
    "has_token",
    "head_changed",
    "parse_verdict",
    "post_status",
    "require_sha",
    "verdict_state",
    "verdict_word",
]
