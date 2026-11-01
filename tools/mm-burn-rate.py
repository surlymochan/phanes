#!/usr/bin/env python3
"""Estimate MiniMax weekly quota burn from health.json history."""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone


def parse_ts(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def read_history(path):
    points = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                row = json.loads(raw)
                ts = parse_ts(row.get("ts"))
                mm = row.get("minimax", row)
                pct = float(mm["weekly_pct"])
                if not math.isfinite(pct) or not 0 <= pct <= 100:
                    continue
                end = parse_ts(mm["weekly_end"])
                points.append({"ts": ts, "weekly_pct": pct, "weekly_end": end})
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
    return points


def append_health(history, health_path):
    with open(health_path, encoding="utf-8") as fh:
        health = json.load(fh)
    mm = health.get("minimax")
    if not isinstance(mm, dict):
        raise ValueError("health.json has no minimax object")
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    row = {"ts": now.isoformat(), "minimax": mm}
    try:
        existing = read_history(history)
    except FileNotFoundError:
        existing = []
    if any(p["ts"].astimezone(now.tzinfo).replace(second=0, microsecond=0) == now
           for p in existing):
        return False
    with open(history, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    return True


def analyze(points, hours=6):
    if not points:
        return {"status": "insufficient", "points": 0}
    newest = max(p["ts"] for p in points)
    cutoff = newest - timedelta(hours=hours)
    selected = sorted((p for p in points if cutoff <= p["ts"] <= newest),
                      key=lambda p: p["ts"])
    # Keep the nearest older sample as an anchor when snapshots straddle the cutoff.
    older = [p for p in points if p["ts"] < cutoff]
    if older:
        selected.insert(0, max(older, key=lambda p: p["ts"]))
    if len(selected) < 2 or len({p["weekly_end"] for p in selected}) != 1:
        return {"status": "insufficient", "points": len(selected)}
    xs = [(p["ts"] - selected[0]["ts"]).total_seconds() / 3600 for p in selected]
    ys = [p["weekly_pct"] for p in selected]
    xbar, ybar = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - xbar) ** 2 for x in xs)
    if not denom:
        return {"status": "insufficient", "points": len(selected)}
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom
    rate = -slope
    result = {"status": "ok", "points": len(selected), "burn_pct_per_hour": round(rate, 4),
              "five_hour_pct": round(rate * 5, 4), "weekly_end": selected[0]["weekly_end"].isoformat(),
              "exhaustion_at": None, "alert": False}
    remaining = ys[-1]
    if rate > 0:
        exhausted = newest + timedelta(hours=remaining / rate)
        if exhausted < selected[0]["weekly_end"]:
            result["exhaustion_at"] = exhausted.isoformat()
            result["alert"] = True
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True, help="JSONL history file")
    parser.add_argument("--append", metavar="HEALTH_JSON", help="append current health snapshot")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.append:
            append_health(args.history, args.append)
        result = analyze(read_history(args.history))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif result["status"] == "insufficient":
        print(f"insufficient (points={result['points']})")
    else:
        exhaustion = result["exhaustion_at"] or "after weekly_end / not predicted"
        print(f"burn={result['burn_pct_per_hour']:.4f}%/h five_hour={result['five_hour_pct']:.4f}% "
              f"exhaustion={exhaustion} alert={str(result['alert']).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
