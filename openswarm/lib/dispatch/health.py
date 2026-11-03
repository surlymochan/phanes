"""Host health probing and pause/resume decisions for dispatch.

SSH-failure pauses have no local recovery watermark; the caller owns their
recovery policy (for example, retrying SSH and clearing its failure streak).
"""

import re


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


def ssh_fail_record(streak, status, updated_at):
    """The per-host state the health probe remembers between two probes.

    The dispatch gate reads the same field, so the persisted shape of a host
    record lives here only: ``ssh_fail_streak`` sits on the record itself.
    """
    try:
        count = int(streak or 0)
    except (TypeError, ValueError):
        count = 0
    return {"ssh_fail_streak": count, "status": status, "updated_at": updated_at}


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


def run_probe(host, runner):
    """Run df/free commands through ``runner(host, command)`` and parse output.

    The runner returns stdout text and may raise on SSH failure.
    """
    disk = runner(host, "df -P /")
    inode = runner(host, "df -Pi /")
    memory = runner(host, "free -m")
    return {"host": host, "disk_pct": _df_percent(disk, "Use%"),
            "inode_pct": _df_percent(inode, "IUse%"),
            "mem_avail_mb": _memory(memory), "ssh_fail_streak": 0}


def next_ssh_streak(previous, reachable):
    """The consecutive SSH failure count of a host after one probe."""
    if reachable:
        return 0
    try:
        count = int(previous or 0)
    except (TypeError, ValueError):
        count = 0
    return count + 1


def check_host(host, runner, thresholds=None, paused=False, ssh_fail_streak=0):
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
        probe = run_probe(host, runner)
    except OSError as error:
        streak += 1
        decision, reason = evaluate({"host": host, "paused": paused,
                                     "ssh_fail_streak": streak}, limits)
        return {"host": host, "status": STATUS_UNREACHABLE, "decision": decision, "reason": reason,
                "error": str(error), "metrics": {"ssh_fail_streak": streak},
                "triggers": ["ssh_fail_streak"] if decision == "pause" else []}
    except ValueError as error:
        decision, reason = evaluate({"host": host, "paused": paused}, limits)
        return {"host": host, "status": STATUS_UNREACHABLE, "decision": decision, "reason": reason,
                "error": str(error), "metrics": {}, "triggers": []}
    probe["paused"] = bool(paused)
    probe["ssh_fail_streak"] = 0
    decision, reason = evaluate(probe, limits)
    levels = pressure_levels(probe, limits)
    triggers = [row["field"] for row in levels if row["over_pause"]]
    pending = [row["field"] for row in levels if not row["over_pause"] and row["over_resume"]]
    if triggers:
        status = STATUS_HARD
    elif pending:
        status = STATUS_SOFT
    else:
        status = STATUS_OK
    metrics = {row["field"]: row["value"] for row in levels}
    metrics["ssh_fail_streak"] = 0
    return {"host": host, "status": status, "decision": decision, "reason": reason,
            "metrics": metrics, "triggers": triggers}
