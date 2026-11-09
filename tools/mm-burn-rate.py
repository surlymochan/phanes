#!/usr/bin/env python3
"""Estimate MiniMax weekly quota burn from health.json history (Refs #1561).

The hourly burn of the MiniMax provider is measured from the real token counters of
the provider request log the broker writes (``<socket>.requests.jsonl``): the rows
carry the ``in_tok``/``out_tok`` counters the provider reported, so the reading can
be reconciled with the MiniMax console (Refs #1671).  The quota history stays what
it was: the *remaining* percentage of the coding plan.

The snapshot carries the clock hours of the console (``hourly_buckets``) and the
total of the current Token Plan window (``interval_tokens``, with the moment the
window resets), so an operator samples the console and compares one hour with it.

A fleet that burned nothing in the hour is a zero reading that alerts; a collector
that stopped is a collection failure and is never passed off as a zero burn.  The
broker writes a heartbeat next to its log, so a quiet hour of a live collector is
told apart from a collector that stopped.

Usage:
  python3 tools/mm-burn-rate.py --history FILE.jsonl [--append health.json]
                                [--broker-log FILE] [--status-file FILE] [--json]
"""
import argparse
import json
import math
import os
import socket
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

def _env_int(name, default):
    """Read an integer from the environment; a malformed value falls back."""
    try:
        return int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


# Matches the box hotfix HEALTH_QUOTA_FRESH_SEC=900 used for other quota signals.
STALE_SEC = 900
# Anchor window: the nearest sample at most this long before the 6h cutoff is used.
ANCHOR_SEC = 900
# The hourly burn window and the bound of the collector liveness (Refs #1671).
TOKEN_WINDOW_SEC = 3600
TOKEN_LIVE_SEC = 90
# A burn below this many tokens in the hour is a collapsed fleet (Refs #1671).
TOKEN_MIN_HOURLY = _env_int("MM_BURN_MIN_HOURLY_TOKENS", 1000)
# The broker writes a liveness file next to its request log, so a quiet hour of a
# live collector is a zero burn and not a broken collection (Refs #1671).
HEARTBEAT_SUFFIX = ".heartbeat"
# The MiniMax console counts in Beijing time: the hourly buckets use it, so a
# bucket is the figure the console shows for that clock hour (Refs #1671).
CONSOLE_TZ = timezone(timedelta(hours=8))
# The Token Plan counts inside a window that resets at its end.
TOKEN_PLAN_INTERVAL_H = 5
# The clock hours the snapshot carries: an operator compares one hour of the
# status with the hour of the MiniMax console.
BUCKET_HOURS = 6


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
    point = {"ts": ts, "weekly_pct": pct, "weekly_end": parse_ts(mm.get("weekly_end"))}
    # The end of the current Token Plan window (the coding-plan remains payload
    # carries it): the interval the console counts in, and the moment it resets.
    interval_end = mm.get("interval_end")
    if isinstance(interval_end, str) and interval_end.strip():
        try:
            point["interval_end"] = parse_ts(interval_end)
        except ValueError:
            pass
    return point


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


def analyze(points, hours=6, now=None, stale_sec=STALE_SEC, anchor_sec=ANCHOR_SEC):
    if not points:
        return {"status": "insufficient", "points": 0}
    now = now or datetime.now(timezone.utc)
    newest = max(p["ts"] for p in points)
    base = {"points": len(points), "newest_ts": newest.isoformat(timespec="seconds"),
            "sample_age_s": int((now - newest).total_seconds()), "alert": False}
    if base["sample_age_s"] > stale_sec:
        return dict(base, status="stale", exhaustion_at=None)
    cutoff = newest - timedelta(hours=hours)
    selected = sorted((p for p in points if cutoff <= p["ts"] <= newest), key=lambda p: p["ts"])
    anchors = [p for p in points
               if cutoff - timedelta(seconds=anchor_sec) <= p["ts"] < cutoff]
    if anchors:
        selected.insert(0, max(anchors, key=lambda p: p["ts"]))
    base["points"] = len(selected)
    if len(selected) < 2 or len({p["weekly_end"] for p in selected}) != 1:
        return dict(base, status="insufficient")
    xs = [(p["ts"] - selected[0]["ts"]).total_seconds() / 3600 for p in selected]
    ys = [p["weekly_pct"] for p in selected]
    xbar, ybar = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - xbar) ** 2 for x in xs)
    if not denom:
        return dict(base, status="insufficient")
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom
    rate = -slope
    result = dict(base, status="ok", burn_pct_per_hour=round(rate, 4),
                  five_hour_pct=round(rate * 5, 4), window_hours=round(xs[-1], 4),
                  weekly_end=selected[0]["weekly_end"].isoformat())
    remaining = ys[-1]
    if rate > 0:
        exhausted = (newest + timedelta(hours=remaining / rate)).replace(microsecond=0)
        if exhausted < selected[0]["weekly_end"]:
            result["exhaustion_at"] = exhausted.isoformat()
            result["alert"] = True
    result.setdefault("exhaustion_at", None)
    return result


# --- the hourly token reading of the provider (Refs #1671) ---------------------------------

def _home():
    """The home directory of the box, or None when the environment names none."""
    try:
        return Path.home()
    except RuntimeError:
        return None


def broker_socket():
    """The broker socket of the box, when the environment names one."""
    for name in ("OPENSWARM_PI_BROKER_SOCKET", "PI_BROKER_SOCKET"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def heartbeat_path(log_path):
    """The liveness file the broker writes next to its request log."""
    return Path(str(log_path) + HEARTBEAT_SUFFIX)


def _age_s(path, now):
    """The age of a file in seconds, or None when it cannot be read."""
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return None
    return (now - datetime.fromtimestamp(mtime, timezone.utc)).total_seconds()


def _socket_unreachable(path):
    """True when the broker socket is there but nobody answers it.

    A socket that is not there at all says nothing: the log may still be fresh.
    """
    try:
        info = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISSOCK(info.st_mode):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(path)
    except OSError:
        return True
    return False


def collector_state(log_path, socket_path=None, now=None, live_sec=TOKEN_LIVE_SEC):
    """The state of the collector behind the request log (Refs #1671).

    * ``alive``        - the collector wrote recently: its heartbeat is fresh, or
      a legacy log without a heartbeat was written within 90 seconds;
    * ``stopped``      - the heartbeat missed three 30-second updates: the collector is
      gone, so the last known burn must not be shown as the current one;
    * ``unreachable``  - the broker socket is there but nobody answers it: the
      log that is still readable says nothing about the burn any more.
    * ``broken``      - audit writes failed or the request log is missing; a
      fresh heartbeat alone cannot prove that the reporting window is complete.

    The heartbeat is what tells a quiet hour of a live collector (a fleet that
    burned nothing) from a collector that stopped: without it an hour without a
    request looks exactly like a broken collection.
    """
    now = now or datetime.now(timezone.utc)
    heartbeat = heartbeat_path(log_path)
    # Once a heartbeat exists it is authoritative: log writes cannot mask a
    # stopped timer. Legacy collectors without one use a short write deadline.
    age = _age_s(heartbeat if heartbeat.exists() else log_path, now)
    if age is None or not -60 <= age <= live_sec:
        return "stopped"
    if heartbeat.exists():
        try:
            content = heartbeat.read_text(encoding="utf-8").strip()
            if content.startswith("{"):
                health = json.loads(content)
                info = Path(log_path).stat()
                if (health.get("log_dev", info.st_dev) != info.st_dev
                        or health.get("log_ino", info.st_ino) != info.st_ino
                        or health.get("log_size", 0) > info.st_size):
                    return "broken"
                error_at = health.get("audit_error_at")
                if error_at and (now - parse_ts(error_at)).total_seconds() < BUCKET_HOURS * 3600:
                    return "broken"
            else:
                parse_ts(content)  # compatibility with timestamp-only heartbeats
        except (OSError, ValueError, TypeError, AttributeError):
            return "broken"
    try:
        if not stat.S_ISREG(Path(log_path).stat().st_mode):
            return "broken"
    except OSError:
        return "broken"
    if socket_path and _socket_unreachable(socket_path):
        return "unreachable"
    return "alive"


def broker_logs(explicit=None):
    """The provider request logs of the broker: the current one and its rotations.

    The broker writes ``<socket>.requests.jsonl`` next to its socket and rotates it
    by byte size (``.1``, ``.2``, ...).  Every generation is read: an hour of traffic
    must not be lost when the broker rotates twice inside the hour.
    """
    main = None
    if explicit:
        main = Path(explicit)
    else:
        candidates = []
        named = os.environ.get("PI_BROKER_LOG")
        if named:
            candidates.append(Path(named))
        socket = broker_socket()
        if socket:
            candidates.append(Path(str(socket) + ".requests.jsonl"))
        home = _home()
        if home is not None:
            candidates.append(home / "Library" / "Caches" / "swrm-pi-broker" / "broker.sock.requests.jsonl")
        candidates.append(Path("/run/swrm-pi/broker.sock.requests.jsonl"))
        for candidate in candidates:
            if candidate.exists() or heartbeat_path(candidate).exists():
                main = candidate
                break
        if main is None and candidates:
            main = candidates[0]
    if main is None:
        return []
    rotations = sorted((path for path in main.parent.glob(main.name + ".*")
                        if path.name[len(main.name) + 1:].isdigit()),
                       key=lambda path: int(path.name[len(main.name) + 1:]))
    return [main] + rotations


def _counter(row, names):
    """The first usable token counter of a row, or None."""
    for name in names:
        value = row.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value) or value < 0:
            continue
        return int(value)
    return None


def _row_tokens(row):
    """The tokens a provider request row burned."""
    incoming = _counter(row, ("in_tok", "input_tokens"))
    outgoing = _counter(row, ("out_tok", "output_tokens"))
    if incoming is None or outgoing is None:
        # HTTP errors can follow a forwarded request (including broker 502s).
        # Only explicit counters establish zero usage; missing usage is unknown.
        raise ValueError("provider usage missing")
    return incoming + outgoing


def read_token_rows(paths):
    """Return valid rows and error times (None means the time is unknown).

    A dated request without usage invalidates only windows containing it. An
    unreadable file or an undated corrupt line has to invalidate every window.
    """
    rows, errors = [], []
    for path in paths:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            # A discovered log may disappear after the liveness check or during
            # rotation. Missing records invalidate the window just like read errors.
            errors.append(None)
            continue
        with fh:
            for raw in fh:
                if not raw.strip():
                    continue
                when = None
                try:
                    row = json.loads(raw)
                    if not isinstance(row, dict):
                        raise ValueError("row is not an object")
                    when = parse_ts(row.get("ts"))
                    tokens = _row_tokens(row)
                except (ValueError, TypeError):
                    errors.append(when)
                    continue
                rows.append({"ts": when, "tokens": tokens})
    return rows, errors


def coverage_start(log_path, rows, errors):
    """The beginning of retained collection, independently of its liveness.

    New brokers persist it across restarts and reset it when the log is lost.
    Legacy logs can only establish coverage back to their oldest dated record;
    an empty legacy log with a fresh heartbeat proves no historical coverage.
    """
    try:
        health = json.loads(heartbeat_path(log_path).read_text(encoding="utf-8"))
        if isinstance(health, dict) and "coverage_start" in health:
            try:
                return parse_ts(health["coverage_start"])
            except (ValueError, TypeError):
                return None
    except (OSError, ValueError, TypeError):
        pass
    times = [row["ts"] for row in rows] + [ts for ts in errors if ts is not None]
    return min(times) if times else None


def window_complete(start, end, coverage, errors):
    return (coverage is not None and coverage <= start
            and not any(ts is None or start <= ts <= end for ts in errors))


def hourly_buckets(rows, now=None, hours=BUCKET_HOURS, coverage=None, errors=()):
    """The token totals of the last clock hours, in the timezone of the console.

    The MiniMax console shows the consumption per hour; the buckets are the
    same figures, so an operator samples the console and compares one bucket
    with it (Refs #1671).
    """
    now = now or datetime.now(timezone.utc)
    totals = {}
    for row in rows:
        hour = row["ts"].astimezone(CONSOLE_TZ).replace(minute=0, second=0, microsecond=0)
        bucket = totals.setdefault(hour, {"tokens": 0, "requests": 0})
        bucket["tokens"] += row["tokens"]
        bucket["requests"] += 1
    first = now.astimezone(CONSOLE_TZ).replace(minute=0, second=0, microsecond=0)
    buckets = []
    for index in range(hours - 1, -1, -1):
        hour = first - timedelta(hours=index)
        bucket = totals.get(hour, {"tokens": 0, "requests": 0})
        end = min(hour + timedelta(hours=1) - timedelta(microseconds=1), now)
        complete = window_complete(hour, end, coverage, errors)
        buckets.append({"hour": hour.isoformat(), "tokens": bucket["tokens"] if complete else None,
                        "requests": bucket["requests"] if complete else None, "complete": complete})
    return buckets


def interval_reading(rows, interval_end, interval_hours=TOKEN_PLAN_INTERVAL_H,
                     now=None, coverage=None, errors=()):
    """The token total of the current Token Plan window (Refs #1671).

    The Token Plan counts inside a window that resets at its end; the total of
    that window is the figure the MiniMax console shows for the current plan.
    The rows of the window before it are not counted: after a reset the reading
    never mixes two intervals.
    """
    if interval_end is None:
        return {}
    start = interval_end - timedelta(hours=interval_hours)
    end = min(interval_end, now or datetime.now(timezone.utc))
    window = [row for row in rows if start <= row["ts"] <= end]
    complete = window_complete(start, end, coverage, errors)
    return {"interval_tokens": sum(row["tokens"] for row in window) if complete else None,
            "interval_requests": len(window) if complete else None, "interval_complete": complete,
            "interval_reset_at": interval_end.isoformat()}


def token_reading(logs, now=None, window_sec=TOKEN_WINDOW_SEC, live_sec=TOKEN_LIVE_SEC,
                  min_tokens=TOKEN_MIN_HOURLY, points=()):
    """The hourly token reading of the MiniMax provider request logs.

    The states are the ones an operator has to tell apart:

    * ``ok``    - the collector is alive and the fleet burned tokens in the hour;
    * ``idle``  - the collector is alive and the fleet burned nothing: a real zero
      burn, which is the collapse the watchdog has to alert on;
    * ``stale`` - the collector missed its liveness deadline: it stopped, so the
      last known burn must not be shown as the current one;
    * ``missing`` - there is no log at all: nothing is known about the burn.
    * ``incomplete`` - coverage is shorter than the window, or records are
      missing usage: any known subtotal is diagnostic and must never alert.
    """
    now = now or datetime.now(timezone.utc)
    result = {"token_state": "missing", "hourly_tokens": None, "hourly_requests": None,
              "token_alert": False, "token_sample_at": None, "token_last_request_at": None,
              "token_log": None, "token_log_age_s": None, "token_window_s": window_sec,
              "token_rows": 0, "token_skipped": 0, "token_collector": "unknown",
              "token_alert_threshold": min_tokens}
    if not logs:
        return result
    main = logs[0]
    result["token_log"] = str(main)
    try:
        age = int((now - datetime.fromtimestamp(main.stat().st_mtime, timezone.utc)).total_seconds())
    except (OSError, ValueError):
        age = None
    result["token_log_age_s"] = age
    # The collector behind the log decides whether the reading means anything: a
    # stopped collector is a collection failure, not a fleet that burned nothing.
    collector = collector_state(main, broker_socket(), now=now, live_sec=live_sec)
    result["token_collector"] = collector
    if collector != "alive":
        result["token_state"] = "stale"
        return result
    rows, errors = read_token_rows(logs)
    # Missing generations mean the window may have lost requests.
    generations = [int(path.name.rsplit(".", 1)[1]) for path in logs[1:]]
    if generations and generations != list(range(1, max(generations) + 1)):
        errors.append(None)
    result["token_skipped"] = len(errors)
    coverage = coverage_start(main, rows, errors)
    result["token_coverage_start"] = coverage.isoformat() if coverage else None
    if rows:
        result["token_last_request_at"] = max(row["ts"] for row in rows).isoformat()
    cutoff = now - timedelta(seconds=window_sec)
    window = [row for row in rows if cutoff <= row["ts"] <= now]
    complete = window_complete(cutoff, now, coverage, errors)
    result["token_window_complete"] = complete
    result["token_rows"] = len(window)
    if window:
        result["token_state"] = "ok"
        result["hourly_tokens"] = sum(row["tokens"] for row in window)
        result["hourly_requests"] = len(window)
        result["token_sample_at"] = max(row["ts"] for row in window).isoformat()
    elif complete:
        result["token_state"] = "idle"
        result["hourly_tokens"] = 0
        result["hourly_requests"] = 0
    if not complete:
        # Known requests remain a diagnostic subtotal, never a complete hour.
        result["token_state"] = "incomplete"
    result["token_alert"] = complete and result["hourly_tokens"] < min_tokens
    # The clock hours of the console and the current Token Plan window: the
    # figures an operator reconciles the reading with.
    result["hourly_buckets"] = hourly_buckets(
        [row for row in rows if now - timedelta(hours=BUCKET_HOURS) <= row["ts"] <= now],
        now=now, coverage=coverage, errors=errors)
    ends = [point["interval_end"] for point in points if point.get("interval_end")]
    result.update(interval_reading(rows, max(ends) if ends else None, now=now,
                                   coverage=coverage, errors=errors))
    return result


def default_status_file():
    """Where the burn snapshot is written when no path is given.

    The snapshot is the reading the lane status shows (Refs #1671), so it lives in
    the dispatcher state directory.
    """
    override = os.environ.get("MM_BURN_STATUS")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / "phanes-ctl" / "autodispatch" / "minimax-burn.json"


def write_status(path, payload, explicit=False):
    """Publish the burn snapshot the lane status reads (Refs #1671).

    The default snapshot belongs to the dispatcher state directory: a machine
    without a dispatcher does not get that directory created by a stray run.  A
    file that was named explicitly is always written.
    """
    path = Path(path)
    if not explicit and not path.parent.exists() and payload.get("token_collector") != "alive":
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--history", required=True, help="JSONL history file")
    parser.add_argument("--append", metavar="HEALTH_JSON", help="append current health snapshot")
    parser.add_argument("--broker-log", metavar="FILE",
                        help="the MiniMax provider request log (JSONL, default: next to the broker socket)")
    parser.add_argument("--status-file", metavar="FILE",
                        help="where the burn snapshot is written (default: the dispatcher state directory)")
    parser.add_argument("--min-hourly-tokens", type=int, default=TOKEN_MIN_HOURLY,
                        help="the hourly token count below which the burn counts as collapsed")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    failed = False
    if args.append:
        try:
            append_health(args.history, args.append)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # A broken health snapshot must not stop the token collection: the burn
            # reading is independent of the quota history (Refs #1671).
            print(f"error: {exc}", file=sys.stderr)
            failed = True
    try:
        points, skipped = read_history(args.history)
        result = analyze(points)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        failed = True
        points, skipped = [], 0
        result = analyze(points)
    result["skipped"] = skipped
    result.update(token_reading(broker_logs(args.broker_log), min_tokens=args.min_hourly_tokens,
                                points=points))
    result["alert"] = bool(result.get("alert") or result["token_alert"])
    result["token_observed_at"] = datetime.now(timezone.utc).isoformat()
    write_status(args.status_file or default_status_file(), result, explicit=bool(args.status_file))
    if args.json:
        # One JSON object, nothing else: the consumers parse the whole stdout.
        print(json.dumps(result, sort_keys=True))
    else:
        if result["status"] == "insufficient":
            print(f"insufficient (points={result['points']}, skipped={result['skipped']})")
        elif result["status"] == "stale":
            print(f"stale (newest_ts={result['newest_ts']}, sample_age_s={result['sample_age_s']}); "
                  "no prediction")
        else:
            exhaustion = result["exhaustion_at"] or "after weekly_end / not predicted"
            print(f"burn={result['burn_pct_per_hour']:.4f}%/h five_hour={result['five_hour_pct']:.4f}% "
                  f"window_hours={result['window_hours']:.2f} exhaustion={exhaustion} "
                  f"alert={str(result['alert']).lower()}")
        if result["token_state"] in ("ok", "idle"):
            print(f"hourly_tokens={result['hourly_tokens']} requests={result['hourly_requests']} "
                  f"token_state={result['token_state']} token_alert={str(result['token_alert']).lower()}")
    if result["token_alert"]:
        return 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
