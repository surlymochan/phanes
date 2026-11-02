"""Fleet status page: every water level of the fleet on one screen (Refs #1718).

The 2026-11-02 night shift had to look at five places to answer one question:
what is the fleet doing right now?  The lane status (``lanes-status.json``), the
GitHub label queue, the fleet capacity reservations, the MiniMax liveness log
and the host probes.  Every one of them carries its own clock, and a stale
reading looks exactly like a fresh one: ``lanes-status.json`` once sat still for
five minutes while the page kept being read as if it were live.

This module is the aggregation half of the fleet status page.  It turns the
sections the caller collected into one machine-readable page in which **every
section carries its own collection time and its own source**.  A section whose
collection failed is marked ``unverified`` (``未核实``) and keeps its error; it
never takes the rest of the page down with it.

The collection time of a section is the clock of its own source, never the
mtime of the file it happens to sit in: ``snapshot_at`` reads the ``_at`` a
dispatcher snapshot carries, so a copied, touched or rewritten file cannot make
a stalled snapshot look live.

The module is pure: it reads no file, runs no command and never touches the
clock.  The caller passes the collected data and the time the page belongs to,
so the whole page can be replayed from a snapshot.

Sections
--------

``lanes``        busy/idle lanes and the cards in flight (``lanes-status.json``)
``pool``         pool depth in two readings: the GitHub labels and the dispatcher's ``_ready``
``capacity``     the fleet capacity reservations and the age of the oldest one
``hosts``        one line per fleet host: alive or not
``burn``         the MiniMax consumption of the last 15 minutes (liveness reading)
``needs_human``  how many pull requests wait for a human
``pauses``       the PAUSE switches that are currently on

Usage::

    report = fleet_status.aggregate(sections, now=epoch)
    print(report["text"])          # the one-screen summary
    json.dump(report, ...)         # the machine-readable page

Standard library only, Python 3.9+.
"""

from __future__ import annotations

import datetime
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = [
    "BURN_WINDOW_S",
    "DEFAULT_MAX_AGE_S",
    "SCHEMA",
    "SECTION_KEYS",
    "aggregate",
    "burn_data",
    "capacity_data",
    "epoch",
    "hosts_data",
    "iso",
    "lane_data",
    "make_section",
    "needs_human_data",
    "pause_data",
    "pool_data",
    "render_text",
    "snapshot_at",
]

SCHEMA = 1

# A section whose source is older than this is stale: its numbers may no longer
# describe the fleet.  The 2026-11-02 incident was a five minute old lane
# status, so five minutes is the default line between "live" and "expired".
DEFAULT_MAX_AGE_S = 300.0

# The MiniMax burn window of the page: the last 15 minutes, the liveness reading.
BURN_WINDOW_S = 900.0

# The sections of the page, in the order they are printed.
SECTION_KEYS = ("lanes", "pool", "capacity", "hosts", "burn", "needs_human", "pauses")

# The sections that print the clock of each of their readings themselves: the
# pool section has two readings with two clocks, so one envelope stamp would
# hide one of them.
SELF_STAMPED_SECTIONS = ("pool",)

# The short labels of the text summary (the brain log reads Chinese).
SECTION_LABELS = {
    "lanes": "车道",
    "pool": "池",
    "capacity": "容量",
    "hosts": "主机",
    "burn": "烧量",
    "needs_human": "待人工",
    "pauses": "开关",
}

# The marker of a section that could not be collected.
UNVERIFIED = "unverified"

# Labels that take a ``lane-ready`` card out of the dispatchable pool.
POOL_EXCLUDING_LABELS = ("in-progress", "lane-pr", "lane-failed")

BACKEND_PREFIX = "backend:"

_ISSUE_RE = re.compile(r"#(\d+)")


# ----------------------------------------------------------------- time helpers

def epoch(value: Any, offset: Optional[float] = None) -> Optional[float]:
    """Return a POSIX timestamp for a number or an ISO-8601 string, else None.

    A naive stamp (no timezone) is read as local wall time; ``offset`` (the
    seconds east of UTC) names the timezone of the fleet instead, for the
    snapshots that record local time without saying which local time it is.

    A stamp that is not a finite number has no clock at all: ``"nan"`` and
    ``"inf"`` parse as numbers, but no moment in time is named by them, so
    they are rejected like any other unusable stamp.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            pass
        else:
            return number if math.isfinite(number) else None
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            if offset is None:
                return parsed.astimezone().timestamp()
            zone = datetime.timezone(datetime.timedelta(seconds=float(offset)))
            parsed = parsed.replace(tzinfo=zone)
        return parsed.timestamp()
    return None


def iso(value: Any) -> Optional[str]:
    """Return the ISO-8601 UTC stamp of a timestamp, or None when there is none."""
    moment = epoch(value)
    if moment is None:
        return None
    try:
        stamp = datetime.datetime.fromtimestamp(moment, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        # A number that is no date (too large, or not finite at all) has no
        # stamp either; it must never take the page down.
        return None
    return stamp.isoformat(timespec="seconds")


def snapshot_at(status: Any, offset: Optional[float] = None) -> Optional[float]:
    """Return the moment a snapshot claims for itself, or None.

    ``_at`` is the clock the dispatcher writes into its own snapshot, and it is
    the only stamp that a copied, touched or rewritten file cannot fake: the
    file mtime says when the file was last written, not when the reading was
    taken.  A snapshot without a valid ``_at`` has no clock at all, so the
    caller has to mark it unverified instead of pretending it is fresh.
    """
    if not isinstance(status, dict):
        return None
    stamp = status.get("_at")
    if stamp is None:
        lanes = status.get("lanes")
        if isinstance(lanes, dict):
            stamp = lanes.get("_at")
    return epoch(stamp, offset)


def _duration(seconds: Any) -> str:
    """Return a short human duration (``42m0s``, ``3h07m``)."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    if total < 0:
        total = 0
    if total < 60:
        return "%ds" % total
    if total < 3600:
        return "%dm%02ds" % (total // 60, total % 60)
    return "%dh%02dm" % (total // 3600, (total % 3600) // 60)


# ------------------------------------------------------------------- the page

def make_section(key: str, title: str, source: str, collected_at: Any = None,
                 data: Any = None, error: Optional[str] = None,
                 max_age_s: Optional[float] = None) -> Dict[str, Any]:
    """Build one section of the page.

    ``collected_at`` is the stamp of the *source*, not of this call: it is what
    tells the reader whether the numbers are still true.  ``error`` marks the
    section as unverified -- the page still ships, but the section says that it
    does not know instead of showing a stale number.
    """
    moment = epoch(collected_at)
    section: Dict[str, Any] = {
        "key": key,
        "title": title,
        "source": source,
        "collected_at": iso(moment),
        "collected_at_epoch": moment,
        "verified": error is None,
        "status": "ok" if error is None else UNVERIFIED,
        "error": error,
        "data": {} if data is None else data,
    }
    if max_age_s is not None:
        section["max_age_s"] = float(max_age_s)
    return section


def aggregate(sections: Iterable[Dict[str, Any]], now: Any = None,
              max_age_s: float = DEFAULT_MAX_AGE_S) -> Dict[str, Any]:
    """Assemble the page from the collected sections.

    Every section keeps its own stamp and source; the page adds the time it was
    built, the age of every section and a verdict that is false as soon as one
    section is unverified or expired.
    """
    moment = epoch(now)
    rows: List[Dict[str, Any]] = []
    for section in sections:
        row = dict(section)
        stamp = row.get("collected_at_epoch")
        age = None
        if moment is not None and epoch(stamp) is not None:
            age = max(0.0, moment - float(stamp))
        limit = row.get("max_age_s")
        limit = float(max_age_s) if limit is None else float(limit)
        row["age_s"] = age
        row["max_age_s"] = limit
        row["stale"] = None if age is None else age > limit
        rows.append(row)

    unverified = [row["key"] for row in rows if not row.get("verified", False)]
    stale = [row["key"] for row in rows if row.get("stale")]
    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": iso(moment),
        "generated_at_epoch": moment,
        "max_age_s": float(max_age_s),
        "sections": rows,
        "summary": {
            "total": len(rows),
            "verified": len(rows) - len(unverified),
            "unverified": len(unverified),
            "unverified_sections": unverified,
            "stale": len(stale),
            "stale_sections": stale,
        },
        "ok": not unverified and not stale,
    }
    report["text"] = render_text(report)
    return report


# ------------------------------------------------------------------- sections

def _lane_records(status: Any) -> Iterable[Any]:
    """Yield the ``(lane, state text, record)`` triples of a lanes-status snapshot.

    Two shapes are in the wild: the dispatcher's schema 2 (one dict per lane,
    with a ``state``) and the autodispatcher's flat ``{"M1": "busy #12"}``.
    """
    if not isinstance(status, dict):
        return
    lanes = status.get("lanes")
    if not isinstance(lanes, dict):
        lanes = status
    for name, value in lanes.items():
        if str(name).startswith("_") or name == "schema":
            continue
        if isinstance(value, dict):
            state = value.get("state") or value.get("status") or ""
            yield str(name), str(state), value
        else:
            yield str(name), str(value), {}


def lane_data(status: Any) -> Dict[str, Any]:
    """The busy/idle reading of a lanes-status snapshot.

    ``in_flight`` lists the cards the busy lanes hold.  A lane whose state is
    neither busy nor idle (an ssh error, for example) counts as unknown and
    keeps its raw text: a broken lane is not an idle lane.
    """
    lanes: List[Dict[str, Any]] = []
    busy = idle = unknown = 0
    in_flight: List[int] = []
    for name, state, record in _lane_records(status):
        text = state.strip()
        issue = record.get("issue") if isinstance(record, dict) else None
        match = _ISSUE_RE.search(text)
        if not isinstance(issue, int) and match:
            issue = int(match.group(1))
        lowered = text.lower()
        if lowered.startswith("busy"):
            kind = "busy"
            busy += 1
            if isinstance(issue, int):
                in_flight.append(issue)
        elif lowered in ("idle", "", "-"):
            kind = "idle"
            idle += 1
        else:
            kind = "unknown"
            unknown += 1
        lanes.append({"lane": name, "kind": kind, "state": text, "issue": issue,
                      "host": record.get("host"), "backend": record.get("backend")})
    reported = status if isinstance(status, dict) else {}
    return {
        "total": len(lanes),
        "busy": busy,
        "idle": idle,
        "unknown": unknown,
        "in_flight": sorted(in_flight),
        "lanes": lanes,
        "reported_busy": reported.get("_busy"),
        "reported_ready": reported.get("_ready"),
        "reported_blocked": reported.get("_blocked"),
        "at": reported.get("_at"),
    }


def _label_names(issue: Any) -> List[str]:
    """The label names of one card (strings or ``{"name": ...}`` dicts)."""
    if not isinstance(issue, dict):
        return []
    labels = issue.get("labels")
    if isinstance(labels, str):
        labels = [labels]
    if not isinstance(labels, (list, tuple, set, frozenset)):
        return []
    names = []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def pool_data(issues: Any, ad_ready: Any = None, target: Any = None) -> Dict[str, Any]:
    """The pool depth in two readings, side by side.

    The GitHub reading counts the open ``lane-ready`` cards that no other label
    claims; the dispatcher reading is the ``_ready`` of the autodispatcher.  The
    two are printed next to each other on purpose: a difference between them
    means one of the two is stale, and that is exactly what the night shift
    could not see.
    """
    ready = 0
    labelled = 0
    excluded = 0
    by_backend: Dict[str, int] = {}
    for issue in issues if isinstance(issues, (list, tuple)) else []:
        if not isinstance(issue, dict):
            continue
        names = _label_names(issue)
        folded = {name.lower() for name in names}
        if "lane-ready" not in folded:
            continue
        labelled += 1
        if folded & {label.lower() for label in POOL_EXCLUDING_LABELS}:
            excluded += 1
            continue
        if str(issue.get("state", "OPEN")).upper() != "OPEN":
            excluded += 1
            continue
        ready += 1
        for name in names:
            if name.lower().startswith(BACKEND_PREFIX):
                backend = name[len(BACKEND_PREFIX):].strip()
                if backend:
                    by_backend[backend] = by_backend.get(backend, 0) + 1
                break

    dispatcher = ad_ready if isinstance(ad_ready, int) and not isinstance(ad_ready, bool) else None
    delta = None if dispatcher is None else ready - dispatcher
    level = None
    if isinstance(target, int) and not isinstance(target, bool):
        level = "empty" if ready == 0 else ("ok" if ready >= target else "low")
    elif ready == 0:
        level = "empty"
    return {
        "gh": {"ready": ready, "labelled": labelled, "excluded": excluded,
               "by_backend": by_backend},
        "ad": {"ready": dispatcher},
        "delta": delta,
        "agree": None if dispatcher is None else ready == dispatcher,
        "level": level,
    }


def capacity_data(reservations: Any, now: Any, limits: Any = None) -> Dict[str, Any]:
    """The fleet capacity reservations and the age of the oldest one.

    A reservation that is never released is the failure this page has to make
    visible: the oldest one says how long the fleet has been held.
    """
    moment = epoch(now)
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for record in reservations if isinstance(reservations, (list, tuple)) else []:
        if not isinstance(record, dict):
            skipped += 1
            continue
        created = epoch(record.get("created"))
        if created is None:
            skipped += 1
            continue
        rows.append({"host": record.get("host"), "run_id": record.get("run_id"),
                     "backend": record.get("backend"), "provider": record.get("provider"),
                     "created": created})
    by_host: Dict[str, int] = {}
    by_backend: Dict[str, int] = {}
    for row in rows:
        for key, counts in (("host", by_host), ("backend", by_backend)):
            value = row.get(key)
            if isinstance(value, str) and value:
                counts[value] = counts.get(value, 0) + 1

    oldest = min(rows, key=lambda row: row["created"]) if rows else None
    oldest_age = None
    if oldest is not None and moment is not None:
        oldest_age = max(0.0, moment - oldest["created"])

    used: Dict[str, int] = {"total": len(rows)}
    for host, count in by_host.items():
        used["host:" + host] = count
    for backend, count in by_backend.items():
        used[backend] = count
    utilization: Dict[str, Any] = {}
    if isinstance(limits, dict):
        for key, limit in limits.items():
            if isinstance(limit, int) and not isinstance(limit, bool):
                utilization[key] = {"used": used.get(key, 0), "limit": limit}
    return {
        "total": len(rows),
        "oldest_age_s": oldest_age,
        "oldest": oldest,
        "by_host": by_host,
        "by_backend": by_backend,
        "skipped": skipped,
        "used": used,
        "limits": utilization,
        "reservations": rows,
    }


def hosts_data(state: Any, now: Any = None, offset: Optional[float] = None) -> Dict[str, Any]:
    """One line per fleet host: is it alive?

    ``fleet-health.json`` is the health probe's own snapshot; every host keeps
    the time it was probed, so a stale probe cannot pass for a live host.
    ``offset`` is the timezone (seconds east of UTC) of the probe's own stamps.
    """
    moment = epoch(now)
    hosts = state.get("hosts") if isinstance(state, dict) else None
    hosts = hosts if isinstance(hosts, dict) else {}
    rows: List[Dict[str, Any]] = []
    alive = 0
    worst = "ok"
    ranks = {"ok": 0, "soft": 1, "unreachable": 2, "hard": 3}
    for name, record in sorted(hosts.items()):
        record = record if isinstance(record, dict) else {}
        status = str(record.get("status", "unknown"))
        if status in ("ok", "soft"):
            alive += 1
        if ranks.get(status, 0) > ranks.get(worst, 0):
            worst = status
        stamp = epoch(record.get("updated_at"), offset)
        rows.append({"host": str(name), "status": status,
                     "ssh_fail_streak": record.get("ssh_fail_streak"),
                     "updated_at": iso(stamp),
                     "age_s": None if stamp is None or moment is None else max(0.0, moment - stamp)})
    return {
        "total": len(rows),
        "alive": alive,
        "down": len(rows) - alive,
        "worst": worst if rows else "unknown",
        "hosts": rows,
        "updated_at": state.get("updated_at") if isinstance(state, dict) else None,
    }


def burn_data(rows: Any, now: Any, window_s: float = BURN_WINDOW_S) -> Dict[str, Any]:
    """The MiniMax consumption of the last ``window_s`` seconds (liveness reading).

    The liveness reading counts the requests the broker actually served: it is
    the same log the watchdog judges a stalled lane from, so a lane that burns
    nothing and a lane that burns a lot are both visible here.
    """
    moment = epoch(now)
    limit = float(window_s)
    requests = 0
    ok = 0
    failed = 0
    rate_limited = 0
    in_tok = 0
    out_tok = 0
    skipped = 0
    by_lane: Dict[str, int] = {}
    newest = None
    for row in rows if isinstance(rows, (list, tuple)) else []:
        if not isinstance(row, dict):
            skipped += 1
            continue
        stamp = epoch(row.get("ts"))
        if stamp is None or moment is None:
            skipped += 1
            continue
        if stamp > moment or stamp < moment - limit:
            continue
        requests += 1
        newest = stamp if newest is None else max(newest, stamp)
        status = row.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            if status == 429:
                rate_limited += 1
                failed += 1
            elif 200 <= status < 300:
                ok += 1
            else:
                failed += 1
        lane = row.get("lane")
        name = lane if isinstance(lane, str) and lane else "-"
        by_lane[name] = by_lane.get(name, 0) + 1
        for key, total in (("in_tok", "in_tok"), ("out_tok", "out_tok")):
            value = row.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                if key == "in_tok":
                    in_tok += value
                else:
                    out_tok += value
    return {
        "window_s": limit,
        "requests": requests,
        "ok": ok,
        "failed": failed,
        "rate_limited": rate_limited,
        "in_tok": in_tok,
        "out_tok": out_tok,
        "by_lane": by_lane,
        "skipped": skipped,
        "last_ts": iso(newest),
    }


def needs_human_data(rows: Any) -> Dict[str, Any]:
    """How many pull requests wait for a human, and which ones."""
    items: List[Dict[str, Any]] = []
    for row in rows if isinstance(rows, (list, tuple)) else []:
        if not isinstance(row, dict):
            continue
        number = row.get("number")
        items.append({"number": number, "title": row.get("title"), "url": row.get("url")})
    numbers = [item["number"] for item in items if isinstance(item["number"], int)]
    return {"count": len(items), "numbers": numbers, "items": items}


def pause_data(records: Any, now: Any) -> Dict[str, Any]:
    """The PAUSE switches that are currently on.

    A pause without an expiry never ends by itself: it is listed with its age so
    that a forgotten switch stays visible.
    """
    moment = epoch(now)
    switches: List[Dict[str, Any]] = []
    for record in records if isinstance(records, (list, tuple)) else []:
        if not isinstance(record, dict):
            continue
        scope = str(record.get("scope", "?"))
        created = epoch(record.get("created_at"))
        expires = epoch(record.get("expires_at"))
        age = None
        if created is not None and moment is not None:
            age = max(0.0, moment - created)
        remaining = None
        if expires is not None and moment is not None:
            remaining = max(0.0, expires - moment)
        switches.append({"scope": scope, "reason": str(record.get("reason", "")),
                         "created_at": iso(created), "expires_at": iso(expires),
                         "age_s": age, "expires_in_s": remaining})
    switches.sort(key=lambda row: row["scope"])
    return {"count": len(switches), "switches": switches}


# ----------------------------------------------------------------- text output

def _lane_line(data: Dict[str, Any]) -> str:
    parts = ["busy=%d" % data.get("busy", 0), "idle=%d" % data.get("idle", 0)]
    if data.get("unknown"):
        parts.append("unknown=%d" % data["unknown"])
    cards = data.get("in_flight") or []
    parts.append("in_flight=%d%s" % (len(cards),
                                      " (" + ",".join("#%d" % card for card in cards) + ")"
                                      if cards else ""))
    return " ".join(parts)


def _pool_line(data: Dict[str, Any]) -> str:
    """The pool line: both readings, each with its own clock."""
    gh = data.get("gh") or {}
    ad = data.get("ad") or {}
    ready = "-" if ad.get("ready") is None else ad.get("ready")
    if ad.get("status") == "unverified":
        parts = ["gh=%s" % gh.get("ready", 0), "ad=%s(未核实)" % ready]
    else:
        parts = ["gh=%s" % gh.get("ready", 0), "ad=%s" % ready]
    if data.get("delta") is not None:
        parts.append("delta=%+d" % data["delta"])
    if gh.get("by_backend"):
        parts.append("by_backend=" + ",".join("%s:%d" % (name, count)
                                               for name, count in sorted(gh["by_backend"].items())))
    if gh.get("collected_at"):
        parts.append("gh@%s" % gh["collected_at"])
    if ad.get("collected_at"):
        parts.append("ad@%s" % ad["collected_at"])
    return " ".join(parts)


def _capacity_line(data: Dict[str, Any]) -> str:
    age = data.get("oldest_age_s")
    parts = ["reservations=%d" % data.get("total", 0)]
    parts.append("oldest=%s" % ("-" if age is None else _duration(age)))
    if data.get("oldest"):
        parts.append("oldest_run=%s" % (data["oldest"].get("run_id") or "-"))
    if data.get("by_host"):
        parts.append("by_host=" + ",".join("%s:%d" % (name, count)
                                           for name, count in sorted(data["by_host"].items())))
    return " ".join(parts)


def _hosts_line(data: Dict[str, Any]) -> str:
    parts = ["alive=%d/%d" % (data.get("alive", 0), data.get("total", 0)),
             "worst=%s" % data.get("worst", "unknown")]
    down = [row["host"] for row in data.get("hosts", []) if row.get("status") not in ("ok", "soft")]
    if down:
        parts.append("down=" + ",".join(down))
    return " ".join(parts)


def _burn_line(data: Dict[str, Any]) -> str:
    parts = ["%dm requests=%d" % (int(data.get("window_s", BURN_WINDOW_S)) // 60,
                                  data.get("requests", 0)),
             "ok=%d" % data.get("ok", 0),
             "rate_limited=%d" % data.get("rate_limited", 0),
             "in_tok=%d" % data.get("in_tok", 0),
             "out_tok=%d" % data.get("out_tok", 0)]
    return " ".join(parts)


def _needs_human_line(data: Dict[str, Any]) -> str:
    numbers = data.get("numbers") or []
    return "count=%d%s" % (data.get("count", 0),
                           " (" + ",".join("#%d" % number for number in numbers) + ")"
                           if numbers else "")


def _pause_line(data: Dict[str, Any]) -> str:
    switches = data.get("switches") or []
    names = []
    for row in switches[:6]:
        age = row.get("age_s")
        names.append("%s(%s)" % (row.get("scope"), "-" if age is None else _duration(age)))
    if len(switches) > 6:
        names.append("+%d" % (len(switches) - 6))
    return "count=%d%s" % (data.get("count", 0), (" " + " ".join(names)) if names else "")


_LINE_BUILDERS = {
    "lanes": _lane_line,
    "pool": _pool_line,
    "capacity": _capacity_line,
    "hosts": _hosts_line,
    "burn": _burn_line,
    "needs_human": _needs_human_line,
    "pauses": _pause_line,
}


def render_text(report: Dict[str, Any]) -> str:
    """Render the one-screen summary of a page.

    The summary is meant for the brain log: one line per section, the number
    first, then the time the numbers were collected and where they came from.
    An unverified section says so instead of showing a number it cannot stand
    behind, and an expired section is marked as expired.
    """
    summary = report.get("summary") or {}
    lines = ["fleet status @ %s  sections=%d unverified=%d stale=%d" % (
        report.get("generated_at") or "-", summary.get("total", 0),
        summary.get("unverified", 0), summary.get("stale", 0))]
    for section in report.get("sections") or []:
        key = section.get("key", "?")
        label = SECTION_LABELS.get(key, key)
        data = section.get("data") or {}
        builder = _LINE_BUILDERS.get(key)
        if not section.get("verified", False):
            body = "未核实: %s" % (section.get("error") or "unknown error")
            if builder and data:
                # What could be read is still worth showing next to the mark.
                body += " | " + builder(data)
        else:
            body = builder(data) if builder else json_dumps(data)
        stamp = section.get("collected_at")
        age = section.get("age_s")
        marks = []
        if section.get("stale"):
            marks.append("过期%s" % _duration(age))
        if age is not None and not section.get("stale"):
            marks.append("age=%s" % _duration(age))
        tail = ""
        if stamp and key not in SELF_STAMPED_SECTIONS:
            tail = "  @%s" % stamp
        if section.get("source"):
            tail += "  %s" % section["source"]
        if marks:
            tail += "  [%s]" % " ".join(marks)
        lines.append("[%s] %s%s" % (label, body, tail))
    return "\n".join(lines)


def json_dumps(data: Any) -> str:
    """Return a short JSON text of a data mapping (used for unknown sections)."""
    import json
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
