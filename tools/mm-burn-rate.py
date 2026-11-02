#!/usr/bin/env python3
"""Estimate MiniMax weekly quota burn from health.json history (Refs #1561).

Usage:
  python3 tools/mm-burn-rate.py --history FILE.jsonl [--append health.json] [--json]
"""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone

# Matches the box hotfix HEALTH_QUOTA_FRESH_SEC=900 used for other quota signals.
STALE_SEC = 900
# Anchor window: the nearest sample at most this long before the 6h cutoff is used.
ANCHOR_SEC = 900


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--history", required=True, help="JSONL history file")
    parser.add_argument("--append", metavar="HEALTH_JSON", help="append current health snapshot")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.append:
            append_health(args.history, args.append)
        points, skipped = read_history(args.history)
        result = analyze(points)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result["skipped"] = skipped
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
