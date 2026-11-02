"""Backlog-balanced reviewer selection (pure module, no I/O).

The dispatcher may review a pull request on any idle lane.  When several lanes were
free the old tie-break (the LRU history that all dispatches share) kept picking the
same lane, so one review lane reviewed round after round while the other two stayed
idle.  A review is the bottleneck of the merge pipeline: a PR waits for its verdict
before it can merge, so an idle review lane is wasted capacity.

The dispatcher therefore counts, per lane, the reviews that lane still owes a
verdict for -- its *review backlog*:

  * the PR the lane is reviewing right now (the running review), and
  * the PRs whose review run failed and wait for a retry (the review queue).

The next PR goes to the lane with the smallest backlog.  Lanes with the same backlog
are served round-robin: the review lane that waited longest wins.  A lane that holds
more reviews than ``review.lane_max`` is skipped, so the PR waits for a free lane
instead of piling up on a single lane.

The claim ("this lane still owes this PR a verdict") lives in the review record of
the PR, under ``pending_lane``; the dispatcher sets it when it hands a PR to a lane
and releases it when the review concluded or the PR left the review pipeline.

Refs #1701.
"""

CLAIM_KEY = "pending_lane"


def backlog(reviews, state=None):
    """The number of reviews every lane still owes a verdict for.

    ``reviews`` maps a PR number to its review record; a record naming a lane in
    ``pending_lane`` is a review that lane still owes.  ``state`` is the dispatch
    state: a lane that runs a review without a claim (a lost review ledger) still
    counts, so the backlog never understates the work in flight.
    """
    counts = {}
    claimed = set()
    for pr, entry in (reviews or {}).items():
        lane = (entry or {}).get(CLAIM_KEY)
        if not lane:
            continue
        claimed.add(str(pr))
        counts[lane] = counts.get(lane, 0) + 1
    for lane, st in (state or {}).items():
        if not isinstance(st, dict):
            continue
        pr = st.get("review_pr")
        if not pr or str(pr) in claimed:
            continue
        counts[lane] = counts.get(lane, 0) + 1
    return counts


def least_loaded(candidates, counts):
    """The candidates with the smallest backlog, ordered by (backlog, lane name)."""
    return sorted(candidates or [], key=lambda lane: (counts.get(lane, 0), lane))


def over_cap(lane, counts, cap):
    """True when the lane may not take another review (it already holds ``cap`` of them).

    A lane may hold ``cap`` reviews at the same time; a lane that already holds that
    many is skipped, so the PR waits for a free lane (a cap of 0 disables the check).
    """
    if not cap or cap <= 0:
        return False
    return counts.get(lane, 0) >= cap


def releasable(reviews, candidates, busy=()):
    """The PR numbers whose claim can be released: the PR left the review pipeline.

    A claim is a promise ("this lane still owes this PR a verdict").  It is released
    when the PR is no longer waiting for a review -- it was closed, parked for a human
    or is not a review candidate any more -- and the PR is not being reviewed right
    now.
    """
    keep = {str(pr) for pr in (candidates or ())}
    running = {str(pr) for pr in (busy or ())}
    out = []
    for pr, entry in (reviews or {}).items():
        if not (entry or {}).get(CLAIM_KEY):
            continue
        if str(pr) in keep or str(pr) in running:
            continue
        out.append(str(pr))
    return sorted(out, key=lambda pr: int(pr) if str(pr).isdigit() else 0)


def snapshot(counts, lanes=None):
    """The backlog of the given lanes as ``{lane: count}`` (0 for a lane without reviews)."""
    out = {lane: counts.get(lane, 0) for lane in (lanes or [])}
    for lane, count in (counts or {}).items():
        out.setdefault(lane, count)
    return out


def render(counts, lanes=None):
    """The backlog of the given lanes as log text (``CX2=0 CX4=1 CX5=0``)."""
    return " ".join(f"{lane}={counts.get(lane, 0)}" for lane in (lanes or sorted(counts or {})))
