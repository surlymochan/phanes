#!/usr/bin/env python3
"""MiniMax quota burn watchdog: weekly burn rate plus the real hourly token read (Refs #1561, #1671).

Usage:
  python3 tools/mm-burn-rate.py --history FILE.jsonl [--append health.json]
                      [--broker-log FILE] [--quota-json FILE]
                      [--status-file FILE] [--low-tokens N] [--json]

The weekly burn comes from the health.json history (the quota percentages the
box health loop samples).  The hourly burn is the *real* MiniMax token
consumption: the provider request log of the MiniMax broker
(``openswarm/bin/pi-minimax-broker.mjs``) carries the token counters MiniMax
reported per request, so the sum over the last hour is the number the MiniMax
console shows for the same hour.  Those rows are the same ones the quota
watchdog reads (``openswarm/lib/supervise/liveness.py``,
``tools/broker-log-summary.py``).

The request log is found without configuration: the broker writes ``$PI_BROKER_LOG``
or, without it, ``<socket>.requests.jsonl`` next to its socket
(``openswarm/bin/pi-minimax-broker.mjs``); the socket is ``$PI_BROKER_SOCKET``,
``$OPENSWARM_PI_BROKER_SOCKET`` or the default of the box and of the fleet
coordinator.  The broker rotates its log by renaming it to ``<log>.1``, so every
rotated generation that is still there is read too: the hour is then complete
and the reading can be reconciled with the console.

A reading is only a reading when the collector behind it is alive and its log
is complete: the broker socket tells whether the collector still runs (a
stopped broker leaves a readable log behind, and that log says nothing about
the burn), the writability of the log tells whether the collector can write it
at all (a collector that answers but cannot append collects nothing), and the
request numbering of the broker (``id``) tells whether the log lost rows (a
second rotation inside the hour overwrites ``<log>.1``, and a failed append
drops a row).  A collector that is gone, a log that cannot be written and a
log that lost rows are *collection failures*: nothing is known about the burn,
so there is no reading and no alert -- a broken collection is never reported
as a fleet that burned nothing.

A quiet hour is only a zero burn when the collector is verified: the broker
socket answers, or the log itself carries a request of the hour (then the
collector demonstrably wrote inside the hour).  A quiet log of a collector that
cannot be verified is *unverified*: nothing is known about the burn, so there
is neither a reading nor an alert.  That is the difference between a fleet
that burned nothing and a collection that is broken.

The Token Plan counts inside a five hour window that resets at the ``end_time``
of the coding-plan remains payload (``--quota-json``): the interval total is
only summed inside one window, so a reading never mixes two intervals.

The JSON snapshot is written to ``--status-file`` (default: ``minimax-burn.json``
next to the history file) and is what the lane status shows.  The exit code is
1 while the watchdog alerts (a low token burn or a weekly exhaustion before
``weekly_end``), so the caller of the watchdog sees the alert.  The token alert
does not depend on the quota history: a collapsed burn alerts even when the
history is missing, stale or too short.  A broken quota collection (a health
snapshot that cannot be appended) does not stop the token reading either: the
snapshot is written anyway and the exit code is 2, so the operator sees the
broken quota collection while the burn is judged on its own.
"""
import argparse
import json
import math
import os
import socket
import stat
import sys
from datetime import datetime, timedelta, timezone

# Matches the box hotfix HEALTH_QUOTA_FRESH_SEC=900 used for other quota signals.
STALE_SEC = 900
# Anchor window: the nearest sample at most this long before the 6h cutoff is used.
ANCHOR_SEC = 900
# The MiniMax Token Plan counts inside a five hour window.
TOKEN_PLAN_INTERVAL_H = 5
# An hourly token read below this is a burn collapse (the fleet did nothing).
DEFAULT_LOW_TOKENS = 1000
# Epoch values above this are milliseconds, not seconds (1e11 s is year 5138).
_MS_EPOCH_THRESHOLD = 1e11
# The broker writes its request log next to its socket and rotates it by
# renaming it to "<log>.1" (openswarm/bin/pi-minimax-broker.mjs).
BROKER_LOG_SUFFIX = ".requests.jsonl"
ROTATED_SUFFIX = ".1"
# The rotated generations of the broker log that are read: the broker keeps
# one ("<log>.1"), an operator may keep more.
ROTATED_GENERATIONS = 10
# The MiniMax console counts in Beijing time: the hourly buckets use it, so a
# bucket is the figure of the console for that clock hour.
CONSOLE_TZ = timezone(timedelta(hours=8))
# The default MiniMax broker sockets: the fleet coordinator runs the broker
# under /run, the box keeps its socket in the cache directory.
BROKER_SOCKETS = ("/run/openswarm-pi/broker.sock",
                  "~/Library/Caches/openswarm-pi-broker/broker.sock")
# The hours of the request log the watchdog reads: the hourly buckets cover
# six hours, the Token Plan window five.
READ_WINDOW_H = 7


def parse_ts(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("expected a finite number")
    return float(value)


def _point(row):
    if not isinstance(row, dict):
        raise ValueError("row is not an object")
    ts = parse_ts(row.get("ts"))
    mm = row.get("minimax", row)
    if not isinstance(mm, dict):
        raise ValueError("minimax is not an object")
    pct = _num(mm.get("weekly_pct"))
    if not 0 <= pct <= 100:
        raise ValueError("weekly_pct out of range")
    return {"ts": ts, "weekly_pct": pct, "weekly_end": parse_ts(mm.get("weekly_end"))}


def read_history(path):
    """Return (points, skipped). A missing file counts as an empty history."""
    points, skipped = [], 0
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return points, skipped
    with fh:
        for raw in fh:
            if not raw.strip():
                continue
            try:
                points.append(_point(json.loads(raw)))
            except (ValueError, TypeError, KeyError, AttributeError):
                skipped += 1
    return points, skipped


def append_health(history, health_path, now=None):
    with open(health_path, encoding="utf-8") as fh:
        health = json.load(fh)
    if not isinstance(health, dict):
        raise ValueError("health.json top level is not an object")
    mm = health.get("minimax")
    if not isinstance(mm, dict):
        raise ValueError("health.json has no minimax object")
    now = (now or datetime.now().astimezone()).replace(second=0, microsecond=0)
    row = {"ts": now.isoformat(), "minimax": mm}
    try:
        _point(row)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"health.json minimax is not readable back: {exc}") from exc
    existing, _ = read_history(history)
    if any(p["ts"].astimezone(now.tzinfo).replace(second=0, microsecond=0) == now
           for p in existing):
        return False
    with open(history, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    return True


def broker_row(line):
    """Parse one row of the MiniMax provider request log.

    The real shape is the row the broker writes
    (``openswarm/bin/pi-minimax-broker.mjs``)::

        {"ts","id","lane","status","err","ms","in_tok","out_tok"}

    ``in_tok``/``out_tok`` are the token counters MiniMax reported for the
    request.  ``id`` is the number the broker gave the request: it counts the
    requests of one broker run, so a hole in the numbering is a request whose
    row never reached the log.  A row without a timestamp or with a non-numeric
    status is not a request and is skipped.
    """
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    stamp = obj.get("ts")
    if not isinstance(stamp, str):
        return None
    try:
        when = parse_ts(stamp)
    except ValueError:
        return None
    status = obj.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    lane = obj.get("lane") if isinstance(obj.get("lane"), str) else "-"
    rid = obj.get("id")
    if isinstance(rid, bool) or not isinstance(rid, int):
        rid = None
    return {"ts": when, "lane": lane, "status": status, "tokens": _row_tokens(obj), "rid": rid}


def _row_tokens(obj):
    """The tokens of one request row (missing counters count as zero)."""
    total = 0
    for key in ("in_tok", "out_tok"):
        value = obj.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            total += int(value)
    return total


def broker_socket_candidates(env=None):
    """The MiniMax broker socket paths, in the order they are looked up.

    The broker is started with its socket path as an argument
    (``openswarm/bin/pi-broker-service``); the fleet coordinator exports it
    as ``OPENSWARM_PI_BROKER_SOCKET``.
    """
    env = os.environ if env is None else env
    sockets = []
    for name in ("OPENSWARM_PI_BROKER_SOCKET", "PI_BROKER_SOCKET"):
        value = env.get(name)
        if value:
            sockets.append(value)
    sockets.extend(os.path.expanduser(socket) for socket in BROKER_SOCKETS)
    return sockets


def broker_log_candidates(env=None):
    """The MiniMax provider request logs, in the order they are looked up.

    The broker writes ``$PI_BROKER_LOG`` or, without it, ``<socket>.requests.jsonl``
    (``openswarm/bin/pi-minimax-broker.mjs``).  The explicit log wins, so an
    operator can point the watchdog at another file.
    """
    env = os.environ if env is None else env
    explicit = env.get("MM_BROKER_LOG") or env.get("PI_BROKER_LOG")
    if explicit:
        return [explicit]
    return [socket + BROKER_LOG_SUFFIX for socket in broker_socket_candidates(env)]


def default_broker_log(env=None):
    """The request log the watchdog reads: the first candidate that exists.

    Without any configuration the watchdog still reads the log of the running
    broker, so the reading exists in the normal setup (Refs #1671).
    """
    candidates = broker_log_candidates(env)
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0] if candidates else None


def broker_log_files(path):
    """The log files of one broker: the current one and its rotated generations.

    The broker rotates its audit log by renaming it to ``<log>.1``
    (``openswarm/bin/pi-minimax-broker.mjs``): without the rotated file the
    rows of the hour before the rotation are missing and the reading is too
    low to be reconciled with the console.  Every generation that is still
    there is read, so the hour is complete as long as the broker kept it.
    """
    if not path:
        return []
    files = []
    for candidate in ([path, path + ROTATED_SUFFIX]
                      + [f"{path}.{generation}"
                         for generation in range(2, ROTATED_GENERATIONS + 1)]):
        if os.path.exists(candidate):
            files.append(candidate)
    return files


def read_broker_log(path, since=None):
    """Return (rows, skipped, files) of the MiniMax provider request log.

    ``files`` are the log files that could be read: an empty list means the
    collection is not there at all (a collection failure), which is not the
    same as a fleet that burned nothing.  ``since`` drops the rows older than
    it: the watchdog only needs the last hours.
    """
    rows, skipped, files = [], 0, []
    for candidate in broker_log_files(path):
        try:
            fh = open(candidate, encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            continue
        files.append(candidate)
        with fh:
            for raw in fh:
                if not raw.strip():
                    continue
                row = broker_row(raw)
                if row is None:
                    skipped += 1
                    continue
                if since is not None and row["ts"] < since:
                    continue
                rows.append(row)
    rows.sort(key=lambda row: row["ts"])
    return rows, skipped, files


def broker_socket_for_log(path):
    """The broker socket of a request log that follows the broker naming.

    The broker writes ``<socket>.requests.jsonl`` next to its socket
    (``openswarm/bin/pi-minimax-broker.mjs``), so the socket of such a log is
    known: it tells whether the collector behind the log still runs.
    """
    if not path or not path.endswith(BROKER_LOG_SUFFIX):
        return None
    return path[: -len(BROKER_LOG_SUFFIX)] or None


def broker_socket_state(path, timeout=1.0):
    """(state, detail): the state of the MiniMax broker socket.

    ``alive``     the broker answers on its socket: it still collects.
    ``stopped``   the socket is gone or refuses the connection: the broker
                  stopped and its (readable) log is history, not a reading of
                  this hour.
    ``unknown``   the socket cannot be verified (a path that is not a socket,
                  a timeout).
    """
    if not path:
        return "unknown", "no broker socket is known"
    if not os.path.exists(path):
        return "stopped", "the broker socket is gone"
    try:
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            return "unknown", "the broker socket path is not a socket"
    except OSError as exc:
        return "unknown", f"the broker socket cannot be inspected ({exc.strerror or exc})"
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(path)
    except (FileNotFoundError, ConnectionRefusedError):
        return "stopped", "the broker does not listen on its socket any more"
    except OSError as exc:
        return "unknown", f"the broker socket cannot be reached ({exc.strerror or exc})"
    finally:
        client.close()
    return "alive", "the broker socket answers"


def collector_state(log_path, socket_path=None, socket_known=False):
    """The state of the MiniMax collector behind the request log.

    ``alive``       the collector answers on its socket: a quiet hour is a
                    real zero burn.
    ``stopped``     the collector is gone: the readable log is history, not a
                    reading of this hour.
    ``unwritable``  the collector cannot write its log: nothing is collected.
    ``gone``        the log is not there: nothing is collected.
    ``unknown``     the collector cannot be verified (an explicitly configured
                    log without a known socket): the log is read as it is.

    The log is checked before the socket: a collector that answers but cannot
    write its log collects nothing, and the readable log of an hour ago would
    look like a zero burn (Refs #1671).
    """
    if log_path and not os.path.exists(log_path):
        return {"collector_state": "gone", "collector_detail": "the request log is gone"}
    if log_path and not os.access(log_path, os.W_OK):
        return {"collector_state": "unwritable",
                "collector_detail": "the request log is not writable"}
    if socket_path and (socket_known or os.path.exists(socket_path)):
        state, detail = broker_socket_state(socket_path)
        return {"collector_state": state, "collector_detail": detail}
    return {"collector_state": "unknown",
            "collector_detail": "the collector cannot be verified"}


def request_row_gaps(rows):
    """The request rows that are missing before each row of the read log.

    The broker numbers every request of a run (``id``) and writes exactly one
    row per request, so a hole in the numbering is a request whose row never
    reached the log: a rotation that overwrote an older generation or an
    append that failed.  A numbering that starts again is a broker restart,
    not a hole.
    """
    gaps = []
    previous = None
    for row in rows:
        rid = row.get("rid")
        if rid is None:
            gaps.append(0)
            continue
        if previous is None:
            gap = max(0, rid - 1)      # the rows before the first one we have
        elif rid > previous:
            gap = rid - previous - 1
        else:
            gap = 0                    # the broker restarted: the counter starts over
        gaps.append(gap)
        previous = rid
    return gaps


def log_numbering_known(rows):
    """True when every row carries the request number of the broker."""
    return all(row.get("rid") is not None for row in rows)


def missing_request_rows(rows, gaps, start, end):
    """The request rows the log lost inside the window (start, end]."""
    missing = 0
    for row, gap in zip(rows, gaps):
        if start < row["ts"] <= end:
            missing += gap
    return missing


def hourly_buckets(rows, end, hours=6, tz=CONSOLE_TZ):
    """The per-hour token sums an operator compares with the MiniMax console.

    The buckets are the clock hours of the console (Beijing time), so the
    figure of a bucket is the figure the console shows for that hour.
    """
    last = end.astimezone(tz)
    first = (last - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    buckets = {}
    for row in rows:
        hour = row["ts"].astimezone(tz).replace(minute=0, second=0, microsecond=0)
        if hour < first or hour > last:
            continue
        entry = buckets.setdefault(hour, {"hour": hour.isoformat(timespec="seconds"),
                                          "tokens": 0, "requests": 0})
        entry["tokens"] += row["tokens"]
        entry["requests"] += 1
    return [buckets[key] for key in sorted(buckets)]


def token_reading(rows, now, resets_at=None, low_tokens=DEFAULT_LOW_TOKENS,
                  interval_hours=TOKEN_PLAN_INTERVAL_H, collected=True, collector=None):
    """The real MiniMax token reading of the provider request log.

    The hour is the last 60 minutes before ``now`` -- the hour the MiniMax
    console shows.  A log that could be read but holds no request of that hour
    is a *genuine* zero burn only when the collector is verified: the broker
    socket answers, or the log itself carries a request of the hour (then the
    collector demonstrably wrote inside the hour).  Then the reading is 0
    tokens and the watchdog alerts (that is the burn collapse of #1671).  A
    log that could not be read at all, a collector that is gone, a collector
    that cannot write its log and a log that lost rows are *collection
    failures*: nothing is known about the burn, so there is no reading and no
    alert.  A quiet log of a collector that cannot be verified is *unverified*:
    a fleet that burned nothing and a collection that is broken look the same,
    so the burn is unknown.  A broken collection is never reported as a fleet
    that burned nothing.

    ``token_sample_at`` is the timestamp of the newest request of the hour
    (the token sample itself) and ``token_observed_at`` the moment the
    reading was taken.  The status shows the reading while the observation is
    fresh, so a stopped watchdog cannot pass an old reading off as current.
    ``token_last_request_age_s`` says how long the broker has been silent: it
    tells a stopped collector apart from an idle fleet.

    The Token Plan window ends at ``resets_at`` (the ``end_time`` of the
    coding-plan remains payload); only the rows of that window are summed, so
    a reading never mixes the tokens of two intervals.
    """
    collector = collector or {}
    reading = {"hourly_tokens": None, "hourly_requests": 0, "token_state": "unavailable",
               "token_alert": False, "token_sample_at": None, "token_sample_age_s": None,
               "token_last_request_at": None, "token_last_request_age_s": None,
               "token_observed_at": now.isoformat(timespec="seconds"),
               "interval_tokens": None, "interval_requests": 0,
               "interval_start": None, "interval_end": None, "interval_reset_at": None,
               "hourly_buckets": [], "token_missing_rows": 0,
               "collector_state": collector.get("collector_state", "unknown"),
               "collector_detail": collector.get("collector_detail", ""),
               "collector_verified": False}
    if not collected:
        return reading  # the collection failed: the burn is unknown
    if reading["collector_state"] in ("stopped", "gone", "unwritable"):
        # The collector is not there: its readable log is history, not a
        # reading of this hour.
        reading["token_state"] = "collector_down"
        return reading
    gaps = request_row_gaps(rows)
    numbered = log_numbering_known(rows)
    start = now - timedelta(minutes=60)
    inside = [row for row in rows if start < row["ts"] <= now]
    tokens = sum(row["tokens"] for row in inside)
    reading["hourly_requests"] = len(inside)
    if inside:
        sample = max(row["ts"] for row in inside)
        reading["token_sample_at"] = sample.isoformat(timespec="seconds")
        reading["token_sample_age_s"] = int((now - sample).total_seconds())
    if rows:
        last = max(row["ts"] for row in rows)
        reading["token_last_request_at"] = last.isoformat(timespec="seconds")
        reading["token_last_request_age_s"] = int((now - last).total_seconds())
    # The reading is verified when the collector answers on its socket or
    # when the log carries a request of the hour: then the collector wrote
    # inside the hour and a quiet hour is a real zero burn.
    verified = bool(inside) or reading["collector_state"] == "alive"
    reading["collector_verified"] = verified
    missing = missing_request_rows(rows, gaps, start, now) if numbered else 0
    reading["token_missing_rows"] = missing
    if missing:
        # The log lost rows of the hour: the sum is not the figure of the
        # console, so it is not shown as one.
        reading["token_state"] = "incomplete"
        reading["token_partial_tokens"] = tokens
    elif not inside and not verified:
        # A quiet hour of a collector that cannot be verified: the burn is
        # unknown, not zero.
        reading["token_state"] = "unverified"
    else:
        reading.update(hourly_tokens=tokens, token_state="ok" if inside else "idle")
    end = resets_at if (resets_at is not None and resets_at > now) else now
    window_start = end - timedelta(hours=interval_hours)
    window = [row for row in rows if window_start <= row["ts"] <= end]
    interval_missing = missing_request_rows(rows, gaps, window_start, end) if numbered else 0
    reading.update(interval_tokens=sum(row["tokens"] for row in window),
                   interval_requests=len(window),
                   interval_start=window_start.isoformat(timespec="seconds"),
                   interval_end=end.isoformat(timespec="seconds"),
                   interval_reset_at=resets_at.isoformat(timespec="seconds") if resets_at else None)
    if interval_missing:
        reading["interval_tokens"] = None
        reading["interval_missing_rows"] = interval_missing
    reading["hourly_buckets"] = hourly_buckets(rows, now)
    if low_tokens is not None and tokens < low_tokens and not missing and verified:
        reading["token_alert"] = True
    return reading


def interval_reset_at(path):
    """The end of the current MiniMax Token Plan window of a coding-plan payload.

    The payload carries the window end as ``end_time`` (epoch milliseconds)
    inside ``model_remains`` -- the same field
    ``openswarm/lib/dispatch/quota.py`` reads to release a spent window.  A
    missing or unreadable payload yields ``None``: the reading then anchors
    its interval on the newest request.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    containers = [payload]
    for key in ("coding_plan", "data", "result"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        remains = container.get("model_remains")
        if not isinstance(remains, list):
            continue
        for entry in remains:
            if not isinstance(entry, dict):
                continue
            seconds = entry.get("end_time")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                continue
            if seconds <= 0:
                continue
            if seconds > _MS_EPOCH_THRESHOLD:
                seconds = seconds / 1000.0
            return datetime.fromtimestamp(seconds, timezone.utc)
    return None


def analyze(points, hours=6, now=None, stale_sec=STALE_SEC, anchor_sec=ANCHOR_SEC, tokens=None):
    """The weekly quota burn of the health history plus the token reading.

    The token alert does not depend on the quota history: a collapsed token
    burn alerts even when the history is missing, stale or too short, so the
    watchdog is not quiet at the start of the fleet or while the quota
    collection is broken.
    """
    now = now or datetime.now(timezone.utc)
    result = {"points": len(points), "newest_ts": None, "sample_age_s": None,
              "status": "insufficient", "quota_alert": False, "exhaustion_at": None}
    if points:
        newest = max(p["ts"] for p in points)
        result["newest_ts"] = newest.isoformat(timespec="seconds")
        result["sample_age_s"] = int((now - newest).total_seconds())
        if result["sample_age_s"] > stale_sec:
            result["status"] = "stale"
        else:
            cutoff = newest - timedelta(hours=hours)
            selected = sorted((p for p in points if cutoff <= p["ts"] <= newest), key=lambda p: p["ts"])
            anchors = [p for p in points
                       if cutoff - timedelta(seconds=anchor_sec) <= p["ts"] < cutoff]
            if anchors:
                selected.insert(0, max(anchors, key=lambda p: p["ts"]))
            result["points"] = len(selected)
            if len(selected) >= 2 and len({p["weekly_end"] for p in selected}) == 1:
                xs = [(p["ts"] - selected[0]["ts"]).total_seconds() / 3600 for p in selected]
                ys = [p["weekly_pct"] for p in selected]
                xbar, ybar = sum(xs) / len(xs), sum(ys) / len(ys)
                denom = sum((x - xbar) ** 2 for x in xs)
                if denom:
                    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom
                    rate = -slope
                    result.update(status="ok", burn_pct_per_hour=round(rate, 4),
                                  five_hour_pct=round(rate * 5, 4), window_hours=round(xs[-1], 4),
                                  weekly_end=selected[0]["weekly_end"].isoformat())
                    remaining = ys[-1]
                    if rate > 0:
                        exhausted = (newest + timedelta(hours=remaining / rate)).replace(microsecond=0)
                        if exhausted < selected[0]["weekly_end"]:
                            result["exhaustion_at"] = exhausted.isoformat()
                            result["quota_alert"] = True
    if tokens:
        result.update(tokens)
    # The watchdog alerts on the weekly exhaustion and on a collapsed token burn.
    result["alert"] = bool(result.get("quota_alert") or result.get("token_alert"))
    return result


def status_path(history, explicit=None):
    """The snapshot path: ``--status-file``, then ``MM_BURN_STATUS``, then the history directory."""
    if explicit:
        return explicit
    env = os.environ.get("MM_BURN_STATUS")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(history)), "minimax-burn.json")


def write_status(path, result):
    """Write the snapshot the lane status reads."""
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--history", required=True, help="JSONL history file")
    parser.add_argument("--append", metavar="HEALTH_JSON", help="append current health snapshot")
    parser.add_argument("--broker-log", metavar="FILE",
                        help="MiniMax provider request log (default: the log of the running broker)")
    parser.add_argument("--quota-json", metavar="FILE",
                        help="MiniMax coding-plan remains payload (the Token Plan window end)")
    parser.add_argument("--status-file", metavar="FILE",
                        help="JSON snapshot for the lane status (default: next to the history file)")
    parser.add_argument("--low-tokens", type=float, default=DEFAULT_LOW_TOKENS,
                        help="hourly token read below this is a burn collapse")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    # Without configuration the watchdog still reads the log of the running
    # broker: the reading exists in the normal setup (Refs #1671).
    configured_log = (args.broker_log or os.environ.get("MM_BROKER_LOG")
                      or os.environ.get("PI_BROKER_LOG"))
    broker_log = configured_log or default_broker_log()
    broker_socket = broker_socket_for_log(broker_log)
    quota_json = args.quota_json or os.environ.get("MM_QUOTA_JSON")
    now = datetime.now(timezone.utc)
    # A broken quota collection must not block the token reading: the burn of
    # #1671 is measured by the provider request log, not by the quota history.
    health_error = None
    if args.append:
        try:
            append_health(args.history, args.append)
        except (OSError, ValueError, TypeError) as exc:
            health_error = f"the health snapshot was not appended: {exc}"
            print(f"warning: {health_error}", file=sys.stderr)
    try:
        points, skipped = read_history(args.history)
    except OSError as exc:
        points, skipped = [], 0
        health_error = health_error or f"the quota history is unreadable: {exc}"
        print(f"warning: {health_error}", file=sys.stderr)
    try:
        rows, broker_skipped, broker_files = read_broker_log(
            broker_log, since=now - timedelta(hours=READ_WINDOW_H))
        collector = collector_state(broker_log, broker_socket,
                                    socket_known=not configured_log)
        tokens = token_reading(rows, now, resets_at=interval_reset_at(quota_json),
                               low_tokens=args.low_tokens, collected=bool(broker_files),
                               collector=collector)
        result = analyze(points, now=now, tokens=tokens)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result["skipped"] = skipped
    result["broker_skipped"] = broker_skipped
    result["broker_log"] = broker_log
    result["broker_socket"] = broker_socket
    if health_error:
        result["health_error"] = health_error
    result["written_at"] = now.isoformat(timespec="seconds")
    try:
        write_status(status_path(args.history, args.status_file), result)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif result["status"] == "insufficient":
        print(f"insufficient (points={result['points']}, skipped={skipped})")
    elif result["status"] == "stale":
        print(f"stale (newest_ts={result['newest_ts']}, sample_age_s={result['sample_age_s']}); "
              "no prediction")
    else:
        exhaustion = result["exhaustion_at"] or "after weekly_end / not predicted"
        print(f"burn={result['burn_pct_per_hour']:.4f}%/h five_hour={result['five_hour_pct']:.4f}% "
              f"window_hours={result['window_hours']:.2f} exhaustion={exhaustion} "
              f"alert={str(result['alert']).lower()}")
    if not args.json and result.get("hourly_tokens") is not None:
        print(f"minimax_hourly_tokens={result['hourly_tokens']} "
              f"token_alert={str(result['token_alert']).lower()} "
              f"token_state={result.get('token_state')} "
              f"interval_tokens={result.get('interval_tokens')} "
              f"token_sample_at={result.get('token_sample_at')}")
    elif not args.json and result.get("token_state") in ("unavailable", "collector_down",
                                                         "incomplete", "unverified"):
        # A collection failure is not a burn reading: it says so instead of
        # pretending the fleet burned nothing.
        detail = result.get("collector_detail") or result.get("broker_log")
        if result.get("token_state") == "incomplete":
            detail = f"{result.get('token_missing_rows')} request rows are missing from the log"
        elif result.get("token_state") == "unverified":
            detail = (f"the collector is not verified ({detail}) and the log holds no "
                      "request of the hour")
        print(f"minimax token reading unavailable: {result.get('token_state')} ({detail})")
    return 1 if result.get("alert") else (2 if health_error else 0)


if __name__ == "__main__":
    raise SystemExit(main())
