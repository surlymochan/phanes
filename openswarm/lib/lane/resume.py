"""Lane restart recovery: what an unfinished run may resume from (Refs #1720).

A `lane serve` restart (a stalled restart, a host blip, a broker disconnect) used to void the run
that was in flight: the burned tokens and the half-finished change were gone and the card had to be
dispatched again from zero.  The lane supervisor therefore persists a *resume record* for every
in-flight run (branch, pushed commit, task file, workspace path, session id) and, when it starts
again, scans the unfinished runs of its lane and tries to resume them.

Recovery is graded and conservative:

* a run whose branch carries a pushed commit resumes from the branch tip;
* a run with only uncommitted workspace changes resumes from that state and is marked as such;
* a run whose state does not line up with the AD side is abandoned with a written reason.

The decision never guesses: an unreadable AD state, a missing workspace, a workspace that is not on
the run branch or a commit that was never pushed does not become a resume.  This module holds the
pure decision logic; the executor side (`bin/openswarm`) persists the records and applies the
decision.
"""
import json
import os

# Recovery grades.
TIER_COMMIT = "commit"        # the run branch carries a pushed commit: resume from the branch tip
TIER_WORKSPACE = "workspace"  # only uncommitted changes are left: restore them and say so
TIER_NONE = "none"            # nothing to resume from

ACTION_RESUME = "resume"
ACTION_ABANDON = "abandon"
ACTION_SKIP = "skip"          # the AD state cannot be read: the run is left as it is

# What the AD side (the dispatcher) still knows about a lane.
AD_IN_PROGRESS = "in_progress"  # the AD still runs this run on this lane
AD_REASSIGNED = "reassigned"    # the AD gave this lane another run, or the issue moved elsewhere
AD_DONE = "done"                # the AD side reports the issue as finished
AD_UNKNOWN = "unknown"          # the AD state is not readable: nothing can be confirmed

RECORD_NAME = "resume.json"

# The AD state entries that mean "this run is still on the lane".
_RUNNING_STATUS = ("in_progress", "running", "busy", "active", "assigned", "dispatched")
# The AD state entries that mean "this run is over".
_DONE_STATUS = ("done", "finished", "complete", "completed", "closed", "failed", "cancelled", "canceled")


def record_path(run_dir):
    """Path of the resume record inside a run directory."""
    return os.path.join(run_dir, RECORD_NAME)


def read_record(path):
    """Read a resume record; a missing or damaged record reads as None."""
    try:
        with open(path, encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def write_record(path, record):
    """Write a resume record atomically, so a crash never leaves a half-written record."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=1, ensure_ascii=False)
    os.replace(temporary, path)
    return record


def issue_keys(issue):
    """The comparable forms of an issue key: a number, a "PR12" key and their plain digits."""
    if issue is None:
        return set()
    text = str(issue).strip()
    if not text:
        return set()
    keys = {text}
    if text[:2].upper() == "PR":
        text = text[2:]
        keys.add(text)
    if text.isdigit():
        keys.add(str(int(text)))
    return keys


def entry_status(entry):
    """The explicit status an AD state entry carries, if it carries one."""
    if not isinstance(entry, dict):
        return None
    for key in ("status", "state", "issue_status"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower().replace("-", "_")
    return None


def ad_view_from_state(state, lane_name, run_id):
    """The AD-side view of one lane run, computed from a parsed AD state file.

    The AD state maps a lane name to the run it executes (``{"issue": 12, "run": "r-1", ...}``)
    and keeps the finished issues in ``_done``.  The view answers one question: may this lane
    resume this run?

    * ``in_progress`` -- the AD still runs this run on this lane and the issue is not finished;
    * ``reassigned``  -- the AD gave the lane another run, or the issue moved to another lane;
    * ``done``        -- the AD side already finished the issue;
    * ``unknown``     -- the AD state does not track this lane, so nothing can be confirmed.
    """
    unknown = {"status": AD_UNKNOWN, "run": None, "issue": None, "reason": ""}
    if not isinstance(state, dict):
        unknown["reason"] = "the AD state is not a mapping"
        return unknown
    entry = state.get(lane_name)
    if not isinstance(entry, dict):
        unknown["reason"] = "the AD state does not track lane %s" % lane_name
        return unknown
    entry_run = entry.get("run")
    issue = entry.get("issue")
    if entry_run != run_id:
        return {"status": AD_REASSIGNED, "run": entry_run, "issue": issue,
                "reason": "the AD side gave lane %s run %s" % (lane_name, entry_run or "none")}
    status = entry_status(entry)
    if status and status in _DONE_STATUS:
        return {"status": AD_DONE, "run": entry_run, "issue": issue,
                "reason": "the AD side reports issue %s as %s" % (issue, status)}
    keys = issue_keys(issue)
    done = state.get("_done")
    if isinstance(done, (list, tuple, set)):
        finished = set()
        for item in done:
            finished |= issue_keys(item)
        if keys & finished:
            return {"status": AD_DONE, "run": entry_run, "issue": issue,
                    "reason": "the AD side already finished issue %s" % issue}
    issues = state.get("_issues")
    if isinstance(issues, dict):
        row = issues.get(str(issue))
        row_status = entry_status(row)
        if row_status and row_status in _DONE_STATUS:
            return {"status": AD_DONE, "run": entry_run, "issue": issue,
                    "reason": "the AD side reports issue %s as %s" % (issue, row_status)}
    # The same issue on another lane means the card was re-dispatched in the meantime.
    for other, other_entry in state.items():
        if other == lane_name or str(other).startswith("_") or not isinstance(other_entry, dict):
            continue
        if issue_keys(other_entry.get("issue")) & keys and other_entry.get("run") != run_id:
            return {"status": AD_REASSIGNED, "run": entry_run, "issue": issue,
                    "reason": "issue %s was re-dispatched to lane %s" % (issue, other)}
    if not keys or status != "in_progress":
        return {"status": AD_UNKNOWN, "run": entry_run, "issue": issue,
                "reason": "the AD side does not explicitly confirm issue %s is in progress" % issue}
    return {"status": AD_IN_PROGRESS, "run": entry_run, "issue": issue, "reason": ""}


def grade_workspace(probe):
    """Grade the resumable state of a workspace.

    ``probe`` carries the live workspace state: ``tip`` (the run branch tip), ``base`` (the base
    commit of the task), ``pushed`` (the remote commit of the run branch, if the branch has one),
    ``ahead`` (the tip is a new commit beyond the base), ``pushed_point`` (that commit is pushed)
    and ``dirty`` (the workspace holds uncommitted changes).

    A commit is only a recovery point when it is a *pushed* commit: the branch tip has to be a new
    commit beyond the base, and the remote must not contradict it.  A run whose workspace still
    holds uncommitted work never falls back to the commit grade, because resetting the workspace to
    the branch tip would throw that work away.
    """
    def abandon(reason):
        return {"action": ACTION_ABANDON, "tier": TIER_NONE, "point": None, "reason": reason}

    probe = probe if isinstance(probe, dict) else {}
    tip = probe.get("tip")
    base = probe.get("base")
    dirty = bool(probe.get("dirty"))
    # The tip is a new commit only when it differs from the base: the base itself is never a
    # recovery point, even when the workspace reports the tip as ahead of it.
    ahead = bool(probe.get("ahead")) and bool(tip) and tip != base
    pushed_point = bool(probe.get("pushed_point")) and ahead
    if ahead and pushed_point and not dirty:
        return {"action": ACTION_RESUME, "tier": TIER_COMMIT, "point": tip,
                "reason": "resuming from the pushed branch tip %s" % (tip or "")[:12]}
    if dirty:
        return {"action": ACTION_RESUME, "tier": TIER_WORKSPACE, "point": "workspace",
                "reason": "recovered from the uncommitted workspace state"}
    if ahead:
        return {"action": ACTION_RESUME, "tier": TIER_WORKSPACE, "point": "workspace",
                "reason": "recovered from the workspace state (the branch commit is not pushed)"}
    return abandon("neither a pushed commit nor an uncommitted workspace change is left")


def plan_recovery(record, ad_view, run_status, workspace):
    """Decide whether an unfinished run can be resumed, and from which recovery point.

    ``record``     -- the executor's resume record (run id, lane, branch, commit, workspace, ...)
    ``ad_view``    -- the AD side view of the lane: {"status": ..., "run": ..., "issue": ...}
    ``run_status`` -- the run's status.json
    ``workspace``  -- the live workspace probe: {"exists", "dirty", "tip", "pushed", "ahead", ...}

    The returned decision is {"action", "tier", "point", "reason"}.
    """
    def abandon(reason):
        return {"action": ACTION_ABANDON, "tier": TIER_NONE, "point": None, "reason": reason}

    if not isinstance(record, dict) or not record.get("run_id"):
        return abandon("no resume record")
    run_id = record["run_id"]
    if (run_status or {}).get("state") == "finished":
        return abandon("the run already finished")
    view = ad_view if isinstance(ad_view, dict) else {}
    ad_status = view.get("status")
    if ad_status == AD_REASSIGNED:
        return abandon(view.get("reason") or "the AD side re-dispatched the task")
    if ad_status == AD_DONE:
        return abandon(view.get("reason") or "the AD side reports the issue as finished")
    if ad_status != AD_IN_PROGRESS:
        if ad_status == AD_UNKNOWN:
            # Without a readable dispatcher state the run cannot be confirmed.  It is left as it
            # is: neither resumed nor failed on a guess.
            return {"action": ACTION_SKIP, "tier": TIER_NONE, "point": None,
                    "reason": view.get("reason") or
                    "the AD side does not confirm the run is still in progress (%s)" % AD_UNKNOWN}
        return abandon(view.get("reason") or
                       "the AD side does not confirm the run is still in progress (%s)"
                       % (ad_status or AD_UNKNOWN))
    if view.get("run") and view.get("run") != run_id:
        return abandon("the AD side tracks run %s on this lane, not %s" % (view.get("run"), run_id))
    probe = workspace if isinstance(workspace, dict) else {}
    if not probe.get("exists"):
        return abandon("the workspace %s is gone" % (record.get("workspace") or "unknown"))
    branch = record.get("branch")
    head_branch = probe.get("head_branch")
    if branch and head_branch and head_branch != branch:
        return abandon("the workspace sits on branch %s, not on the run branch %s"
                       % (head_branch, branch))
    if branch and not head_branch:
        # A detached worktree only counts when it still points at the run branch tip.
        if not (probe.get("head") and probe.get("tip") and probe["head"] == probe["tip"]):
            return abandon("the workspace is detached and not on the run branch %s" % branch)
    return grade_workspace(probe)


def resume_prompt(record, decision):
    """The continuation prompt of a recovered run."""
    tier = (decision or {}).get("tier")
    point = (decision or {}).get("point")
    session = (decision or {}).get("session") or {}
    if tier == TIER_COMMIT:
        where = "the pushed branch tip commit %s" % (point or "")
        detail = ("The work of this run was committed; the workspace was reset to that commit and "
                  "the run continues from there.")
    elif tier == TIER_WORKSPACE:
        where = "the uncommitted changes in the workspace"
        detail = ("This run was recovered from uncommitted state: the workspace still holds the "
                  "uncommitted changes and they are not committed yet.")
    else:
        where = "the recovered workspace"
        detail = "The run continues from the recovered workspace."
    restored = ""
    if session.get("id"):
        restored = ("\nThe interrupted session %s was restored, so the reasoning this run already "
                    "spent is still part of the conversation.\n" % session.get("id"))
    return ("The lane supervisor restarted while this run was in flight, and the run was recovered.\n"
            "Recovery point: %s.\n%s%s\n"
            "The task below is unchanged. Continue from the recovery point, finish the remaining work "
            "and end with the final JSON report.\n\n" % (where, detail, restored))


def brain_line(run_id, tier, point, reason, lane=None):
    """One short brain-log line about a recovery attempt."""
    parts = ["run=%s" % run_id]
    if lane:
        parts.append("lane=%s" % lane)
    parts.append("tier=%s" % (tier or TIER_NONE))
    parts.append("point=%s" % (point or "none"))
    if reason:
        parts.append("reason=%s" % reason)
    return " ".join(parts)
