"""Dispatch queue: order the ready issues and plan a dispatch without touching GitHub.

Pure functions only (Refs #1394).  Nothing here runs `gh`, edits a label or starts a run:

``build_queue(issues, cfg)``
    Turns the rows of ``gh issue list --json number,title,labels,body`` into an ordered
    queue: only the ready issues that are not claimed, done or failed, ordered
    p0 < p1 < p2 < unlabelled and then by issue number.

``shadow_plan(queue, idle_lanes)``
    Answers "which idle lane would take which issue" as ``[{"issue", "lane", "reason"}]``
    and changes nothing.  A ``backend:<b>`` label pins an issue to that executor family,
    ``strict-backend`` forbids the cross-backend fallback; unpinned issues are the fallback
    of every lane.

The parent card wires both calls into ``openswarm/bin/openswarm``; this module stays a
dependency-free library (standard library only, Python 3.9+) so it can be unit tested
offline.  The label names are configurable: the autodispatch labels (``lane-ready``,
``in-progress``, ``lane-pr``, ``lane-failed``) are the defaults.
"""

# The four labels the queue is filtered by; every one of them can be renamed in cfg.
DEFAULT_LABELS = {
    "ready": "lane-ready",
    "in_progress": "in-progress",
    "pr": "lane-pr",
    "failed": "lane-failed",
}

# Priority labels, ordered: p0 first, p2 last, an issue without one sorts after all of them.
PRIORITY_LABELS = {"p0": 0, "p1": 1, "p2": 2}
UNPRIORITIZED = len(PRIORITY_LABELS)  # 3

BACKEND_PREFIX = "backend:"          # pins an issue to one executor family
STRICT_BACKEND_LABEL = "strict-backend"  # forbids the cross-backend fallback

__all__ = [
    "DEFAULT_LABELS",
    "PRIORITY_LABELS",
    "UNPRIORITIZED",
    "BACKEND_PREFIX",
    "STRICT_BACKEND_LABEL",
    "label_names",
    "issue_labels",
    "build_queue",
    "shadow_plan",
]


def label_names(cfg=None):
    """Resolve the configurable label names (ready / in_progress / pr / failed).

    A flat ``cfg["ready"]`` overrides the default, and ``cfg["labels"]["ready"]``
    overrides both.  Unknown or empty values keep the default.
    """
    names = dict(DEFAULT_LABELS)
    if not isinstance(cfg, dict):
        return names
    for key in names:
        value = cfg.get(key)
        if isinstance(value, str) and value.strip():
            names[key] = value.strip()
    nested = cfg.get("labels")
    if isinstance(nested, dict):
        for key in names:
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                names[key] = value.strip()
    return names


def issue_labels(issue):
    """Return the label names of one issue row (strings or ``{"name": ...}`` dicts)."""
    if not isinstance(issue, dict):
        return []
    names = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _priority_of(labels):
    """Return ``(rank, label)`` of the strongest priority label of an issue."""
    best = (UNPRIORITIZED, None)
    for name in labels:
        rank = PRIORITY_LABELS.get(name.strip().casefold())
        if rank is not None and rank < best[0]:
            best = (rank, name.strip())
    return best


def _backend_of(labels):
    """Return the backend an issue is pinned to (``backend:<b>``), or None."""
    for name in labels:
        stripped = name.strip()
        if stripped.casefold().startswith(BACKEND_PREFIX):
            backend = stripped.split(":", 1)[1].strip()
            if backend:
                return backend
    return None


def build_queue(issues, cfg=None):
    """Build the dispatch queue from the rows of ``gh issue list --json number,title,labels,body``.

    A row is kept when it carries the ready label and none of the in-progress / pr /
    failed labels.  The result is ordered p0 < p1 < p2 < unlabelled and then by issue
    number; every row is a dict with the keys:

    ``number``          the issue number (int)
    ``title``           the issue title
    ``body``            the issue body
    ``labels``          the label names of the issue
    ``priority``        0 for p0, 1 for p1, 2 for p2, 3 when unlabelled
    ``priority_label``  the priority label, None when the issue has none
    ``backend``         the pinned backend (``backend:<b>``), None when unpinned
    ``strict``          True when the issue carries ``strict-backend``
    """
    names = label_names(cfg)
    ready = names["ready"].casefold()
    blockers = {names["in_progress"].casefold(), names["pr"].casefold(), names["failed"].casefold()}
    entries = []
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        labels = issue_labels(issue)
        folded = {name.casefold() for name in labels}
        if ready not in folded or folded & blockers:
            continue
        if str(issue.get("state") or "OPEN").strip().upper() != "OPEN":
            continue
        try:
            number = int(issue.get("number"))
        except (TypeError, ValueError):
            continue
        priority, priority_label = _priority_of(labels)
        entries.append({
            "number": number,
            "title": issue.get("title") or "",
            "body": issue.get("body") or "",
            "labels": labels,
            "priority": priority,
            "priority_label": priority_label,
            "backend": _backend_of(labels),
            "strict": STRICT_BACKEND_LABEL.casefold() in folded,
        })
    entries.sort(key=lambda entry: (entry["priority"], entry["number"]))
    return entries


def _lane_of(lane):
    """Return ``(name, backend)`` of one idle lane.

    A lane is a dict (``{"lane": "lane-a", "backend": "pi"}``); a bare string is a lane
    whose backend is unknown.
    """
    if isinstance(lane, dict):
        name = lane.get("lane") or lane.get("name") or lane.get("id") or ""
        backend = lane.get("backend")
    else:
        name = lane
        backend = None
    name = str(name).strip()
    if isinstance(backend, str) and backend.strip():
        backend = backend.strip()
    else:
        backend = None
    return name, backend


def _matches(pin, backend):
    """Whether a lane of this backend runs the backend an issue is pinned to."""
    return bool(pin) and bool(backend) and pin.casefold() == backend.casefold()


def _reason(entry, backend):
    """Explain why an issue was picked for a lane."""
    pin = entry.get("backend")
    if not pin:
        return "unpinned issue"
    if backend and pin.casefold() == backend.casefold():
        return "pinned to backend {}".format(pin)
    return "cross-pin fallback: pinned to backend {}, lane runs backend {}".format(
        pin, backend or "unknown")


def shadow_plan(queue, idle_lanes=None):
    """Plan which idle lane would take which queued issue, without changing anything.

    Every lane takes at most one issue and every issue goes to at most one lane.  The
    plan is built in three passes, so that a pinned issue is never stolen by a lane that
    cannot run it:

    1. every pinned issue goes to an idle lane of its own backend;
    2. the unpinned issues fill the lanes that are still free;
    3. the pins that no lane can run fall back to a free lane of another backend,
       unless the issue is ``strict-backend``.

    Returns a list of ``{"issue": <number>, "lane": <lane name>, "reason": <str>}``,
    one row per assigned lane, in the order of the idle lanes.
    """
    lanes = []
    for lane in idle_lanes or []:
        name, backend = _lane_of(lane)
        if name:
            lanes.append((name, backend))
    if not lanes:
        return []
    entries = [entry for entry in (queue or []) if isinstance(entry, dict) and "number" in entry]
    assigned = {}   # lane index -> (issue, reason)
    taken = set()   # issue numbers already planned
    free = list(range(len(lanes)))

    def take(entry, index):
        assigned[index] = (entry, _reason(entry, lanes[index][1]))
        taken.add(entry["number"])
        free.remove(index)

    # 1. a pinned issue goes to a lane that runs its backend
    for entry in entries:
        pin = entry.get("backend")
        if not pin:
            continue
        for index in list(free):
            if _matches(pin, lanes[index][1]):
                take(entry, index)
                break

    # 2. the unpinned issues fill the lanes that are still free
    for entry in entries:
        if entry["number"] in taken or entry.get("backend"):
            continue
        if not free:
            break
        take(entry, free[0])

    # 3. the pins no lane can run fall back to another backend, unless they are strict
    for entry in entries:
        if entry["number"] in taken or entry.get("strict"):
            continue
        if not free:
            break
        take(entry, free[0])

    return [
        {"issue": entry["number"], "lane": lanes[index][0], "reason": reason}
        for index, (entry, reason) in sorted(assigned.items())
    ]
