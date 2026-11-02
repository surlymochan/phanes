#!/usr/bin/env python3
"""Match box VM pauses to the lane runs they interrupted (Refs #1604).

``box-pauses.jsonl`` records the pause windows of the box VM; every lane run
directory holds a ``result.json`` (and usually a ``status.json``) with the
outcome of the run.  The tool lines the two up on the clock: a run belongs to a
pause when it started before the pause ended and was still running (or had not
started yet) five seconds before the pause began.

Usage:
  box-pause-impact.py --pauses box-pauses.jsonl --runs <run dir>
                      [--since ISO] [--json] [--self-test]

Pause records are ``{"from": ..., "to": ..., "tz": ...}`` with local time
stamps.  ``tz`` accepts "+08:00", "UTC+8", "Z"/"UTC" or a zone name
("Asia/Shanghai") and defaults to UTC+8; the ``started``/``resumed`` (epoch)
shape of the same file is accepted as well, so the real pause log is not
thrown away as a bad line.  Malformed lines and run directories without a
result.json are skipped and counted.
"""
import argparse
import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

RUN_ID_RE = re.compile(r"^(\d{8}-\d{6})")
ISSUE_RE = re.compile(r"#\d+")
PAUSE_TZ_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.I)
PAUSE_FROM_KEYS = ("from", "start", "started", "started_at")
PAUSE_TO_KEYS = ("to", "end", "ended", "resumed", "resumed_at")
LOCAL_TZ = timezone(timedelta(hours=8))
EDGE_S = 5
SUSPECT_CLASSES = ("exec_error", "stalled")


def parse_tz(value):
    """Return the timezone of a pause record; the default is UTC+8.

    Accepted are "+08:00", "UTC+8", "Z"/"UTC" and zone names ("Asia/Shanghai").
    """
    if value is None:
        return LOCAL_TZ
    raw = str(value).strip()
    if not raw:
        return LOCAL_TZ
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
        raise ValueError(f"unknown pause timezone {value!r}") from exc


def parse_time(value, tz):
    """Return the instant of a stamp; a naive stamp is local to ``tz``."""
    if value is None or isinstance(value, bool):
        raise ValueError("missing timestamp")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz)
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def first_stamp(row, keys, tz):
    """Return the first usable stamp of ``keys``; raise ValueError when there is none."""
    for key in keys:
        if key not in row:
            continue
        try:
            return parse_time(row[key], tz)
        except (ValueError, TypeError):
            continue
    raise ValueError(f"no usable {keys[0]} stamp")


def load_pauses(path):
    """Return (pauses, bad_lines, missing_file) of a box pause log (JSONL)."""
    target = Path(path)
    if not target.is_file():
        return [], 0, 1
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], 0, 1
    pauses, bad = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        try:
            if not isinstance(row, dict):
                raise ValueError("pause record is not an object")
            tz = parse_tz(row.get("tz"))
            start = first_stamp(row, PAUSE_FROM_KEYS, tz)
            end = first_stamp(row, PAUSE_TO_KEYS, tz)
            if end < start:
                raise ValueError("pause ends before it starts")
        except (ValueError, TypeError, KeyError, AttributeError):
            bad += 1
            continue
        pauses.append({"from": start, "to": end})
    return pauses, bad, 0


def read_json(path):
    """Return the JSON object of ``path``; an unreadable file becomes {}."""
    try:
        with path.open(encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def as_seconds(value, default=0.0):
    """Return a duration in seconds; an unusable value becomes the default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def summary_text(result):
    """Return the summary of a result.json; the real file keeps it in "result"."""
    for holder in (result, result.get("result")):
        if not isinstance(holder, dict):
            continue
        summary = holder.get("summary")
        if isinstance(summary, dict):
            summary = summary.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
    return ""


def load_runs(directory):
    """Return (runs, bad, missing_result) of the run directories of ``directory``."""
    runs, bad, missing = [], 0, 0
    for folder in sorted(Path(directory).iterdir()):
        if not folder.is_dir():
            continue
        match = RUN_ID_RE.match(folder.name)
        if not match:
            bad += 1
            continue
        try:
            started = datetime.strptime(match[1], "%Y%m%d-%H%M%S").replace(tzinfo=LOCAL_TZ)
        except ValueError:
            bad += 1
            continue
        result_path = folder / "result.json"
        if not result_path.is_file():
            missing += 1
            continue
        result = read_json(result_path)
        if not result:
            bad += 1
            continue
        state = read_json(folder / "status.json")
        title = result.get("title") or result.get("task_title") or state.get("title") or ""
        error = str(result.get("error") or result.get("error_message") or summary_text(result) or "")
        first_line = error.splitlines()[0].strip() if error.splitlines() else ""
        runs.append({
            "id": folder.name,
            "start": started.isoformat(timespec="seconds"),
            "start_dt": started,
            "seconds": as_seconds(result.get("seconds",
                                            result.get("duration_seconds",
                                                       state.get("seconds",
                                                                 state.get("duration_seconds", 0))))),
            "lane": result.get("lane") or state.get("lane") or "-",
            "title": str(title),
            "status": result.get("status") or state.get("state") or "unknown",
            "error_class": result.get("error_class") or state.get("error_class") or "-",
            "error": first_line or "-",
        })
    return runs, bad, missing


def public_run(run):
    """Return the JSON-safe view of a run; the internal start stamp is dropped."""
    return {key: value for key, value in run.items() if key != "start_dt"}


def is_suspect(run):
    """True when the run looks like a failure caused by the pause."""
    return (str(run.get("error_class")) in SUSPECT_CLASSES
            or str(run.get("status")).lower() == "stalled")


def make_report(pauses, runs, since=None):
    """Return one report entry per pause, with the runs it interrupted."""
    if since:
        cutoff = parse_time(since, LOCAL_TZ)
        pauses = [pause for pause in pauses if pause["to"] >= cutoff]
    entries = []
    for pause in pauses:
        affected = [run for run in runs if run["start_dt"] < pause["to"] and
                    run["start_dt"] + timedelta(seconds=run["seconds"])
                    > pause["from"] - timedelta(seconds=EDGE_S)]
        suspected = [run for run in affected if is_suspect(run)]
        entries.append({"from": pause["from"].isoformat(timespec="seconds"),
                        "to": pause["to"].isoformat(timespec="seconds"),
                        "runs": [public_run(run) for run in affected],
                        "count": len(affected),
                        "exec_error_or_stalled": len(suspected),
                        "lanes": sorted({run["lane"] for run in affected}),
                        "suspected": [public_run(run) for run in suspected]})
    return entries


def suspected_runs(report):
    """Return the failed runs suspected of a pause hit, once per run."""
    hits, order = {}, []
    for item in report:
        for run in item["suspected"]:
            if run["id"] not in hits:
                hits[run["id"]] = dict(public_run(run), pause_hits=0)
                order.append(run["id"])
            hits[run["id"]]["pause_hits"] += 1
    return [hits[run_id] for run_id in order]


def render_text(report, bad_pauses, missing_pauses, bad_runs, missing):
    """Return the human readable report."""
    lines = [f"pauses={len(report)} bad_pause_lines={bad_pauses} "
             f"missing_pauses_file={missing_pauses} bad_runs={bad_runs} missing_result={missing}"]
    for item in report:
        lines.append(f"\n{item['from']} .. {item['to']}: runs={item['count']} "
                     f"exec_error/stalled={item['exec_error_or_stalled']} lanes={','.join(item['lanes']) or '-'}")
        for run in item["runs"]:
            issue = ISSUE_RE.search(run["title"])
            lines.append(f"  {run['id']} lane={run['lane']} issue={issue.group() if issue else '-'} "
                         f"status={run['status']} error_class={run['error_class']} error={run['error']}")
    lines.append("\n疑似被暂停误伤的失败：")
    suspects = suspected_runs(report)
    if not suspects:
        lines.append("  无")
    for run in suspects:
        lines.append(f"  {run['id']} lane={run['lane']} status={run['status']} "
                     f"error_class={run['error_class']} error={run['error']} "
                     f"pause_hits={run['pause_hits']}")
    return "\n".join(lines)


def self_test():
    """Check the pause matching, the summaries and the JSON report offline."""
    # The fixture lives beside this script, never in the repository root.
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
        root = Path(temp)
        pauses_path, runs_dir = root / "pauses.jsonl", root / "runs"
        runs_dir.mkdir()
        pauses_path.write_text("\n".join(json.dumps(p) for p in [
            {"from": "2026-11-02 07:40:00", "to": "2026-11-02 07:50:00", "tz": "+08:00"},
            {"from": "2026-11-02 08:00:00", "to": "2026-11-02 08:10:00", "tz": "+08:00"}]), encoding="utf-8")
        samples = [
            ("20261102-074500-a", {"status": "failed", "lane": "M6", "title": "Review #1563", "seconds": 600, "error_class": "exec_error", "error": "Anthropic stream ended before message_stop\nmore"}),
            ("20261102-075001-b", {"status": "stalled", "lane": "M7", "title": "#1572", "seconds": 650, "error_class": "stalled"}),
            ("20261102-075955-c", {"status": "failed", "lane": "M13", "title": "#1577", "seconds": 1, "error_class": "exec_error"}),
            ("20261102-080500-d", {"status": "done", "lane": "M2", "seconds": 60, "error_class": "none"}),
            ("20261102-081100-e", {"status": "done", "lane": "M3", "seconds": 10, "error_class": "none"}),
        ]
        for name, result in samples:
            folder = runs_dir / name
            folder.mkdir()
            (folder / "result.json").write_text(json.dumps(result), encoding="utf-8")
            (folder / "status.json").write_text(json.dumps({"lane": result["lane"]}), encoding="utf-8")
        (runs_dir / "20261102-074700-missing").mkdir()
        pauses, bad_pauses, missing_pauses = load_pauses(pauses_path)
        runs, bad_runs, missing = load_runs(runs_dir)
        report = make_report(pauses, runs)
        assert [x["count"] for x in report] == [1, 3], report
        assert [x["lanes"] for x in report] == [["M6"], ["M13", "M2", "M7"]], report
        assert [x["exec_error_or_stalled"] for x in report] == [1, 2], report
        assert bad_pauses == bad_runs == 0 and missing == 1 and missing_pauses == 0
        # The report is JSON-safe: no datetime survives into the payload.
        payload = {"pauses": report, "suspected": suspected_runs(report),
                   "bad_pause_lines": bad_pauses, "missing_pauses_file": missing_pauses,
                   "bad_runs": bad_runs, "missing_result": missing}
        decoded = json.loads(json.dumps(payload, ensure_ascii=False, indent=2))
        assert decoded["pauses"][0]["count"] == 1, decoded
        assert decoded["pauses"][0]["runs"][0]["start"] == "2026-11-02T07:45:00+08:00", decoded
        assert [run["id"] for run in decoded["suspected"]] == \
            ["20261102-074500-a", "20261102-075001-b", "20261102-075955-c"], decoded

        # The timezone of a pause record: "+08:00", "UTC+8", "Z" and a zone name.
        noon = datetime(2026, 11, 2, 12, 0)
        assert parse_tz("+08:00").utcoffset(noon) == timedelta(hours=8)
        assert parse_tz("UTC+8").utcoffset(noon) == timedelta(hours=8)
        assert parse_tz(None).utcoffset(noon) == timedelta(hours=8)
        assert parse_tz("").utcoffset(noon) == timedelta(hours=8)
        assert parse_tz("Z").utcoffset(noon) == timedelta(0)
        try:
            zone = parse_tz("Asia/Shanghai")
        except ValueError:
            zone = None  # no zone database on this host
        if zone is not None:
            assert zone.utcoffset(noon) == timedelta(hours=8)

        # The "started"/"resumed" (epoch) shape of the same file is not a bad line.
        epoch_path = root / "epoch.jsonl"
        epoch_path.write_text(json.dumps({
            "started": datetime(2026, 11, 2, 7, 40, tzinfo=LOCAL_TZ).timestamp(),
            "resumed": datetime(2026, 11, 2, 7, 50, tzinfo=LOCAL_TZ).timestamp()}), encoding="utf-8")
        epoch_pauses, epoch_bad, epoch_missing = load_pauses(epoch_path)
        assert epoch_bad == 0 and epoch_missing == 0, (epoch_bad, epoch_missing)
        assert len(epoch_pauses) == 1, epoch_pauses
        assert epoch_pauses[0]["from"].isoformat(timespec="seconds") == "2026-11-02T07:40:00+08:00"

        # A missing pause file is counted apart from a malformed line.
        missing_pauses, bad_pauses, missing_file = load_pauses(root / "nope.jsonl")
        assert missing_pauses == [] and bad_pauses == 0 and missing_file == 1

        # A bad pause line is skipped, not fatal.
        bad_path = root / "bad.jsonl"
        bad_path.write_text("\n".join(["not-json", json.dumps({"from": "2026-11-02 07:40:00"})]),
                            encoding="utf-8")
        bad_only, bad_lines, _ = load_pauses(bad_path)
        assert bad_only == [] and bad_lines == 2, (bad_only, bad_lines)
    print("box pause impact self-test: 2 pauses, overlap/boundary, summaries, missing result, "
          "JSON report, timezones and epoch pauses passed")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Match box pauses to the affected lane runs.")
    parser.add_argument("--pauses", help="box pause log (JSONL)")
    parser.add_argument("--runs", help="directory with one subdirectory per run")
    parser.add_argument("--since", help="ISO timestamp; naive values use UTC+8")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if not args.pauses or not args.runs:
        parser.error("--pauses and --runs are required")
    if not Path(args.runs).is_dir():
        parser.error(f"--runs directory not found: {args.runs}")
    if args.since:
        try:
            parse_time(args.since, LOCAL_TZ)
        except ValueError as exc:
            parser.error(f"invalid --since: {exc}")
    pauses, bad_pauses, missing_pauses = load_pauses(args.pauses)
    runs, bad_runs, missing = load_runs(args.runs)
    report = make_report(pauses, runs, args.since)
    if args.json:
        payload = {"pauses": report, "suspected": suspected_runs(report),
                   "bad_pause_lines": bad_pauses, "missing_pauses_file": missing_pauses,
                   "bad_runs": bad_runs, "missing_result": missing}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(report, bad_pauses, missing_pauses, bad_runs, missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
