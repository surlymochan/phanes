#!/usr/bin/env python3
"""Read available host memory for low-memory dispatch protection.

The probe is reproducible: the same ``/proc/meminfo`` text always yields the
same sample.  ``remote_snippet`` emits a standalone one-liner so a lane host
without this repository can report its own ``MemAvailable`` over SSH, and the
dispatcher measures the host that runs the lane instead of its own memory.
"""

import inspect
import json


# Fallback for the rare host where the probe source cannot be read back: the
# same MemAvailable field, read inline.
FALLBACK_SNIPPET = (
    "import json;print(json.dumps({'available_kb':[int(l.split()[1]) for l in "
    "open('/proc/meminfo') if l.startswith('MemAvailable:')][0]}))")


def parse_meminfo(text):
    """Return the ``kB`` fields of a ``/proc/meminfo`` text."""
    fields = {}
    for line in text.splitlines():
        name, separator, value = line.partition(":")
        if not separator:
            continue
        parts = value.split()
        if parts and parts[0].isdigit():
            fields[name] = int(parts[0])
    return fields


def sample(available_kb):
    """Return the probe sample of an available-memory reading."""
    available_kb = int(available_kb)
    return {"available_kb": available_kb, "available_mb": available_kb / 1024}


def memory_usage(meminfo_path="/proc/meminfo"):
    """Return a reproducible MemAvailable sample in kB and MiB."""
    with open(meminfo_path, encoding="ascii") as stream:
        fields = parse_meminfo(stream.read())
    if "MemAvailable" not in fields:
        raise ValueError("MemAvailable is missing from meminfo")
    return sample(fields["MemAvailable"])


def remote_snippet():
    """Return one physical line usable by python3 -c on a host without this repo.

    Only the trusted probe sources are executed; the probed path is the remote
    ``/proc/meminfo``.  Reusing their source keeps both probes identical.
    """
    try:
        source = "\n".join(inspect.getsource(f) for f in (parse_meminfo, sample, memory_usage))
    except (OSError, TypeError):
        return FALLBACK_SNIPPET
    return (f"import json;exec({source!r});"
            f"print(json.dumps(memory_usage()))")


def parse_remote_sample(output):
    """Parse the JSON a remote host prints for its available memory."""
    text = (output or "").strip()
    if not text:
        raise ValueError("remote memory probe returned no output")
    data = json.loads(text.splitlines()[-1])
    if not isinstance(data, dict) or "available_kb" not in data:
        raise ValueError("remote memory probe returned no available_kb")
    return sample(data["available_kb"])


def update_guard(state, host, available_mb, threshold_mb, resume_mb=None):
    """Update the persisted guard of one host and report ``(low, changed)``.

    The guard is per host: a low-memory host only stops the new dispatch to
    its own lanes, and it lifts itself as soon as the host reports enough
    memory again (``resume_mb`` defaults to the pause watermark).
    """
    guards = state.setdefault("_memory_guard", {})
    entry = guards.setdefault(host, {})
    was_low = bool(entry.get("low"))
    resume = threshold_mb if resume_mb is None else resume_mb
    if was_low:
        low = available_mb < resume
    else:
        low = threshold_mb > 0 and available_mb < threshold_mb
    entry["low"] = low
    entry["available_mb"] = available_mb
    return low, low != was_low


def host_low(state, host):
    """Whether the persisted guard pauses the new dispatch to the host."""
    entry = (state.get("_memory_guard") or {}).get(host) or {}
    return bool(entry.get("low"))


def main():
    print(json.dumps(memory_usage(), sort_keys=True))


if __name__ == "__main__":
    main()
