#!/usr/bin/env python3
"""Triage a burst of lane ``exec_error`` runs (Refs #1734).

Answers the operational question the GH2 card asks: "the lane kept ending in
``failed/exec_error`` -- is that the card, the host, the provider quota, the
broker socket or the executor session?"  The tool only reads artifacts that a
run already left behind, so it can be run on the lane host or on a copy of the
run directories that was pulled back to the box.

Inputs
------
``--runs DIR``   A directory that holds run directories (each with a
                 ``result.json``).  ``openswarm`` keeps them under
                 ``<home>/runs/<run_id>/``; a fleet harvest drops the same
                 layout under its ``--into`` directory.  May be repeated.
``--lane NAME``  Only report this lane.  May be repeated.
``--since ISO``  Ignore runs that finished before this timestamp.
``--harvest FILE``  A fleet harvest report (``openswarm fleet harvest
                 --json``).  Its ``failed`` entries carry no ``error_class``:
                 the remote bundle could not be fetched, so the remote
                 execution state of those runs stays unknown.
``--broker-log FILE``  A broker request log (JSONL, the format
                 ``tools/broker-log-summary.py`` reads).  It answers whether a
                 failing run reached the broker at all.
``--compare-lane NAME``  Compare this lane with the first reported lane.

What the tool reports
---------------------
For every failed run: the recorded ``error_class``, the executor return code,
the wall time, and the *evidence line* (file, line number, text) that supports
the cause hypothesis.  The hypotheses reuse the executor's own patterns, so the
tool agrees with ``executor_error_class``
(``swrm/bin/swrm:3942``):

  exec_error        the executor exited non-zero or produced no final report
                    (``swrm/bin/swrm:3942-3959``)
  quota_or_rate_limit  the error text matched RATE_LIMIT_RE
  auth              the error text matched AUTH_ERROR_RE
  model_unavailable the error text matched MODEL_UNAVAILABLE_RE

For every lane: the dominant signature (``<error_class>:<cause>``), the burst
window, the duration distribution and the evidence lines.  The signature is a
coarse grouping of the recorded evidence, so it cannot decide the root cause:
matching signatures do not prove a common cause and differing signatures do
not rule a common cause out.  The comparison therefore reports the root cause
as unknown and names the evidence it still needs.

A lane whose runs carry no ``error_class`` has an unknown remote execution
state -- the harvest may have failed to fetch the result bundle -- so the tool
cannot claim that the executor never ran there.

Usage:
  lane-exec-error-triage.py --runs DIR [--runs DIR ...] [--lane NAME]...
                            [--since ISO] [--harvest FILE] [--broker-log FILE]
                            [--compare-lane NAME] [--json] [--self-test]
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The executor's own patterns (swrm/bin/swrm:3483-3486); a triage
# that disagrees with the executor about "quota" is worthless.
MODEL_UNAVAILABLE_RE = re.compile(r"(at capacity|server.?overloaded|overloaded|try a different model|model is not supported|not supported when using|model .{0,40}(not (found|available)|is unavailable))", re.I)
RATE_LIMIT_RE = re.compile(r"(\b429\b|resource[_ -]?exhausted|rate.?limit|usage limit|quota|too many requests|insufficient balance|spend.?control)", re.I)
AUTH_ERROR_RE = re.compile(r"(authentication required|not authenticated|unauthenticated|not logged in|please log ?in|login required|auth(?:entication)? error|FatalAuthenticationError)", re.I)

# Evidence patterns for the causes the executor does not name itself.  The
# order matters: the first match wins, so the specific ones come first.
CAUSE_PATTERNS = (
    ("broker_unreachable", re.compile(r"broker\.sock|broker socket|no such file or directory.*broker|"
                                      r"connect(?:ion)? (?:refused|failed|reset)|ECONNREFUSED|socat:", re.I)),
    ("quota_or_rate_limit", RATE_LIMIT_RE),
    ("auth", AUTH_ERROR_RE),
    ("model_unavailable", MODEL_UNAVAILABLE_RE),
    ("oom", re.compile(r"out of memory|\boom\b|cannot allocate memory|killed process|"
                       r"memory exhausted|exit code 137|signal 9", re.I)),
    ("sandbox_spawn_failed", re.compile(r"bwrap|bubblewrap|unshare|namespace|operation not permitted|"
                                        r"failed to create (?:namespace|container|sandbox)", re.I)),
    ("session_unusable", re.compile(r"cannot resume a session|session (?:dir|directory|file).*"
                                    r"(?:not found|missing|corrupt|invalid)|no such session", re.I)),
    ("executor_missing", re.compile(r"\bpi not found\b|\bbinary not found\b|command not found|"
                                    r"no such file or directory: (?:pi|codex|copilot|cursor)", re.I)),
    ("timeout", re.compile(r"timed? ?out|timeout", re.I)),
)

# The recorded error class wins over the text heuristics, except for
# exec_error: that class says "non-zero exit or no final report", so the
# evidence picks the cause.
ERROR_CLASS_CAUSES = {
    "rate_limit": "quota_or_rate_limit",
    "quota": "quota_or_rate_limit",
    "budget_exceeded_daily": "quota_or_rate_limit",
    "budget_exceeded": "budget_timeout",
    "auth": "auth",
    "model_unavailable": "model_unavailable",
    "risk_blocked": "risk_blocked",
    "executor_io_lost": "executor_io_lost",
    "stalled": "stalled",
    "prepare_failed": "prepare_failed",
    "worktree_unavailable": "worktree_unavailable",
    "vm_paused": "vm_paused",
    "no_report": "no_final_report",
    "executor_failed": "executor_hard_failure",
    "exec_error": "executor_hard_failure",
}

# The cause of a run whose result carries no error class at all.
NO_CLASS_CAUSE = "missing_error_class"

RUN_ID_RE = re.compile(r"(\d{8})-(\d{6})")
# The executor writes its stderr next to the event stream: events.1.pi.jsonl
# and events.1.pi.jsonl.err (swrm/bin/swrm:4613).
EVENT_RE = re.compile(r"^events\.[^.]+\.[a-z_]+\.jsonl(\.err)?$")
MAX_SCAN_BYTES = 512 * 1024
IN_PROGRESS_STATES = {"started", "starting", "queued", "routing", "running", "finalizing", "verifying"}


def parse_ts(value):
    """Parse an epoch or ISO-8601 timestamp into an aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return parse_ts(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def run_id_time(text):
    """Parse the ``%Y%m%d-%H%M%S`` prefix of a run id into a UTC datetime."""
    match = RUN_ID_RE.search(text or "")
    if not match:
        return None
    try:
        stamp = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return stamp.astimezone(timezone.utc)


def load_json(path):
    """Read a JSON file; None when it is missing or unparsable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def fmt_ts(when):
    """Format a timestamp for the text report."""
    if when is None:
        return "-"
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def iter_run_dirs(directories):
    """Yield the run directories below the given roots.

    A root may be a runs directory (its children are runs) or a single run
    directory (it holds a result.json itself).  A run directory without a
    result.json but with a status.json is reported too. In-flight runs count
    toward the run total, but not toward failures while awaiting a result.
    """
    for directory in directories:
        root = Path(directory)
        if not root.is_dir():
            raise ValueError(f"runs directory not found: {directory}")
        if (root / "result.json").is_file() or (root / "status.json").is_file():
            yield root
            continue
        seen = set()
        for path in sorted(root.rglob("result.json")):
            seen.add(path.parent)
            yield path.parent
        for path in sorted(root.rglob("status.json")):
            if path.parent not in seen:
                yield path.parent


def attempt_of(result):
    """The last attempt of a run; the one that decided its error class."""
    attempts = result.get("attempts")
    if isinstance(attempts, list) and attempts:
        last = attempts[-1]
        if isinstance(last, dict):
            return last
    return {}


def error_class_of(result):
    """The recorded error class of a run (result.json first, then attempts)."""
    for source in (result.get("error_class"), attempt_of(result).get("error_class")):
        if isinstance(source, str) and source:
            return source
    return None


def read_run(run_dir):
    """Read one run directory into a record."""
    result = load_json(run_dir / "result.json")
    if not isinstance(result, dict):
        result = {}
    status = load_json(run_dir / "status.json")
    if not isinstance(status, dict):
        status = {}
    # A result can be written before status.json is marked finished. Prefer
    # that result over a stale in-flight state.
    in_progress = not result and status.get("state") in IN_PROGRESS_STATES
    attempt = attempt_of(result)
    error_class = error_class_of(result)
    started = parse_ts(status.get("started")) or parse_ts(status.get("attempt_started"))
    finished = parse_ts(status.get("finished")) or parse_ts(result.get("finished"))
    seconds = attempt.get("seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        seconds = result.get("seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        seconds = None
    if started is None and finished is not None and seconds is not None:
        started = finished.fromtimestamp(finished.timestamp() - seconds, timezone.utc)
    if finished is None and started is not None and seconds is not None:
        finished = started.fromtimestamp(started.timestamp() + seconds, timezone.utc)
    # The run id carries the creation time ("%Y%m%d-%H%M%S-<hex>"), so it is
    # the last resort when status.json lost its timestamps.
    created = run_id_time(str(result.get("id") or run_dir.name))
    if created is not None:
        if started is None:
            started = created
        if finished is None and not in_progress:
            finished = created
    lane = result.get("lane") or status.get("lane") or "unknown"
    return {
        "id": str(result.get("id") or run_dir.name),
        "run_dir": str(run_dir),
        "lane": str(lane),
        "backend": result.get("backend") or status.get("backend"),
        "model": result.get("model") or status.get("model"),
        "title": result.get("title") or status.get("title") or "",
        "status": result.get("status") or status.get("status") or status.get("state"),
        "in_progress": in_progress,
        "error_class": error_class,
        "rc": attempt.get("rc"),
        "seconds": seconds,
        "started": started,
        "finished": finished,
        "commit": result.get("commit"),
        "error_tail": attempt.get("error_tail") if isinstance(attempt.get("error_tail"), str) else "",
        "has_result": bool(result),
    }


def event_files(run_dir):
    """The executor event stream and its stderr, stderr first."""
    found = []
    try:
        names = sorted(path.name for path in Path(run_dir).iterdir())
    except OSError:
        return found
    for name in names:
        if EVENT_RE.match(name):
            found.append(Path(run_dir) / name)
    return sorted(found, key=lambda path: (not path.name.endswith(".err"), path.name))


def scan_evidence(run_dir, record):
    """Collect the lines that support a cause hypothesis.

    Every hit keeps its file and line number, so the report can be read as
    "log evidence", not as a guess.
    """
    evidence = []
    seen = set()
    sources = [("result.json:attempts[].error_tail", record.get("error_tail") or "")]
    for path in event_files(run_dir):
        try:
            size = path.stat().st_size
            with path.open("rb") as stream:
                # Count skipped newlines without loading the whole log. Keep
                # the partial first line too: its number is still absolute.
                remaining = max(0, size - MAX_SCAN_BYTES)
                skipped_lines = 0
                while remaining:
                    chunk = stream.read(min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    skipped_lines += chunk.count(b"\n")
                    remaining -= len(chunk)
                raw = stream.read(MAX_SCAN_BYTES).decode("utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(raw.split("\n"), skipped_lines + 1):
            line = line.strip()
            if not line:
                continue
            for cause, pattern in CAUSE_PATTERNS:
                if not pattern.search(line):
                    continue
                key = (cause, path.name, line[:120])
                if key in seen:
                    continue
                seen.add(key)
                evidence.append({"cause": cause, "file": path.name, "line": number,
                                 "text": line[:300]})
    for name, text in sources:
        for cause, pattern in CAUSE_PATTERNS:
            if not text or not pattern.search(text):
                continue
            key = (cause, name, text[:120])
            if key in seen:
                continue
            seen.add(key)
            evidence.append({"cause": cause, "file": name, "line": 0, "text": text[:300]})
    rc = record.get("rc")
    if isinstance(rc, int) and not isinstance(rc, bool) and rc < 0:
        evidence.append({"cause": f"killed_by_signal_{-rc}",
                         "file": "result.json:attempts[].rc", "line": 0,
                         "text": f"executor exited with rc={rc} (killed by signal {-rc})"})
    return evidence


def classify(record, evidence):
    """The cause hypothesis of a run and the signature used for comparisons."""
    error_class = record.get("error_class")
    causes = [item["cause"] for item in evidence]
    if record.get("in_progress"):
        return "in_progress", "none:in_progress"
    if not record.get("has_result"):
        # No result was available; the remote execution state is unknown.
        return "no_result", "none:no_result"
    if not error_class:
        return NO_CLASS_CAUSE, (f"none:{NO_CLASS_CAUSE}")
    if error_class != "exec_error":
        cause = ERROR_CLASS_CAUSES.get(error_class, error_class)
        return cause, f"{error_class}:{cause}"
    rc = record.get("rc")
    if isinstance(rc, int) and not isinstance(rc, bool) and rc < 0:
        # A negative return code is a child killed by a signal.  SIGKILL (-9)
        # with an empty stderr is the cgroup memory ceiling (the lane unit
        # sets MemoryMax) or an external kill.
        cause = f"killed_by_signal_{-rc}"
        return cause, f"{error_class}:{cause}"
    for cause, _pattern in CAUSE_PATTERNS:
        if cause in causes:
            return cause, f"{error_class}:{cause}"
    return "executor_hard_failure", f"{error_class}:executor_hard_failure"


def broker_records(path):
    """Read a broker request log into {ts, lane, status, err} rows."""
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                stamp = parse_ts(obj.get("ts"))
                if stamp is None:
                    continue
                rows.append({"ts": stamp, "lane": obj.get("lane") or "-",
                             "status": obj.get("status"), "err": str(obj.get("err") or "")[:200]})
    except OSError:
        return []
    return rows


def broker_cross_check(rows, record):
    """Did the failing run reach the broker at all?

    No request of this lane in the run window means the executor died before
    the request left the sandbox (socket, sandbox or session problem).  When
    other lanes did get requests in the same window, the broker was alive and
    the failure is local to this lane; a request that answered 429 or 5xx
    points at the provider side instead.
    """
    start, end = record.get("started"), record.get("finished")
    if start is None or end is None or not rows:
        return None
    window = [row for row in rows if start - _window_slack() <= row["ts"] <= end + _window_slack()]
    same_lane, others = [], []
    for row in window:
        # A broker log that does not record the lane ("-") counts as this
        # lane's request: it is the only attribution available.
        (same_lane if row["lane"] in (record.get("lane"), "-") else others).append(row)
    statuses = Counter(str(row["status"]) for row in same_lane)
    summary = {"requests": len(same_lane), "other_requests": len(others),
               "statuses": dict(statuses)}
    if not same_lane:
        summary["verdict"] = "lane_silent_broker_alive" if others else "no_broker_request"
        return summary
    if statuses.get("429", 0) and statuses["429"] >= max(statuses.values()):
        summary["verdict"] = "broker_rate_limited"
        return summary
    if any(str(code).startswith("5") for code in statuses):
        summary["verdict"] = "broker_upstream_error"
        return summary
    summary["verdict"] = "broker_saw_request"
    return summary


def _window_slack():
    """Tolerance around a run window when matching broker requests."""
    return timedelta(seconds=30)


def harvest_failures(path):
    """The failed entries of a fleet harvest report.

    A harvest failure carries no ``error_class``: the box could not fetch the
    remote result bundle; the remote execution state is unknown.
    """
    report = load_json(path)
    if not isinstance(report, dict):
        return []
    entries = report.get("failed")
    if not isinstance(entries, list):
        return []
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rows.append({"host": entry.get("host"), "lane": entry.get("lane"),
                     "run_id": entry.get("run_id"), "status": entry.get("status", "failed"),
                     "error_class": entry.get("error_class"),
                     "exit_code": entry.get("exit_code"), "error": str(entry.get("error") or "")[:300]})
    return rows


def summarise_lane(lane, records):
    """Aggregate the runs of one lane into a verdict."""
    failures = [record for record in records
                if record.get("status") != "done" and not record.get("in_progress")]
    signatures = Counter(record["signature"] for record in failures)
    dominant = signatures.most_common(1)[0][0] if signatures else None
    times = sorted(record["finished"] for record in failures if record.get("finished"))
    durations = [record["seconds"] for record in failures if isinstance(record.get("seconds"), (int, float))]
    evidence = []
    for record in failures:
        for item in record.get("evidence", []):
            row = dict(item)
            row["run"] = record["id"]
            evidence.append(row)
    return {
        "lane": lane,
        "runs": len(records),
        "failures": len(failures),
        "causes": dict(Counter(record["cause"] for record in failures)),
        "signatures": dict(signatures),
        "dominant_signature": dominant,
        "first_failure": times[0] if times else None,
        "last_failure": times[-1] if times else None,
        "duration_min": min(durations) if durations else None,
        "duration_max": max(durations) if durations else None,
        "runs_detail": [{"id": record["id"], "status": record["status"],
                         "error_class": record["error_class"], "cause": record["cause"],
                         "rc": record["rc"], "seconds": record["seconds"],
                         "started": record["started"], "finished": record["finished"],
                         "backend": record["backend"], "commit": record.get("commit"),
                         "broker": record.get("broker")} for record in failures],
        "evidence": evidence,
    }


def compare_lanes(first, second, second_name=None):
    """Report the signatures of two lanes without inferring the root cause.

    Equal signatures do not prove a common cause and differing signatures do
    not rule one out, so the root cause stays unknown until the field
    evidence of both lanes is read.  A lane that is missing from the report
    makes the comparison insufficient as well.
    """
    lanes = [first.get("lane") if first else None, second.get("lane") if second else None]
    a = first.get("dominant_signature") if first else None
    b = second.get("dominant_signature") if second else None
    missing = []
    if not first:
        missing.append("the first lane")
    if not second:
        missing.append(str((second or {}).get("lane") or second_name or "the compared lane"))
    if missing:
        reason = (f"lane(s) {', '.join(missing)} missing from the report; "
                  f"insufficient evidence to determine root cause")
    else:
        reason = (f"signatures: {a} vs {b}; matching or differing signatures do not "
                  f"decide the root cause; insufficient evidence to determine root cause")
    return {"lanes": lanes, "same_root_cause": None, "reason": reason,
            "needs": "field evidence (executor stderr, broker request log) of both lanes"}


def build_report(directories, lanes=None, since=None, broker_path=None, harvest_path=None,
                 compare_lane=None):
    """Read every run and build the triage report."""
    records = []
    for run_dir in iter_run_dirs(directories):
        record = read_run(run_dir)
        if lanes and record["lane"] not in lanes:
            continue
        if since is not None:
            finished = record["finished"] or record["started"]
            if finished is None or finished < since:
                continue
        record["evidence"] = scan_evidence(run_dir, record)
        record["cause"], record["signature"] = classify(record, record["evidence"])
        records.append(record)
    records.sort(key=lambda item: (item["finished"] or item["started"] or datetime.min.replace(tzinfo=timezone.utc),
                                    item["id"]))
    rows = broker_records(broker_path) if broker_path else []
    for record in records:
        record["broker"] = broker_cross_check(rows, record) if rows else None
    by_lane = {}
    for record in records:
        by_lane.setdefault(record["lane"], []).append(record)
    lane_summaries = [summarise_lane(lane, items) for lane, items in sorted(by_lane.items())]
    report = {
        "runs": len(records),
        "lanes": lane_summaries,
        "harvest_failures": harvest_failures(harvest_path) if harvest_path else [],
        "broker_log": broker_path,
    }
    if compare_lane:
        first = next((item for item in lane_summaries if item["lane"] != compare_lane), None)
        second = next((item for item in lane_summaries if item["lane"] == compare_lane), None)
        report["compare"] = compare_lanes(first, second, second_name=compare_lane)
    else:
        report["compare"] = None
    return report


def render_text(report):
    """Render the report for a terminal."""
    lines = [f"runs={report['runs']} lanes={len(report['lanes'])}"]
    for lane in report["lanes"]:
        lines.append("")
        lines.append(f"lane {lane['lane']}: runs={lane['runs']} failures={lane['failures']} "
                     f"dominant={lane['dominant_signature']}")
        lines.append(f"  causes: {lane['causes'] or '-'}")
        lines.append(f"  burst: {fmt_ts(lane['first_failure'])} .. {fmt_ts(lane['last_failure'])} "
                     f"duration={lane['duration_min']}..{lane['duration_max']}s")
        lines.append(f"  {'run':<24} {'status':<8} {'error_class':<16} {'cause':<24} {'rc':>5} {'sec':>8}")
        for run in lane["runs_detail"]:
            lines.append(f"  {run['id']:<24} {str(run['status']):<8} {str(run['error_class']):<16} "
                         f"{run['cause']:<24} {str(run['rc']):>5} {str(run['seconds']):>8}")
            broker = run.get("broker")
            if broker:
                lines.append(f"    broker: requests={broker['requests']} "
                             f"other_lane_requests={broker['other_requests']} "
                             f"statuses={broker['statuses'] or '-'} verdict={broker['verdict']}")
        if lane["evidence"]:
            lines.append("  evidence:")
            for item in lane["evidence"][:12]:
                where = f"{item['file']}:{item['line']}" if item["line"] else item["file"]
                lines.append(f"    [{item['cause']}] {item['run']} {where}: {item['text'][:160]}")
    harvest = report.get("harvest_failures") or []
    if harvest:
        lines.append("")
        lines.append(f"harvest failures without an error class: {len(harvest)}")
        for row in harvest[:12]:
            lines.append(f"  {row.get('host')}/{row.get('lane')} run={row.get('run_id')} "
                         f"rc={row.get('exit_code')} {row.get('error')}")
    compare = report.get("compare")
    if compare:
        lanes = compare.get("lanes") or []
        first = lanes[0] if len(lanes) > 0 and lanes[0] else "-"
        second = lanes[1] if len(lanes) > 1 and lanes[1] else "-"
        lines.append("")
        lines.append(f"compare {first} vs {second}: "
                     f"same_root_cause={compare.get('same_root_cause')}")
        lines.append(f"  {compare.get('reason')}")
        if compare.get("needs"):
            lines.append(f"  needs: {compare['needs']}")
    return "\n".join(lines)


def _write_fixture(root, run_id, lane, error_class, rc, seconds, tail, backend="pi",
                   started=1762113420.0, status="failed", with_result=True):
    """Write a run directory that looks like a real openswarm run."""
    run_dir = Path(root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "id": run_id, "status": status, "error_class": error_class, "backend": backend,
        "model": "MiniMax-M3.1-Flash", "lane": lane, "title": "diagnostic card",
        "seconds": seconds, "commit": None,
        "attempts": [{"backend": backend, "rc": rc, "seconds": seconds,
                      "error_class": error_class, "error_tail": tail}],
    }
    if with_result:
        (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (run_dir / "status.json").write_text(json.dumps(
        {"lane": lane, "backend": backend, "started": started, "finished": started + seconds,
         "state": "finished", "status": status}), encoding="utf-8")
    return run_dir


def run_self_test():
    """Check the classification, the evidence lines and the comparison."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        gh2 = root / "gh2"
        cx9 = root / "cx9"
        # GH2: the executor died before a request reached the broker.
        for run_id in ("20261103-001700-aaaa", "20261103-002100-bbbb",
                       "20261103-002300-cccc", "20261103-002500-dddd"):
            run_dir = _write_fixture(gh2, run_id, "GH2", "exec_error", 1, 118,
                                     "socat: connect(/tmp/openswarm-pi-abc/broker.sock): "
                                     "No such file or directory")
            (run_dir / "events.1.pi.jsonl.err").write_text(
                "pi isolated launcher\nsocat: connect(/tmp/openswarm-pi-abc/broker.sock): "
                "No such file or directory\n", encoding="utf-8")
        # CX9: the box could not fetch the remote result bundle.
        for run_id in ("20261102-231000-1111", "20261102-233000-2222"):
            _write_fixture(cx9, run_id, "CX9", None, None, 95,
                           "remote run info failed (rc=255)", backend="codex")
        # A run that finished without leaving a result is a third shape.
        _write_fixture(cx9, "20261102-235000-3333", "CX9", None, None, 40,
                       "", backend="codex", with_result=False)

        harvest = root / "harvest.json"
        harvest.write_text(json.dumps({"ok": False, "status": "failed", "into": str(root),
                                       "completed": [], "fetch_fails": 2,
                                       "failed": [{"host": "gores2", "lane": "CX9",
                                                   "run_id": "20261102-231000-1111", "status": "failed",
                                                   "exit_code": 255, "stderr": "",
                                                   "error": "remote run info failed (rc=255)",
                                                   "fetch_fails": 2}]}), encoding="utf-8")

        broker = root / "broker.requests.jsonl"
        broker.write_text("\n".join([
            json.dumps({"ts": "2026-11-03T00:17:30Z", "id": "x", "lane": "M8", "status": 200,
                        "err": "", "ms": 900}),
        ]) + "\n", encoding="utf-8")

        report = build_report([str(root)], broker_path=str(broker), harvest_path=str(harvest))
        lanes = {item["lane"]: item for item in report["lanes"]}
        assert set(lanes) == {"GH2", "CX9"}, sorted(lanes)
        gh2_lane = lanes["GH2"]
        assert gh2_lane["failures"] == 4, gh2_lane
        assert gh2_lane["dominant_signature"] == "exec_error:broker_unreachable", gh2_lane
        assert gh2_lane["causes"] == {"broker_unreachable": 4}, gh2_lane["causes"]
        evidence = gh2_lane["evidence"][0]
        assert evidence["file"] == "events.1.pi.jsonl.err", evidence
        assert evidence["line"] == 2, evidence
        assert "broker.sock" in evidence["text"], evidence
        assert gh2_lane["runs_detail"][0]["status"] == "failed", gh2_lane

        cx9_lane = lanes["CX9"]
        assert cx9_lane["failures"] == 3, cx9_lane
        assert cx9_lane["dominant_signature"] == "none:missing_error_class", cx9_lane
        assert cx9_lane["causes"]["no_result"] == 1, cx9_lane["causes"]
        assert report["harvest_failures"][0]["error_class"] is None, report["harvest_failures"]

        report = build_report([str(root)], broker_path=str(broker), compare_lane="CX9")
        compare = report["compare"]
        assert compare["same_root_cause"] is None, compare
        assert "insufficient evidence" in compare["reason"], compare

        # A quota failure keeps its own class and never becomes exec_error.
        _write_fixture(root / "quota", "20261103-003000-eeee", "M8", "rate_limit", 75, 30,
                       "429 Too Many Requests")
        report = build_report([str(root / "quota")])
        assert report["lanes"][0]["dominant_signature"] == "rate_limit:quota_or_rate_limit", report

    print("ok")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Triage a burst of lane exec_error runs.")
    parser.add_argument("--runs", action="append", metavar="DIR",
                        help="directory with run directories (result.json); may be repeated")
    parser.add_argument("--lane", action="append", metavar="NAME",
                        help="only report this lane; may be repeated")
    parser.add_argument("--since", metavar="ISO", help="ignore runs that finished before this time")
    parser.add_argument("--harvest", metavar="FILE",
                        help="fleet harvest report; its failed entries carry no error class")
    parser.add_argument("--broker-log", metavar="FILE", help="broker request log (JSONL)")
    parser.add_argument("--compare-lane", metavar="NAME", help="compare this lane with the first one")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()
    if not args.runs:
        parser.error("at least one --runs DIR is required")

    since = None
    if args.since:
        since = parse_ts(args.since)
        if since is None:
            parser.error(f"invalid --since: {args.since!r}")

    try:
        report = build_report(args.runs, lanes=set(args.lane or []) or None, since=since,
                               broker_path=args.broker_log, harvest_path=args.harvest,
                               compare_lane=args.compare_lane)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
