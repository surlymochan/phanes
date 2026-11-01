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


def _number(probe, field):
    value = probe.get(field)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


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

    pressures = (
        ("disk_pct", "disk_pause_pct", "disk_resume_pct", "% disk", True),
        ("inode_pct", "inode_pause_pct", "inode_resume_pct", "% inodes", True),
        ("mem_avail_mb", "mem_pause_mb", "mem_resume_mb", " MB memory available", False),
    )
    measured = 0
    active = []
    for field, pause_key, resume_key, label, high_is_bad in pressures:
        value = _number(probe, field)
        if value is None:
            continue
        measured += 1
        if (value >= limits[pause_key]) if high_is_bad else (value <= limits[pause_key]):
            active.append(f"{value:g}{label} (pause threshold {limits[pause_key]})")
            if not paused:
                return ("pause", f"{host}: {active[-1]}")
        elif paused and ((value > limits[resume_key]) if high_is_bad else (value < limits[resume_key])):
            active.append(f"{value:g}{label} (waiting for recovery threshold {limits[resume_key]})")

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
