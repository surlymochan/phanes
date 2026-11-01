#!/usr/bin/env python3
"""Summarize broker request logs (JSONL) by minute or lane (Refs #1557).

Each line is a JSON object: {"ts","id","lane","status","err","ms","in_tok","out_tok"}.
Malformed lines are skipped and counted.

Usage:
  broker-log-summary.py LOG... [--since ISO|HH:MM] [--tz +08:00]
                        [--by minute|lane] [--window MIN] [--json]

Verdict is computed over the last --window minutes, relative to the newest record:
  quota_or_rate_limit   non-200 >= 90% and 429 is the majority of non-200
  upstream_unavailable  non-200 >= 90% and 5xx/timeout is the majority of non-200
  ok                    otherwise
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


def parse_tz(text):
    if text == "Z":
        return timezone.utc
    if not TZ_RE.match(text):
        raise argparse.ArgumentTypeError(f"expected +HH:MM or Z, got {text!r}")
    sign = -1 if text[0] == "-" else 1
    offset = timedelta(hours=int(text[1:3]), minutes=int(text[4:6]))
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
    err = obj.get("err") if isinstance(obj.get("err"), str) else ""
    return {"ts": when, "lane": lane, "status": status, "cls": classify(status, err)}


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


def build_report(records, bad, tz, by, window):
    def fmt(dt):
        return dt.astimezone(tz).isoformat(timespec="seconds")

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
    return {
        "records": len(records),
        "bad_lines": bad,
        "tz": datetime(2000, 1, 1, tzinfo=tz).isoformat()[-6:],
        "by": by,
        "overall": summarize("ALL", records),
        "groups": [summarize(k, groups[k]) for k in sorted(groups)],
        "window": win,
        "verdict": verdict,
    }


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
    lines.append(f"VERDICT {report['verdict']}")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="Summarize broker request logs.")
    p.add_argument("logs", nargs="+", metavar="LOG")
    p.add_argument("--since", help="ISO timestamp, or HH:MM on the newest record's local date")
    p.add_argument("--tz", type=parse_tz, default=timezone.utc,
                   help="output timezone offset, e.g. +08:00 (default: UTC)")
    p.add_argument("--by", choices=("minute", "lane"), default="minute")
    p.add_argument("--window", type=float, default=10.0,
                   help="verdict window in minutes, counted back from the newest record")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

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

    report = build_report(records, bad, args.tz, args.by, args.window)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
