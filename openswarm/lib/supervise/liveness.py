"""Lane liveness: a lane counts as silent only while the box was awake (Refs #1498).

The 05:12 incident: the box was frozen for 26 minutes, the MiniMax watchdog counted
that freeze as lane silence and killed the runs of M12 and M14 that were in fact
alive.  The box side now takes ``max(started, resume)``; this module is the generic
judgement a watcher (watchdog, autodispatch) can share.

A lane is silent only while the box was *awake*:

``silence_s(started_at, last_output_at, now, pauses)``
    The seconds between ``max(started_at, last_output_at)`` and ``now`` minus the
    parts of that window the box spent paused (the records of ``box-pauses.jsonl``,
    ``{"started": ..., "resumed": ...}``).

``verdict(lane, run, now, pauses, limit_s, resume_grace_s=300)``
    ``ok`` / ``stalled`` / ``grace``.  Within ``resume_grace_s`` after a resume the
    lane is in ``grace``: it is neither killed nor re-dispatched.  Only awake silence
    longer than ``limit_s`` is ``stalled``.

``load_pauses(path, since)``
    Read a pause log (JSONL), skipping malformed lines and keeping the records that
    reach into the period after ``since``.

The CLI judges a file of runs::

    python3 openswarm/lib/supervise/liveness.py check \\
        --runs runs.json --pauses box-pauses.jsonl --limit 1200 --json

``runs.json`` holds a JSON array of run records (or an object with a ``runs`` array);
every record carries a ``run_id``, a ``lane`` and the timestamps::

    [{"run_id": "20261102-053000-ab12", "lane": "M12",
      "started": 1793580000.0, "last_output_at": 1793580300.0}]

Standard library only, Python 3.9+.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

__all__ = [
    "evaluate",
    "load_pauses",
    "main",
    "silence_s",
    "verdict",
]

OK = "ok"
STALLED = "stalled"
GRACE = "grace"

# After a resume the lane gets this many seconds to produce output again.
DEFAULT_RESUME_GRACE_S = 300

_STARTED_KEYS = ("started", "started_at", "start")
_RESUMED_KEYS = ("resumed", "resume", "resumed_at")
_LAST_OUTPUT_KEYS = ("last_output_at", "last_output", "last_activity_at")


def _epoch(value):
    """Return a POSIX timestamp for a number or an ISO-8601 string, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _first_epoch(record, keys):
    """Return the first usable timestamp of ``keys`` in a mapping."""
    for key in keys:
        value = _epoch(record.get(key))
        if value is not None:
            return value
    return None


def _interval(record):
    """Return the ``(started, resumed)`` of a pause record, or None when unusable.

    ``resumed`` is None for a pause that is still open: it covers everything up to
    now.  A record whose resume precedes its start is broken and dropped.
    """
    if not isinstance(record, dict):
        return None
    started = _first_epoch(record, _STARTED_KEYS)
    if started is None:
        return None
    resumed = _first_epoch(record, _RESUMED_KEYS)
    if resumed is not None and resumed < started:
        return None
    return started, resumed


def _merged_pauses(pauses):
    """Return the pause intervals merged into a list of ``[started, ended]`` lists.

    Overlapping pauses are merged so that a frozen stretch is only counted once;
    an open pause (``ended`` None) swallows every later interval.
    """
    intervals = []
    for record in pauses or []:
        if isinstance(record, dict):
            pair = _interval(record)
        elif isinstance(record, (list, tuple)) and len(record) == 2:
            started = _epoch(record[0])
            ended = _epoch(record[1])
            pair = None if started is None or (ended is not None and ended < started) else (started, ended)
        else:
            pair = None
        if pair is None:
            continue
        intervals.append([pair[0], pair[1]])
    intervals.sort(key=lambda item: item[0])
    merged = []
    for started, ended in intervals:
        if merged:
            previous = merged[-1]
            previous_end = previous[1]
            if previous_end is None or started <= previous_end:
                if previous_end is not None and (ended is None or ended > previous_end):
                    previous[1] = ended
                continue
        merged.append([started, ended])
    return merged


def silence_s(started_at, last_output_at, now, pauses):
    """Return the seconds the lane was awake and silent.

    The window runs from ``max(started_at, last_output_at)`` to ``now``.  The
    stretches the box spent paused are not silence: the lane could not produce
    output then, so they are subtracted.
    """
    start = _epoch(started_at)
    if start is None:
        return 0.0
    last = _epoch(last_output_at)
    if last is not None and last > start:
        start = last
    end = _epoch(now)
    if end is None or end <= start:
        return 0.0
    paused = 0.0
    for pause_start, pause_end in _merged_pauses(pauses):
        stop = end if pause_end is None else min(pause_end, end)
        overlap = stop - max(pause_start, start)
        if overlap > 0:
            paused += overlap
    return max(0.0, (end - start) - paused)


def _lane_name(lane):
    """Return the name of a lane record or of a plain lane name."""
    if isinstance(lane, dict):
        return lane.get("name") or lane.get("lane") or lane.get("id")
    return lane


def _lane_pauses(pauses, lane_name):
    """Keep the pauses that apply to a lane.

    A record naming another lane does not apply; a record without a lane applies
    to every lane.
    """
    if not lane_name:
        return list(pauses or [])
    kept = []
    for record in pauses or []:
        if isinstance(record, dict):
            other = record.get("lane")
            if other is not None and str(other) != str(lane_name):
                continue
        kept.append(record)
    return kept


def _run_times(run):
    """Return the ``(started, last_output_at)`` of a run record."""
    if not isinstance(run, dict):
        return None, None
    return _first_epoch(run, _STARTED_KEYS), _first_epoch(run, _LAST_OUTPUT_KEYS)


def evaluate(lane, run, now, pauses, limit_s, resume_grace_s=DEFAULT_RESUME_GRACE_S):
    """Return the verdict of one run together with the numbers behind it."""
    lane_name = _lane_name(lane)
    scoped = _lane_pauses(pauses, lane_name)
    moment = _epoch(now)
    if moment is None:
        moment = time.time()
    started, last_output = _run_times(run)
    window = started
    if last_output is not None and (window is None or last_output > window):
        window = last_output
    silence = silence_s(started, last_output, moment, scoped)
    last_resume = None
    for _pause_start, pause_end in _merged_pauses(scoped):
        if pause_end is None:
            # The box is frozen right now: there is no resume to be graceful about.
            continue
        if window is not None and pause_end <= window:
            # The resume happened at or before the last output: the lane is alive.
            continue
        if last_resume is None or pause_end > last_resume:
            last_resume = pause_end
    resume_age = None if last_resume is None else moment - last_resume
    if resume_age is not None and resume_age <= resume_grace_s:
        verdict = GRACE
    elif limit_s is not None and silence > limit_s:
        verdict = STALLED
    else:
        verdict = OK
    return {
        "lane": lane_name,
        "run_id": (run or {}).get("run_id") or (run or {}).get("id"),
        "verdict": verdict,
        "silence_s": silence,
        "limit_s": limit_s,
        "started_at": started,
        "last_output_at": last_output,
        "last_resume_at": last_resume,
        "resume_age_s": resume_age,
        "resume_grace_s": resume_grace_s,
    }


def verdict(lane, run, now, pauses, limit_s, resume_grace_s=DEFAULT_RESUME_GRACE_S):
    """Return ``ok``, ``stalled`` or ``grace`` for one run."""
    return evaluate(lane, run, now, pauses, limit_s, resume_grace_s)["verdict"]


def load_pauses(path, since=None):
    """Read the pause records of a box from a JSONL file.

    Malformed lines are skipped.  Only the records that reach into the period
    after ``since`` are kept; a pause that is still open reaches into every
    later period.
    """
    records = []
    boundary = _epoch(since)
    with open(os.fspath(path), encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            pair = _interval(record)
            if pair is None:
                continue
            _started, resumed = pair
            if boundary is not None and resumed is not None and resumed < boundary:
                continue
            records.append(record)
    return records


def _load_runs(path):
    """Read the run records of a box.

    The file holds a JSON array of records or an object with a ``runs`` array; an
    object may also name the ``lane`` every run belongs to.
    """
    with open(os.fspath(path), encoding="utf-8") as stream:
        data = json.load(stream)
    default_lane = None
    if isinstance(data, dict):
        default_lane = data.get("lane")
        data = data.get("runs")
    if not isinstance(data, list):
        raise ValueError("runs file must hold a JSON array of runs")
    runs = []
    for record in data:
        if not isinstance(record, dict):
            continue
        runs.append((record.get("lane", default_lane), record))
    return runs


def main(argv=None):
    parser = argparse.ArgumentParser(prog="liveness.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command")
    check = commands.add_parser("check", help="judge the runs of a box")
    check.add_argument("--runs", required=True, help="runs JSON file")
    check.add_argument("--pauses", help="box pause log (JSONL)")
    check.add_argument("--limit", type=float, default=1200.0, help="awake silence limit in seconds")
    check.add_argument("--grace", type=float, default=float(DEFAULT_RESUME_GRACE_S),
                       help="seconds after a resume in which no run is killed")
    check.add_argument("--now", type=float, default=None, help="treat this epoch as now")
    check.add_argument("--json", action="store_true", dest="as_json", help="print machine-readable output")
    args = parser.parse_args(argv)
    if args.command != "check":
        parser.print_help()
        return 2
    try:
        runs = _load_runs(args.runs)
        pauses = load_pauses(args.pauses, None) if args.pauses else []
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    now = args.now if args.now is not None else time.time()
    reports = [evaluate(lane, run, now, pauses, args.limit, args.grace) for lane, run in runs]
    if args.as_json:
        print(json.dumps({"now": now, "limit_s": args.limit, "runs": reports}, indent=2, sort_keys=True))
    else:
        for report in reports:
            print("%s  %s  %s  silence=%ds" % (report["lane"] or "-", report["run_id"] or "-",
                                               report["verdict"], int(report["silence_s"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
