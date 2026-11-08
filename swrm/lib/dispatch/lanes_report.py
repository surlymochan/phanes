#!/usr/bin/env python3
"""The busy/idle summary of the fleet lanes (Refs #1503).

The supervisor needs one number and one list every round: how many lanes run a card
and why an idle lane is idle.  Until now the answer was pieced together by hand out
of ``lanes-status.json``, ``state.json``, the ``PAUSE-*`` markers and the log, and
twice a round ended with "nothing to dispatch" and nobody able to say why.

This module is that summary, read only: it looks at the files the fleet already
writes and never dispatches, never writes and never changes the state of the fleet.

The reason of an idle lane:

``paused:<file>``
    the lane, its host or its backend is paused (``PAUSE-<lane>``, ``PAUSE-HOST-<host>``,
    ``PAUSE-BACKEND-<backend>``, the global ``PAUSE`` or a record of the pause store).
``host_fail``
    the host of the lane failed (``ssh status timeout``, ``missing in batch``, ``down`` ...).
``drain``
    the lane is draining and takes no new card (``"drain": true`` of the lane
    configuration, the key the dispatcher reads).
``deps``
    every card the lane could take waits for a dependency that is still open.
``hot_blocked:<file>``
    every card the lane could take waits for a file that another busy lane holds.
``no_card``
    no card of the queue is takeable by the lane.
``ready``
    the lane is idle and there is a card it could take right now.
``unknown``
    the fleet could not read the state of the lane (``?``) or the queue of the
    dispatcher could not be read at all.

The rules of the dispatcher are the rules of this report: the same ``can_take``, the
same hot file guard (documentation files and the union never block, the declared
files are split on every separator, a cap below one is raised to one) and the same
fallback chain.  A report that disagrees with the dispatcher names the wrong reason.
"""
import argparse
import datetime
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys


# The configuration of the dispatcher.  Every value can be overridden by the
# configuration file of the dispatcher (``dispatch.json``); the defaults are the
# ones of the engine, so a report without a configuration says what the engine does.
DEFAULTS = {
    "repo": "surlymochan/phanes",
    "labels": {"ready": "lane-ready", "in_progress": "in-progress", "pr": "lane-pr",
               "failed": "lane-failed", "strict_backend": "strict-backend",
               "backend_prefix": "backend:"},
    "skip_labels": [],
    # The backends the fleet dispatches itself; a card of another backend is not
    # dispatchable here (the external fleet takes it).
    "internal_backends": ["pi", "codex", "agy", "copilot"],
    # The fallback chain of the backends.  Like in the engine it only decides the
    # order of the cards, never the eligibility: ``can_take`` does not use it.
    "fallback": {"pi": ["codex", "copilot", "agy"], "codex": ["pi", "copilot", "agy"],
                 "copilot": ["codex", "pi", "agy"], "agy": ["pi", "codex", "copilot"]},
    "never_take_re": {"codex": r"unix\s*socket|AF_UNIX|socketpair|needs-socket|Unix 套接字|套接字"},
    "depends_line_re": r"^\s*(?:依赖|Depends on)\s*[:：](.*)$",
    "hot_files": {"line_re": r"^\s*(?:热文件|Hot-files)\s*[:：](.*)$",
                  "union": [], "limits": {}, "default_limit": 1},
    # Documentation never blocks a card: a conflict there costs nothing.
    "doc_suffixes": [".md", ".markdown", ".rst", ".txt"],
    "doc_dirs": ["docs", "doc"],
}

# The states of the fleet that mean the host of the lane failed.
HOST_FAIL_RE = re.compile(
    r"timeout|timed out|fail(?:ed|ure)?|error|missing in batch|host in backoff"
    r"|\bdown\b|unreachable|refused|permission denied|could not resolve hostname"
    r"|connection (?:closed|reset)|broken pipe|no route to host", re.I)

# The separators of the hot file line of a card: whitespace, comma, Chinese comma,
# Chinese enumeration comma and the two semicolons.
HOT_SEPARATORS = re.compile(r"[\s,，、;；]+")

def _merge(base, override):
    """Merge a configuration into the defaults: the override wins, dicts merge key by key."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _lanes(raw):
    """The lanes of a configuration as ``(name, spec)`` pairs.

    The dispatcher writes ``{lane: {"host": ..., "backend": ...}}`` - the name of the
    lane is the key.  The fleet of the legacy autodispatcher writes
    ``{"lanes": [{"name": ...}]}``.  A bare list of lane names is accepted too and
    keeps its names.
    """
    if isinstance(raw, dict):
        if isinstance(raw.get("lanes"), list):
            return _lanes(raw["lanes"])
        return [(name, spec if isinstance(spec, dict) else {}) for name, spec in raw.items()]
    if isinstance(raw, list):
        rows = []
        for item in raw:
            if isinstance(item, dict):
                rows.append((item.get("name"), item))
            elif isinstance(item, str):
                rows.append((item, {}))
        return rows
    return []


def _status_text(entry):
    """The state text of a lane of the status file, whatever shape the file has.

    The flat map of the legacy autodispatcher holds the text itself
    (``{"P1": "busy #12"}``), the map of the dispatch engine holds a record with a
    ``state`` field (``{"P1": {"state": "busy #12"}}``).  Both give the same text.
    """
    if isinstance(entry, dict):
        for key in ("state", "status", "lane"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    if entry is None:
        return ""
    return str(entry)


def _is_busy(name, status, state):
    """Whether the lane runs a card right now.

    The fleet writes ``busy #<issue>`` for a lane it knows and a bare ``busy`` for a
    lane that runs a card the dispatcher does not know about; the state of the
    dispatcher holds the issue of the lane.
    """
    entry = (status or {}).get(name)
    if _status_text(entry).strip().lower().startswith("busy"):
        return True
    if isinstance(entry, dict) and entry.get("issue"):
        return True
    lane_state = (state or {}).get(name)
    return isinstance(lane_state, dict) and bool(lane_state.get("issue"))


def _pause_scope(key, value):
    """The scope of a pause record and the marker file it comes from.

    A record is keyed by its scope (``lane:P1``, ``host:h1``, ``backend:pi``,
    ``all``) or by the name of its marker file (``P1``, ``HOST-h1``,
    ``BACKEND-pi``); both spellings are understood.
    """
    scope, expiry, marker = key, None, None
    if isinstance(value, dict):
        scope = value.get("scope") or key
        expiry = value.get("expires_at")
        marker = value.get("file") or value.get("marker")
    elif isinstance(value, str) and value:
        marker = value
    text = str(scope)
    if text == "all":
        scope = "all"
    elif text == "mint":
        scope = "mint"
    elif text.startswith("lane:"):
        scope = "lane:" + text[len("lane:"):]
    elif text.startswith("host:"):
        scope = "host:" + text[len("host:"):]
    elif text.startswith("backend:"):
        scope = "backend:" + text[len("backend:"):]
    elif text.startswith("HOST-"):
        scope = "host:" + text[len("HOST-"):]
    elif text.startswith("BACKEND-"):
        scope = "backend:" + text[len("BACKEND-"):]
    else:
        scope = "lane:" + text
    if not marker:
        if scope == "all":
            marker = "PAUSE"
        elif scope == "mint":
            marker = "PAUSE-MINT"
        elif scope.startswith("host:"):
            marker = "PAUSE-HOST-" + scope[len("host:"):]
        elif scope.startswith("backend:"):
            marker = "PAUSE-BACKEND-" + scope[len("backend:"):]
        else:
            marker = "PAUSE-" + scope[len("lane:"):]
    return scope, expiry, marker


def _active_pauses(pauses):
    """The active pauses as ``scope -> marker file``; an expired record is not active."""
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    active = {}
    for key, value in (pauses or {}).items():
        scope, expiry, marker = _pause_scope(key, value)
        if expiry is not None:
            try:
                if float(expiry) <= now:
                    continue
            except (TypeError, ValueError):
                continue
        if scope not in active or not isinstance(value, dict):
            active[scope] = marker
    return active


def _default_can_take(backend, item, config=None, lane=None):
    """Whether a lane of the backend could take the card.

    The rules are the ones of the engine: a card pinned to another backend is still
    takeable (the fallback chain of the backends decides the order of the cards, not
    their eligibility), a card pinned with ``strict-backend`` is never taken by
    another backend - agy included - and a backend that refuses a kind of work
    (``never_take_re``) never takes it either. Verify cards require a review lane
    (CX2/CX4/CX5, including their hyphen or underscore suffixes).
    """
    cfg = _merge(DEFAULTS, config or {})
    if item.get("verify") and not re.fullmatch(r"CX(?:2|4|5)(?:[-_].*)?", str(lane), re.I):
        return False
    pinned = item.get("backend")
    if not pinned or pinned == backend:
        return True
    if item.get("strict"):
        return False
    pattern = (cfg.get("never_take_re") or {}).get(backend)
    if pattern:
        haystack = (item.get("title") or "") + "\n" + (item.get("body") or "")
        if re.search(pattern, haystack, re.I):
            return False
    return True


# Keep the default admission rule available to existing callers.
can_take = _default_can_take


def _is_internal_card(item, internal):
    """Whether the card belongs to the backends of the fleet.

    A card of an external backend is not dispatchable here, and a card that says so
    itself is not either.
    """
    if not isinstance(item, dict):
        return False
    if item.get("external"):
        return False
    backend = item.get("backend")
    if backend and internal and backend not in internal:
        return False
    return True


def _is_doc_file(path, config=None):
    """Whether the file is documentation; documentation never blocks a card."""
    cfg = _merge(DEFAULTS, config or {})
    text = str(path or "").strip().replace("\\", "/").lower()
    if not text:
        return True
    suffixes = tuple(cfg.get("doc_suffixes") or [])
    if suffixes and text.endswith(suffixes):
        return True
    dirs = [str(name).lower() for name in (cfg.get("doc_dirs") or [])]
    return any(name in text.split("/")[:-1] for name in dirs)


def _hot_files(body, config=None):
    """The hot files a card declares, split on every separator.

    The dispatcher splits the ``热文件：`` line on whitespace, ``,``, ``，``, ``、``,
    ``;`` and ``；``; a file named ``a.py、b.py`` is two files, not one.
    """
    cfg = _merge(DEFAULTS, config or {})
    hot_cfg = cfg.get("hot_files") or {}
    pattern = hot_cfg.get("line_re") or DEFAULTS["hot_files"]["line_re"]
    match = re.search(pattern, body or "", re.M)
    if not match:
        return []
    files = []
    for part in HOT_SEPARATORS.split(match.group(1)):
        part = part.strip().rstrip("。")
        if part:
            files.append(part)
    return files


def _hot_limit(path, config=None):
    """The cap of a file; a cap below one is raised to one, so a free file stays free."""
    cfg = _merge(DEFAULTS, config or {})
    hot_cfg = cfg.get("hot_files") or {}
    limits = hot_cfg.get("limits") or {}
    try:
        limit = int(limits.get(path, hot_cfg.get("default_limit", 1)))
    except (TypeError, ValueError):
        limit = 1
    return max(1, limit)


def _hot_conflict(cards, held, config=None):
    """The file that blocks every candidate card, or None when a card could run.

    Only the cards whose dependencies are satisfied are candidates: a card that waits
    for a dependency never blocks a lane.  Documentation and the union of the
    configuration never block either.
    """
    cfg = _merge(DEFAULTS, config or {})
    union = (cfg.get("hot_files") or {}).get("union") or []
    conflicts = []
    for item in cards:
        conflict = None
        for filename in item.get("hot") or []:
            if _is_doc_file(filename, cfg):
                continue
            if any(fnmatch.fnmatch(str(filename), str(pattern)) for pattern in union):
                continue
            if held.get(filename, 0) >= _hot_limit(filename, cfg):
                conflict = filename
                break
        conflicts.append(conflict)
    if conflicts and all(conflicts):
        return conflicts[0]
    return None


def summarize(lanes_cfg, lanes_status, state, pauses, queue, config=None, can_take=None):
    """The busy/idle summary of the lanes and the reason of every idle lane.

    ``lanes_cfg`` is the lane configuration of the fleet, ``lanes_status`` the status
    file (flat map or the map of the engine), ``state`` the state of the dispatcher,
    ``pauses`` the pause records and ``queue`` the cards the dispatcher could hand
    out.  ``queue=None`` means the queue could not be read: the report then says
    ``unknown`` instead of pretending the queue is empty.  Every card needs its
    ``deps_ok`` and its ``hot`` files; the caller that has them (the command line
    reads them from GitHub) fills them in.

    ``can_take(backend, card)`` may replace the default backend admission rule.
    Without it, the report uses the dispatcher rules from ``config`` (including
    the fallback semantics and lane-specific verify gate). External cards are
    always excluded; dependency and hot-file guards still apply after admission.
    """
    cfg = _merge(DEFAULTS, config or {})
    rows = _lanes(lanes_cfg)
    status = lanes_status if isinstance(lanes_status, dict) else {}
    state = state if isinstance(state, dict) else {}
    paused = _active_pauses(pauses)
    internal = set(cfg.get("internal_backends") or [])
    cards = [item for item in (queue or []) if _is_internal_card(item, internal)]

    # The files the busy lanes hold: the conflict of a card is a conflict with them.
    held = {}
    for name, spec in rows:
        if not _is_busy(name, status, state):
            continue
        entry = state.get(name)
        if isinstance(entry, dict):
            for filename in entry.get("hot") or []:
                held[filename] = held.get(filename, 0) + 1

    busy_counts, idle_counts, idle = {}, {}, []
    for name, spec in rows:
        backend = spec.get("backend") or "unknown"
        raw_backend = str(spec.get("backend") or "")
        host = str(spec.get("host") or "")
        busy_counts.setdefault(backend, 0)
        idle_counts.setdefault(backend, 0)
        if _is_busy(name, status, state):
            busy_counts[backend] += 1
            continue
        idle_counts[backend] += 1
        text = _status_text(status.get(name)).strip().lower()
        reason = None
        if "all" in paused:
            reason = "paused:" + paused["all"]
        elif "lane:" + str(name) in paused:
            reason = "paused:" + paused["lane:" + str(name)]
        elif host and "host:" + host in paused:
            reason = "paused:" + paused["host:" + host]
        elif raw_backend and "backend:" + raw_backend in paused:
            reason = "paused:" + paused["backend:" + raw_backend]
        elif HOST_FAIL_RE.search(text):
            reason = "host_fail"
        elif spec.get("drain") or "drain" in text:
            reason = "drain"
        elif text and text != "idle":
            # Only an idle fleet lane can receive work. Other known or future
            # states (for example starting or crashed) cannot be inferred from
            # the queue, even if no more specific reason is available.
            reason = "unknown"
        elif queue is None:
            reason = "unknown"
        else:
            eligible = [item for item in cards
                        if (can_take(backend, item) if can_take is not None
                            else _default_can_take(backend, item, cfg, lane=name))]
            ready = [item for item in eligible if item.get("deps_ok", True) is not False]
            if not eligible:
                reason = "no_card"
            elif not ready:
                reason = "deps"
            else:
                blocked = _hot_conflict(ready, held, cfg)
                reason = ("hot_blocked:" + blocked) if blocked else "ready"
        idle.append({"lane": name, "backend": backend, "reason": reason})
    return {"backends": {backend: {"busy": busy_counts[backend], "idle": idle_counts[backend]}
                         for backend in busy_counts},
            "busy": sum(busy_counts.values()),
            "idle": idle,
            "queue_known": queue is not None}


def _read(path, default):
    """The JSON of a file, or the default when the file is missing or broken."""
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, TypeError, ValueError):
        return default


def _deps_ok(body, config=None):
    """Whether every dependency of the card is closed.

    The dispatcher reads the ``依赖：#N`` line of the card and checks the state of
    every dependency; a card with an open dependency waits for it.
    """
    cfg = _merge(DEFAULTS, config or {})
    pattern = cfg.get("depends_line_re") or DEFAULTS["depends_line_re"]
    match = re.search(pattern, body or "", re.M)
    if not match:
        return True
    for number in re.findall(r"#(\d+)", match.group(1)):
        args = ["gh", "issue", "view", number, "--json", "state"]
        if cfg.get("repo"):
            args[2:2] = ["-R", cfg["repo"]]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, check=False)
        except (OSError, TypeError, ValueError):
            return False
        if proc.returncode:
            return False
        try:
            state = json.loads(proc.stdout or "{}").get("state")
        except (TypeError, ValueError, AttributeError):
            return False
        if state != "CLOSED":
            return False
    return True


def _valid_verify_body(body):
    """The verify heading and required fields, as checked by the autodispatcher.

    A target must have its own nonempty value; evidence must include a nonempty
    checkbox item. A following line cannot supply a missing target or item value.
    """
    heading = re.search(r"^##[^\S\r\n]*Verify[^\S\r\n]*Intent[^\S\r\n]*$", body, re.M | re.I)
    target = re.search(r"(?mi)^[ \t]*目标 PR[^\S\r\n]*[:：][^\S\r\n]*(?=\S)[^\r\n]*$", body)
    evidence = re.search(
        r"(?mi)^[ \t]*证据要求[^\S\r\n]*[:：][^\S\r\n]*$"
        r"(?:\r?\n(?:[ \t]*\r?\n)*[ \t]*[-*][ \t]*\[[ xX]\][^\S\r\n]*\S[^\r\n]*)+", body)
    return bool(heading and target and evidence)


def _queue(config=None):
    """The cards the dispatcher could hand out right now.

    The queue of the dispatcher is the list of the open cards with the ready label;
    it lives in the memory of the tick, so the report reads it the same way: one
    ``gh issue list`` with the label of the configuration, followed by an authoritative
    ``gh issue view`` of each hit's state and labels (the search index can lag edits).
    ``None`` means the queue
    is unknown (no ``gh``, no network, a broken configuration) - the report says so
    instead of pretending the queue is empty.
    """
    cfg = _merge(DEFAULTS, config or {})
    labels = cfg.get("labels") or {}
    ready_label = labels.get("ready", "lane-ready")
    args = ["gh", "issue", "list", "--state", "open", "--label", ready_label,
            "--limit", "100", "--json", "number,title,labels,body"]
    if cfg.get("repo"):
        args[2:2] = ["-R", cfg["repo"]]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
    except (OSError, TypeError, ValueError):
        return None
    if proc.returncode:
        return None
    try:
        raw_cards = json.loads(proc.stdout or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(raw_cards, list):
        return None
    internal = set(cfg.get("internal_backends") or [])
    prefix = labels.get("backend_prefix", "backend:")
    cards = []
    for raw in raw_cards:
        if not isinstance(raw, dict):
            continue
        number = raw.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            return None
        args = ["gh", "issue", "view", str(number), "--json", "state,labels"]
        if cfg.get("repo"):
            args[2:2] = ["-R", cfg["repo"]]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, check=False)
            if proc.returncode:
                return None
            issue = json.loads(proc.stdout)
            if (not isinstance(issue, dict) or issue.get("state") not in ("OPEN", "CLOSED")
                    or not isinstance(issue.get("labels"), list)):
                return None
            names = {label if isinstance(label, str) else label["name"]
                     for label in issue["labels"]}
        except (OSError, TypeError, ValueError, KeyError):
            # An unreadable hit is not known to be stale or dispatchable.
            return None
        if issue["state"] != "OPEN" or ready_label not in names:
            continue
        excluding = {labels.get("in_progress"), labels.get("pr"), labels.get("failed")}
        if names & excluding:
            continue
        if names & set(cfg.get("skip_labels") or []):
            continue
        pinned = next((name[len(prefix):] for name in names if name.startswith(prefix)), None)
        if pinned and internal and pinned not in internal:
            # A card of an external backend is not ours to dispatch.
            continue
        body = raw.get("body") or ""
        verify = "verify-intent" in names
        if verify and not _valid_verify_body(body):
            # A malformed verify card must never become ordinary dispatchable work.
            continue
        cards.append({"n": raw.get("number"), "number": raw.get("number"),
                      "title": raw.get("title") or "", "body": body,
                      "backend": pinned, "strict": labels.get("strict_backend") in names,
                      "verify": verify,
                      "deps_ok": _deps_ok(body, cfg), "hot": _hot_files(body, cfg)})
    return cards


def _pauses(state_dir):
    """The pauses of the fleet: the ``PAUSE*`` markers of the state directory and the
    records of the current home and legacy state-directory pause stores.
    Independent records keep separate keys so an expired record cannot hide a marker."""
    pauses = {}
    try:
        names = sorted(os.listdir(state_dir))
    except OSError:
        names = []
    for name in names:
        if name == "PAUSE":
            pauses["all"] = name
        elif name.startswith("PAUSE-"):
            suffix = name[len("PAUSE-"):]
            if suffix == "MINT":
                continue
            if suffix.startswith("HOST-"):
                pauses["host:" + suffix[len("HOST-"):]] = name
            elif suffix.startswith("BACKEND-"):
                pauses["backend:" + suffix[len("BACKEND-"):]] = name
            else:
                pauses["lane:" + suffix] = name
    home = (os.environ.get("OPENSWARM_HOME") or os.environ.get("XSWARM_HOME")
            or os.path.expanduser("~/.claude/swrm"))
    stores = {os.path.join(state_dir, "pauses"),
              os.path.join(os.path.expanduser(home), "pauses")}
    for store in sorted(stores):
        for path in sorted(glob.glob(os.path.join(store, "*.json"))):
            record = _read(path, {})
            if isinstance(record, dict) and record.get("scope"):
                pauses[path] = record
    return pauses


def _lane_path(path, state_dir):
    """The path of the lane configuration: relative to the state directory if needed."""
    if os.path.exists(path):
        return path
    candidate = os.path.join(state_dir, path)
    return candidate if os.path.exists(candidate) else path


def _summary_line(report):
    """The one line of the text report: ``busy 13: pi 9/11, codex 2/7``."""
    parts = []
    for backend, values in (report.get("backends") or {}).items():
        busy = values.get("busy", 0)
        parts.append("%s %d/%d" % (backend, busy, busy + values.get("idle", 0)))
    return "busy %d: %s" % (report.get("busy", 0), ", ".join(parts))


def main(argv=None):
    """The command line of the report.

    ``lanes_report.py --state-dir D --lanes lanes.json [--json]`` reads the files of
    the fleet out of the state directory, asks the dispatcher for its queue and
    prints the summary: one line with the busy lanes per backend and one line per
    idle lane with its reason.
    """
    parser = argparse.ArgumentParser(description="the busy/idle summary of the fleet lanes")
    parser.add_argument("--state-dir", required=True, help="the state directory of the fleet")
    parser.add_argument("--lanes", required=True, help="the lane configuration of the fleet")
    parser.add_argument("--config", help="the configuration of the dispatcher "
                                         "(default: <state-dir>/dispatch.json)")
    parser.add_argument("--repo", help="the repository of the cards (overrides the configuration)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    config = _read(args.config or os.path.join(args.state_dir, "dispatch.json"), {})
    if not isinstance(config, dict):
        config = {}
    if args.repo:
        config = dict(config)
        config["repo"] = args.repo
    cfg = _merge(DEFAULTS, config)

    lanes = _read(_lane_path(args.lanes, args.state_dir), {})
    status = _read(os.path.join(args.state_dir, "lanes-status.json"), {})
    state = _read(os.path.join(args.state_dir, "state.json"), {})
    pauses = _pauses(args.state_dir)
    report = summarize(lanes, status, state, pauses, _queue(cfg), cfg)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    print(_summary_line(report))
    if not report.get("queue_known"):
        print("queue: unknown")
    for row in report.get("idle", []):
        print("%s: %s" % (row.get("lane"), row.get("reason")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
