#!/usr/bin/env python3
"""Summarize broker request logs (JSONL) by minute or lane (Refs #1557, #1608).

Each line is a JSON object: {"ts","id","lane","status","err","ms","in_tok","out_tok"}.
Malformed lines are skipped and counted.

Usage:
  broker-log-summary.py LOG... [--since ISO|HH:MM] [--tz +08:00]
                        [--by minute|lane] [--window MIN] [--json]
                        [--slow MS] [--pauses box-pauses.jsonl] [--self-test]

Verdict is computed over the last --window minutes, relative to the newest record:
  quota_or_rate_limit   non-200 >= 90% and 429 is the majority of non-200
  upstream_unavailable  non-200 >= 90% and 5xx/timeout is the majority of non-200
  ok                    otherwise

--slow MS lists the requests that took at least MS milliseconds or answered
non-200: start (ts - ms), end, lane, status, the first 80 characters of the
error and the duration.  --pauses box-pauses.jsonl marks every listed request
with the pause window it falls into ("during_pause=<from→to>") and counts the
failed requests that happened during a pause.  Broker "ts" is UTC; the pause
stamps are local time of the "tz" field of the pause record (default UTC+8).

The JSON report keeps the old fields and adds "slow_ms", "slow" (the listed
slow requests) and "pause_overlaps" ({"pauses", "requests", "failed",
"windows"}); the text report only grows the new sections when the new flags
are used.
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone

NON_OK_RATIO = 0.9
CLASSES = ("200", "429", "upstream", "other")
TIMEOUT_RE = re.compile(r"timeout|timed out", re.I)
TZ_RE = re.compile(r"^[+-]\d{2}:\d{2}$")
HHMM_RE = re.compile(r"\d{2}:\d{2}")
PAUSE_TZ_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.I)
PAUSE_FROM_KEYS = ("from", "start", "started", "started_at")
PAUSE_TO_KEYS = ("to", "end", "ended", "resumed", "resumed_at")
DEFAULT_PAUSE_TZ = timezone(timedelta(hours=8))
ERR_CHARS = 80


def parse_tz(text):
    if text == "Z":
        return timezone.utc
    if not TZ_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"expected +HH:MM or Z, got {text!r}")
    hours, minutes = int(text[1:3]), int(text[4:6])
    if hours > 23 or minutes > 59:
        raise argparse.ArgumentTypeError(f"timezone offset out of range: {text!r}")
    sign = -1 if text[0] == "-" else 1
    offset = timedelta(hours=hours, minutes=minutes)
    return timezone(sign * offset)


def parse_ts(text, tz=timezone.utc):
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def classify(status, err):
    if status == 200:
        return "200"
    if status == 429:
        return "429"
    if 500 <= status <= 599 or TIMEOUT_RE.search(err):
        return "upstream"
    return "other"


def parse_line(line):
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    status, ts = obj.get("status"), obj.get("ts")
    if type(status) is not int or not isinstance(ts, str):
        return None
    try:
        when = parse_ts(ts)
    except ValueError:
        return None
    lane = obj.get("lane") if isinstance(obj.get("lane"), str) else "-"
    rid = obj.get("id") if isinstance(obj.get("id"), str) else None
    err = obj.get("err") if isinstance(obj.get("err"), str) else ""
    ms = obj.get("ms")
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        ms = None
    return {"ts": when, "lane": lane, "status": status, "cls": classify(status, err),
            "id": rid, "ms": ms, "err": err}


def load_records(paths):
    records, bad = [], 0
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                rec = parse_line(line)
                if rec is None:
                    bad += 1
                else:
                    records.append(rec)
    return records, bad


def parse_pause_tz(text):
    """Return the timezone of a pause record; the default is UTC+8.

    Accepted are "+08:00", "UTC+8", "Z"/"UTC" and zone names ("Asia/Shanghai").
    """
    if text is None:
        return DEFAULT_PAUSE_TZ
    raw = str(text).strip()
    if not raw:
        return DEFAULT_PAUSE_TZ
    if raw.upper() in ("Z", "UTC", "GMT"):
        return timezone.utc
    match = PAUSE_TZ_RE.match(raw)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        offset = timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))
        return timezone(sign * offset)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(raw)
    except Exception as exc:  # a bad zone name is a bad record, not a crash
        raise ValueError(f"unknown pause timezone {text!r}") from exc


def parse_pause_dt(value, tz):
    """Return the instant of a pause stamp; a naive stamp is local to ``tz``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def parse_pause_record(obj):
    """Return one pause window, or None when the record is unusable.

    A window is ``{"start": datetime, "end": datetime|None, "tz": timezone}``;
    ``end`` is None for a pause that is still open.
    """
    if not isinstance(obj, dict):
        return None
    try:
        tz = parse_pause_tz(obj.get("tz"))
    except ValueError:
        return None
    start = None
    for key in PAUSE_FROM_KEYS:
        start = parse_pause_dt(obj.get(key), tz)
        if start is not None:
            break
    if start is None:
        return None
    end = None
    for key in PAUSE_TO_KEYS:
        end = parse_pause_dt(obj.get(key), tz)
        if end is not None:
            break
    if end is not None and end < start:
        return None
    return {"start": start, "end": end, "tz": tz}


def parse_pause_lines(lines):
    """Return the pause windows of a JSONL pause log; bad lines are skipped."""
    pauses = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        pause = parse_pause_record(obj)
        if pause is not None:
            pauses.append(pause)
    return pauses


def load_pauses(path):
    """Read the pause windows of a box pause log (JSONL)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_pause_lines(fh)


def fmt_dt(dt, tz):
    """Return an ISO stamp of ``dt`` in ``tz`` with second resolution."""
    return dt.astimezone(tz).isoformat(timespec="seconds")


def fmt_ms(ms):
    """Return the duration of a request for the report."""
    if ms is None:
        return "-"
    if isinstance(ms, float) and ms.is_integer():
        return str(int(ms))
    return str(ms)


def request_span(record):
    """Return the (start, end) instants of a request; the start is ts - ms."""
    end = record["ts"]
    ms = record.get("ms")
    return (end - timedelta(milliseconds=ms) if ms else end), end


def slow_requests(records, threshold_ms):
    """Return the requests that took at least ``threshold_ms`` or answered non-200."""
    picked = []
    for record in records:
        ms = record.get("ms")
        if (ms is not None and ms >= threshold_ms) or record["status"] != 200:
            picked.append(record)
    return sorted(picked, key=lambda record: record["ts"])


def overlap_seconds(pause, start, end):
    """Return the seconds a request span and a pause window overlap."""
    stop = pause["end"]
    low = max(start, pause["start"])
    high = min(end, stop) if stop is not None else end
    return (high - low).total_seconds()


def find_pause_overlap(start, end, pauses):
    """Return the pause window a request overlaps (the longest one), or None."""
    best, best_overlap = None, 0.0
    for pause in pauses:
        overlap = overlap_seconds(pause, start, end)
        if overlap > best_overlap:
            best, best_overlap = pause, overlap
    return best


def pause_window_text(pause):
    """Return the "from→to" label of a pause window in the time of the pause."""
    tz = pause["tz"]
    start = fmt_dt(pause["start"], tz)
    end = fmt_dt(pause["end"], tz) if pause["end"] else "open"
    return f"{start}→{end}"


def slow_entries(records, threshold_ms, pauses, tz):
    """Return the report rows of the slow requests."""
    rows = []
    for record in slow_requests(records, threshold_ms):
        start, end = request_span(record)
        pause = find_pause_overlap(start, end, pauses)
        err = record.get("err") or ""
        rows.append({
            "id": record.get("id"),
            "start": fmt_dt(start, tz),
            "end": fmt_dt(end, tz),
            "lane": record["lane"],
            "status": record["status"],
            "err": err[:ERR_CHARS],
            "err_truncated": len(err) > ERR_CHARS,
            "ms": record.get("ms"),
            "during_pause": pause_window_text(pause) if pause else None,
        })
    return rows


def pause_overlap_summary(records, pauses):
    """Return how many requests fell into a pause window and how many failed.

    The windows keep the local time of the pause file; the request stamps of
    the report use the output timezone.
    """
    windows = []
    seen = set()
    total, failed_total = 0, 0
    for pause in pauses:
        hits, failed = 0, 0
        for index, record in enumerate(records):
            start, end = request_span(record)
            if overlap_seconds(pause, start, end) <= 0:
                continue
            hits += 1
            if record["status"] != 200:
                failed += 1
            if index not in seen:
                seen.add(index)
                total += 1
                if record["status"] != 200:
                    failed_total += 1
        windows.append({
            "from": fmt_dt(pause["start"], pause["tz"]),
            "to": fmt_dt(pause["end"], pause["tz"]) if pause["end"] else None,
            "tz": fmt_dt(pause["start"], pause["tz"])[-6:],
            "requests": hits,
            "failed": failed,
        })
    return {"pauses": len(pauses), "requests": total, "failed": failed_total,
            "windows": windows}


def parse_since(text, tz, records):
    if HHMM_RE.fullmatch(text):
        if not records:
            return None
        day = max(r["ts"] for r in records).astimezone(tz).date()
        hh, mm = (int(part) for part in text.split(":"))
        return datetime.combine(day, time(hh, mm), tzinfo=tz)
    return parse_ts(text, tz)


def longest_429_run(recs):
    """recs must be sorted by ts. Returns (count, start, end)."""
    best, cur, start = (0, None, None), 0, None
    for r in recs:
        if r["cls"] != "429":
            cur = 0
            continue
        if cur == 0:
            start = r["ts"]
        cur += 1
        if cur > best[0]:
            best = (cur, start, r["ts"])
    return best


def build_report(records, bad, tz, by, window, slow_ms=None, pauses=None):
    def fmt(dt):
        return fmt_dt(dt, tz)

    def summarize(key, recs):
        recs = sorted(recs, key=lambda r: r["ts"])
        counts = Counter(r["cls"] for r in recs)
        non_ok = [r for r in recs if r["cls"] != "200"]
        count, start, end = longest_429_run(recs)
        return {
            "key": key,
            "total": len(recs),
            "counts": {c: counts.get(c, 0) for c in CLASSES},
            "first_non200": fmt(non_ok[0]["ts"]) if non_ok else None,
            "last_non200": fmt(non_ok[-1]["ts"]) if non_ok else None,
            "longest_429_run": {
                "count": count,
                "start": fmt(start) if start else None,
                "end": fmt(end) if end else None,
            },
        }

    groups = defaultdict(list)
    for r in records:
        if by == "minute":
            key = r["ts"].astimezone(tz).strftime("%Y-%m-%d %H:%M")
        else:
            key = r["lane"]
        groups[key].append(r)

    verdict, win = verdict_for(records, window, fmt)
    report = {
        "records": len(records),
        "bad_lines": bad,
        "tz": datetime(2000, 1, 1, tzinfo=tz).isoformat()[-6:],
        "by": by,
        "overall": summarize("ALL", records),
        "groups": [summarize(k, groups[k]) for k in sorted(groups)],
        "window": win,
        "verdict": verdict,
    }
    report["slow_ms"] = slow_ms
    report["slow"] = slow_entries(records, slow_ms, pauses or [], tz) if slow_ms is not None else []
    report["pause_overlaps"] = pause_overlap_summary(records, pauses) if pauses else None
    return report


def verdict_for(records, window, fmt):
    if not records:
        return "ok", None
    newest = max(r["ts"] for r in records)
    cutoff = newest - timedelta(minutes=window)
    win = [r for r in records if r["ts"] > cutoff]
    non_ok = [r for r in win if r["cls"] != "200"]
    info = {
        "minutes": window,
        "start": fmt(cutoff),
        "end": fmt(newest),
        "total": len(win),
        "non_200": len(non_ok),
    }
    if not win or len(non_ok) / len(win) < NON_OK_RATIO:
        return "ok", info
    n = len(non_ok)
    if sum(r["cls"] == "429" for r in non_ok) / n > 0.5:
        return "quota_or_rate_limit", info
    if sum(r["cls"] == "upstream" for r in non_ok) / n > 0.5:
        return "upstream_unavailable", info
    return "ok", info


def render_text(report):
    ov = report["overall"]
    lines = [
        f"records={report['records']} bad_lines_skipped={report['bad_lines']} "
        f"tz={report['tz']} by={report['by']}",
        f"overall: total={ov['total']} 200={ov['counts']['200']} 429={ov['counts']['429']} "
        f"upstream={ov['counts']['upstream']} other={ov['counts']['other']}",
        f"first_non200={ov['first_non200']} last_non200={ov['last_non200']}",
        f"longest_429_run={ov['longest_429_run']['count']} "
        f"({ov['longest_429_run']['start']} .. {ov['longest_429_run']['end']})",
        "",
        f"{'key':<20} {'total':>6} {'200':>5} {'429':>5} {'upstream':>8} {'other':>5}  "
        f"{'first_non200':<25} {'last_non200':<25} run429",
    ]
    for g in report["groups"]:
        c = g["counts"]
        lines.append(
            f"{g['key']:<20} {g['total']:>6} {c['200']:>5} {c['429']:>5} "
            f"{c['upstream']:>8} {c['other']:>5}  "
            f"{g['first_non200'] or '-':<25} {g['last_non200'] or '-':<25} "
            f"{g['longest_429_run']['count']}")
    w = report["window"]
    if w:
        lines.append("")
        lines.append(f"window {w['minutes']:g}min ({w['start']} .. {w['end']}): "
                     f"{w['total']} req, {w['non_200']} non-200")
    po = report.get("pause_overlaps")
    if po:
        lines.append("")
        lines.append(f"pauses: {po['pauses']} window(s); requests during pause={po['requests']} "
                     f"failed during pause={po['failed']}")
        for window in po["windows"]:
            lines.append(f"  pause {window['from']} .. {window['to'] or 'open'} ({window['tz']}): "
                         f"{window['requests']} req, {window['failed']} failed")
    if report.get("slow_ms") is not None:
        slow = report["slow"]
        lines.append("")
        lines.append(f"slow requests (ms>={report['slow_ms']} or non-200): {len(slow)}")
        lines.append(f"  {'start':<25} {'end':<25} {'lane':<8} {'status':>6} {'ms':>9}  err")
        for row in slow:
            mark = f" during_pause={row['during_pause']}" if row["during_pause"] else ""
            lines.append(f"  {row['start']:<25} {row['end']:<25} {row['lane']:<8} "
                         f"{row['status']:>6} {fmt_ms(row['ms']):>9}  {row['err']}{mark}")
    lines.append(f"VERDICT {report['verdict']}")
    return "\n".join(lines)


SELF_TEST_REQUESTS = [
    {"ts": "2026-11-01T23:49:45Z", "id": "r6", "lane": "M6", "status": 502,
     "err": "provider unavailable", "ms": 1091840, "in_tok": 0, "out_tok": 0},
    {"ts": "2026-11-01T23:49:47Z", "id": "r7", "lane": "M7", "status": 502,
     "err": "provider unavailable", "ms": 1100282, "in_tok": 0, "out_tok": 0},
    {"ts": "2026-11-01T23:49:48Z", "id": "r13", "lane": "M13", "status": 502,
     "err": "provider unavailable", "ms": 1093778, "in_tok": 0, "out_tok": 0},
    {"ts": "2026-11-01T23:50:00Z", "id": "r1", "lane": "M1", "status": 200,
     "err": "", "ms": 1200, "in_tok": 12, "out_tok": 34},
    {"ts": "2026-11-01T23:50:10Z", "id": "r2", "lane": "M2", "status": 429,
     "err": "quota exceeded", "ms": 900, "in_tok": 0, "out_tok": 0},
]
SELF_TEST_PAUSE = {"from": "2026-11-02T07:31:44", "to": "2026-11-02T07:49:46", "tz": "+08:00"}


def run_self_test():
    """Check the slow filter, the timezone conversion and the pause overlap."""
    records, bad = [], 0
    for line in [json.dumps(rec) for rec in SELF_TEST_REQUESTS] + ["not-json"]:
        rec = parse_line(line)
        if rec is None:
            bad += 1
        else:
            records.append(rec)
    assert bad == 1, bad
    assert len(records) == 5, len(records)

    # --slow picks the three long 502s and the fast 429, but not the 200.
    slow = slow_requests(records, 60000)
    assert [rec["lane"] for rec in slow] == ["M6", "M7", "M13", "M2"], slow

    pauses = parse_pause_lines([json.dumps(SELF_TEST_PAUSE)])
    assert len(pauses) == 1, pauses
    window = "2026-11-02T07:31:44+08:00→2026-11-02T07:49:46+08:00"
    assert pause_window_text(pauses[0]) == window, pause_window_text(pauses[0])

    rows = slow_entries(records, 60000, pauses, timezone.utc)
    assert [row["lane"] for row in rows] == ["M6", "M7", "M13", "M2"], rows
    assert rows[0]["start"] == "2026-11-01T23:31:33+00:00", rows[0]
    assert rows[0]["end"] == "2026-11-01T23:49:45+00:00", rows[0]
    assert rows[0]["ms"] == 1091840, rows[0]
    assert rows[0]["err"] == "provider unavailable", rows[0]
    assert all(row["during_pause"] == window for row in rows[:3]), rows
    assert rows[3]["during_pause"] is None, rows[3]

    # The report counts the requests that fell into the pause window.
    summary = pause_overlap_summary(records, pauses)
    assert summary["requests"] == 3, summary
    assert summary["failed"] == 3, summary
    assert summary["windows"][0]["from"] == "2026-11-02T07:31:44+08:00", summary

    # A pause file without a tz field is local time (UTC+8) ...
    default_tz = parse_pause_lines([json.dumps({"from": SELF_TEST_PAUSE["from"],
                                                "to": SELF_TEST_PAUSE["to"]})])[0]
    assert default_tz["tz"].utcoffset(None) == timedelta(hours=8), default_tz
    assert pause_window_text(default_tz) == window
    # ... while a UTC pause file does not cover the requests.
    utc_pause = parse_pause_lines([json.dumps(dict(SELF_TEST_PAUSE, tz="Z"))])[0]
    assert pause_window_text(utc_pause) == "2026-11-02T07:31:44+00:00→2026-11-02T07:49:46+00:00"
    start, end = request_span(records[0])
    assert find_pause_overlap(start, end, [utc_pause]) is None

    # The report keeps the old fields and gains the new ones.
    report = build_report(records, bad, timezone.utc, "minute", 10.0, 60000, pauses)
    assert report["verdict"] == "ok", report["verdict"]
    assert len(report["slow"]) == 4, report["slow"]
    assert report["pause_overlaps"]["failed"] == 3, report["pause_overlaps"]
    print("ok")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Summarize broker request logs.")
    p.add_argument("logs", nargs="*", metavar="LOG")
    p.add_argument("--since", help="ISO timestamp, or HH:MM on the newest record's local date")
    p.add_argument("--tz", type=parse_tz, default=timezone.utc,
                   help="output timezone offset, e.g. +08:00 (default: UTC)")
    p.add_argument("--by", choices=("minute", "lane"), default="minute")
    p.add_argument("--window", type=float, default=10.0,
                   help="verdict window in minutes, counted back from the newest record")
    p.add_argument("--slow", type=int, metavar="MS",
                   help="list the requests that took at least MS ms or answered non-200")
    p.add_argument("--pauses", metavar="PATH",
                   help="box pause log (JSONL); marks the requests that fell into a pause")
    p.add_argument("--self-test", action="store_true", help="run the built-in offline fixtures")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        return run_self_test()
    if not args.logs:
        p.error("at least one LOG is required")
    if args.slow is not None and args.slow < 0:
        p.error("--slow must not be negative")

    try:
        records, bad = load_records(args.logs)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.since:
        try:
            since = parse_since(args.since, args.tz, records)
        except ValueError as exc:
            p.error(f"invalid --since: {exc}")
        if since is not None:
            records = [r for r in records if r["ts"] >= since]
    pauses = []
    if args.pauses:
        try:
            pauses = load_pauses(args.pauses)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    report = build_report(records, bad, args.tz, args.by, args.window, args.slow, pauses)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
