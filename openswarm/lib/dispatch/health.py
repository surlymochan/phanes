"""Host health probing and pause/resume decisions for dispatch.

SSH-failure pauses have no local recovery watermark; the caller owns their
recovery policy (for example, retrying SSH and clearing its failure streak).

The probe also measures the clock of every host against the clock of the
control plane (Refs #1703).  A host whose clock is off cannot be used for a
time-sensitive decision: the fleet logs of the control plane and of the host
then disagree about the order of the same events, and an age computed from the
host's own stamps is wrong by the skew.  The measured skew is remembered per
host, so every consumer can tell a trustworthy time from an untrustworthy one.
"""

import math
import re
import time


DEFAULT_THRESHOLDS = {
    "disk_pause_pct": 95,
    "disk_resume_pct": 92,
    "inode_pause_pct": 95,
    "inode_resume_pct": 92,
    "mem_pause_mb": 512,
    "mem_resume_mb": 640,
    "ssh_fail_pause_streak": 3,
}

# The measured pressures: field, pause watermark, resume watermark, label, and
# whether a high value is the bad one (percentages) or a low one (free memory).
PRESSURES = (
    ("disk_pct", "disk_pause_pct", "disk_resume_pct", "% disk", True),
    ("inode_pct", "inode_pause_pct", "inode_resume_pct", "% inodes", True),
    ("mem_avail_mb", "mem_pause_mb", "mem_resume_mb", " MB memory available", False),
)

# The health of one host, from the operator's point of view.
STATUS_OK = "ok"
STATUS_SOFT = "soft"
STATUS_HARD = "hard"
STATUS_UNREACHABLE = "unreachable"

# A host clock further away than this from the clock of the control plane
# cannot be used for a time-sensitive decision (Refs #1703).
CLOCK_SKEW_THRESHOLD_S = 60

# A skew has to grow by at least this much before it is worth a new alert: a
# steady skew is reported once, not on every probe.
CLOCK_SKEW_EXPAND_MIN_S = 5.0

# The repair hint of a clock alert: the operator has to sync the host before
# the time-based decisions of that host can be trusted again.
CLOCK_NTP_HINT = "synchronize with NTP (timedatectl set-ntp true)"


def clock_skew(remote_epoch, control_epoch):
    """Signed host clock offset in seconds (positive means the host is fast).

    None when one of the two readings is missing: an unmeasured skew is not a
    skew of zero.
    """
    if remote_epoch is None or control_epoch is None:
        return None
    return float(remote_epoch) - float(control_epoch)


def clock_status(offset_s, threshold_s=CLOCK_SKEW_THRESHOLD_S):
    """Describe whether time-based decisions on a host can be trusted.

    The offset is signed (positive means the host is fast); ``unreliable`` says
    whether the host may be judged by time at all, and ``warning`` is the line
    an operator reads (direction, seconds and the repair hint).
    """
    if offset_s is None:
        return {"offset_s": None, "unreliable": True, "direction": None,
                "threshold_s": float(threshold_s), "warning": "clock unavailable"}
    offset_s = float(offset_s)
    if abs(offset_s) <= threshold_s:
        return {"offset_s": round(offset_s, 1), "unreliable": False, "direction": None,
                "threshold_s": float(threshold_s), "warning": None}
    direction = "fast" if offset_s > 0 else "slow"
    return {"offset_s": round(offset_s, 1), "unreliable": True, "direction": direction,
            "threshold_s": float(threshold_s),
            "warning": f"clock {direction} by {abs(offset_s):.1f}s; {CLOCK_NTP_HINT}"}


def clock_record(clock):
    """The clock fields a host record remembers: the offset and its verdict.

    A clock that was not measured (an unreachable host) remembers nothing: the
    last known state of that host stays untouched instead of being replaced by
    a guess.
    """
    if not isinstance(clock, dict) or clock.get("offset_s") is None:
        return {}
    return {"clock_offset_s": clock.get("offset_s"),
            "clock_unreliable": bool(clock.get("unreliable"))}


def clock_states(state):
    """The remembered clock of every host: ``{host: {"offset_s":..., "unreliable":...}}``.

    ``state`` is the parsed ``fleet-health.json`` the health probe writes.  A
    damaged state is never evidence of a skewed clock: a host without a usable
    record has no known skew at all.
    """
    hosts = state.get("hosts") if isinstance(state, dict) else None
    if not isinstance(hosts, dict):
        return {}
    states = {}
    for host, record in hosts.items():
        if not isinstance(record, dict):
            continue
        if "clock_offset_s" not in record and "clock_unreliable" not in record:
            continue
        offset = _number(record, "clock_offset_s")
        unreliable = record.get("clock_unreliable")
        states[str(host)] = {"offset_s": offset,
                             "unreliable": bool(unreliable) if isinstance(unreliable, bool)
                             else bool(offset is not None and abs(offset) > CLOCK_SKEW_THRESHOLD_S)}
    return states


def clock_unreliable_hosts(state):
    """The hosts whose clock cannot be used for a time-sensitive decision."""
    return {host for host, row in clock_states(state).items() if row.get("unreliable")}


def clock_alert(previous, clock, threshold_s=CLOCK_SKEW_THRESHOLD_S,
                expand_min_s=CLOCK_SKEW_EXPAND_MIN_S):
    """Whether the clock skew of a host is worth a line in the brain log.

    ``previous`` is the remembered clock of the host (None when it has none) and
    ``clock`` the clock of this probe.  A skew beyond the threshold is reported
    when it appears for the first time or when it grew; a steady skew is not
    reported again, so the log does not fill up with the same line.  A host
    whose clock came back into range reports nothing, and its next skew counts
    as a first one again.
    """
    if not isinstance(clock, dict) or not clock.get("unreliable"):
        return None
    offset = clock.get("offset_s")
    if offset is None:
        return None
    offset = abs(float(offset))
    remembered = previous if isinstance(previous, dict) else {}
    if not remembered.get("unreliable"):
        kind = "first"
    else:
        before = remembered.get("offset_s")
        if before is None:
            kind = "first"
        elif offset - abs(float(before)) >= expand_min_s:
            kind = "expanded"
        else:
            return None
    return {"kind": kind, "offset_s": offset, "direction": clock.get("direction"),
            "threshold_s": float(threshold_s), "warning": clock.get("warning")}


def _number(probe, field):
    value = probe.get(field)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def thresholds_from_policy(policy):
    """The health thresholds of a policy: ``resources.health`` over DEFAULT_THRESHOLDS."""
    limits = dict(DEFAULT_THRESHOLDS)
    resources = policy.get("resources") if isinstance(policy, dict) else None
    health = resources.get("health") if isinstance(resources, dict) else None
    if isinstance(health, dict):
        for key, value in health.items():
            if key in limits:
                limits[key] = value
    return limits


def ssh_fail_blocked(streak, thresholds=None):
    """Whether a consecutive SSH failure count reached the pause threshold.

    The threshold is the one the health probe pauses a host with
    (``resources.health.ssh_fail_pause_streak``, default 3): at or above it the
    host counts as unreachable.
    """
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        limits.update(thresholds)
    try:
        count = int(streak or 0)
    except (TypeError, ValueError):
        count = 0
    try:
        threshold = float(limits["ssh_fail_pause_streak"])
    except (TypeError, ValueError):
        threshold = float(DEFAULT_THRESHOLDS["ssh_fail_pause_streak"])
    return count >= threshold


def ssh_fail_record(streak, status, updated_at, clock=None):
    """The per-host state the health probe remembers between two probes.

    The dispatch gate reads the same field, so the persisted shape of a host
    record lives here only: ``ssh_fail_streak`` sits on the record itself.
    The measured clock of the host (``clock``) sits on it as well, so the next
    probe can tell a first skew from a growing one.
    """
    try:
        count = int(streak or 0)
    except (TypeError, ValueError):
        count = 0
    record = {"ssh_fail_streak": count, "status": status, "updated_at": updated_at}
    record.update(clock_record(clock))
    return record


def ssh_fail_streaks(state):
    """The consecutive SSH failure count every host has in a fleet-health state.

    ``state`` is the parsed ``fleet-health.json`` the health probe writes:
    ``{"hosts": {host: {"ssh_fail_streak": n, "status": ..., "updated_at": ...}}}``.
    A damaged state is never evidence of a failure: a host without a usable
    count has no streak at all.
    """
    hosts = state.get("hosts") if isinstance(state, dict) else None
    if not isinstance(hosts, dict):
        return {}
    streaks = {}
    for host, record in hosts.items():
        if not isinstance(record, dict):
            continue
        count = _number(record, "ssh_fail_streak")
        if count is not None:
            streaks[str(host)] = int(count)
    return streaks


def pressure_levels(probe, limits):
    """Classify every valid measurement of a probe against the pause/resume watermarks.

    Each row names the field, its value, and whether the value is over the pause
    watermark (hard) and over the resume watermark (still recovering).
    """
    limits = {**DEFAULT_THRESHOLDS, **(limits or {})}
    rows = []
    for field, pause_key, resume_key, label, high_is_bad in PRESSURES:
        value = _number(probe, field)
        if value is None:
            continue
        over_pause = value >= limits[pause_key] if high_is_bad else value <= limits[pause_key]
        over_resume = value > limits[resume_key] if high_is_bad else value < limits[resume_key]
        rows.append({"field": field, "value": value, "label": label,
                     "over_pause": over_pause, "over_resume": over_resume,
                     "pause_threshold": limits[pause_key], "resume_threshold": limits[resume_key]})
    return rows


def evaluate(probe, thresholds=None):
    """Return (decision, reason), applying separate pause/resume watermarks.

    ``probe['paused']`` records the current host state; omitting it means the
    host is currently available. Thresholds override ``DEFAULT_THRESHOLDS``.
    A paused host resumes only when every valid measurement is back at its
    resume watermark; without any valid measurement it stays paused.
    """
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        limits.update(thresholds)
    host = probe.get("host", "unknown")
    paused = bool(probe.get("paused", False))
    failures = _number(probe, "ssh_fail_streak") or 0
    if failures >= limits["ssh_fail_pause_streak"]:
        return ("pause", f"{host}: SSH failure streak {failures:g}")

    measured = 0
    active = []
    for row in pressure_levels(probe, limits):
        measured += 1
        if row["over_pause"]:
            active.append(f"{row['value']:g}{row['label']} (pause threshold {row['pause_threshold']})")
            if not paused:
                return ("pause", f"{host}: {active[-1]}")
        elif paused and row["over_resume"]:
            active.append(f"{row['value']:g}{row['label']} (waiting for recovery threshold "
                          f"{row['resume_threshold']})")

    if paused:
        if active:
            return ("none", f"{host}: still paused; waiting for " + "; ".join(active))
        if not measured:
            return ("none", f"{host}: still paused; no valid measurements")
        return ("resume", f"{host}: all reported pressures recovered")
    return ("none", f"{host}: healthy")


def _percent(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)%", value)
    if not match:
        raise ValueError("invalid df percentage: " + value)
    return float(match.group(1))


def _df_percent(output, usage_column, mountpoint="/"):
    lines = [line.split() for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("df output has no data row")
    header = lines[0]
    alternatives = {"Use%": ("Capacity",), "IUse%": ("%iused",)}
    index = next((header.index(name) for name in (usage_column,) + alternatives.get(usage_column, ())
                  if name in header), None)
    if index is None:
        raise ValueError("df output missing " + usage_column)
    # ``Mounted on`` is printed as two header words but one data column.
    mount_index = header.index("Mounted") if "Mounted" in header else len(header) - 1
    rows = [row for row in lines[1:] if len(row) > mount_index and row[mount_index] == mountpoint]
    if not rows:
        raise ValueError("df output missing mountpoint " + mountpoint)
    row = rows[-1]
    if len(row) <= index:
        raise ValueError("truncated df data row")
    return _percent(row[index])


def _memory(output):
    lines = [line.split() for line in output.splitlines() if line.strip()]
    for row in lines:
        if row and row[0] == "Mem:":
            if len(row) >= 7:
                return int(row[6])
            break
    raise ValueError("free output missing Mem available column")


def _remote_epoch(output):
    """The epoch of a remote ``date +%s.%N`` reading.

    ``%N`` (the fractional seconds) is not portable: a ``date`` without it
    prints the literal ``N``.  The whole seconds are enough to measure a skew
    of minutes, so the fraction is dropped instead of failing the probe.
    """
    text = str(output).strip().split()
    if not text:
        raise ValueError("date output is empty")
    text = text[0]
    if text.endswith("N"):
        text = text[:-1]
    try:
        value = float(text)
    except ValueError:
        raise ValueError("invalid date output: " + str(output)[:40])
    if not math.isfinite(value):
        raise ValueError("invalid date output: " + str(output)[:40])
    return value


def run_probe(host, runner, clock=None):
    """Run df/free commands through ``runner(host, command)`` and parse output.

    The runner returns stdout text and may raise on SSH failure.  With a
    ``clock`` (the control plane's own clock) the probe also reads the clock of
    the host: the control plane is sampled right before and right after the
    remote command, so the middle of the two readings is the control-plane time
    of the moment the host answered -- a slow host or a slow link moves the
    reading, not the verdict.
    """
    disk = runner(host, "df -P /")
    inode = runner(host, "df -Pi /")
    memory = runner(host, "free -m")
    before = clock() if clock else None
    remote_time = _remote_epoch(runner(host, "date +%s.%N"))
    after = clock() if clock else None
    result = {"host": host, "disk_pct": _df_percent(disk, "Use%"),
            "inode_pct": _df_percent(inode, "IUse%"),
            "mem_avail_mb": _memory(memory), "ssh_fail_streak": 0,
            "host_epoch": remote_time}
    if clock:
        result["control_epoch"] = (before + after) / 2
        result["clock_probe_s"] = max(0.0, after - before)
    return result


def next_ssh_streak(previous, reachable):
    """The consecutive SSH failure count of a host after one probe."""
    if reachable:
        return 0
    try:
        count = int(previous or 0)
    except (TypeError, ValueError):
        count = 0
    return count + 1


def check_host(host, runner, thresholds=None, paused=False, ssh_fail_streak=0, control_epoch=None):
    """Probe one host and describe its health.

    ``runner(host, command)`` returns the command output and raises OSError when
    the transport fails.  The row carries the status (ok/soft/hard/unreachable),
    the pause decision, the measured metrics, and the metrics that triggered it.
    An unreachable host keeps counting its SSH failures: the streak reaches the
    pause threshold before the host is paused.
    """
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        limits.update(thresholds)
    streak = int(ssh_fail_streak or 0)
    try:
        probe = run_probe(host, runner, time.time if control_epoch is None else lambda: control_epoch)
    except OSError as error:
        streak += 1
        decision, reason = evaluate({"host": host, "paused": paused,
                                     "ssh_fail_streak": streak}, limits)
        return {"host": host, "status": STATUS_UNREACHABLE, "decision": decision, "reason": reason,
                "error": str(error), "metrics": {"ssh_fail_streak": streak},
                "triggers": ["ssh_fail_streak"] if decision == "pause" else [],
                "clock": clock_status(None)}
    except ValueError as error:
        decision, reason = evaluate({"host": host, "paused": paused}, limits)
        return {"host": host, "status": STATUS_UNREACHABLE, "decision": decision, "reason": reason,
                "error": str(error), "metrics": {}, "triggers": [],
                "clock": clock_status(None)}
    probe["paused"] = bool(paused)
    probe["ssh_fail_streak"] = 0
    offset = clock_skew(probe.get("host_epoch"), probe.get("control_epoch"))
    clock = clock_status(offset)
    decision, reason = evaluate(probe, limits)
    levels = pressure_levels(probe, limits)
    triggers = [row["field"] for row in levels if row["over_pause"]]
    pending = [row["field"] for row in levels if not row["over_pause"] and row["over_resume"]]
    if triggers:
        status = STATUS_HARD
    elif pending or clock["unreliable"]:
        status = STATUS_SOFT
    else:
        status = STATUS_OK
    metrics = {row["field"]: row["value"] for row in levels}
    metrics["ssh_fail_streak"] = 0
    metrics["clock_offset_s"] = clock["offset_s"]
    return {"host": host, "status": status, "decision": decision, "reason": reason,
            "metrics": metrics, "triggers": triggers, "clock": clock}
