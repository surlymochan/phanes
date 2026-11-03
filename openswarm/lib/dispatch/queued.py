"""Queued run diagnosis: name the layer that blocks a run (Refs #1687).

A run that stays in ``run_state=queued`` without a single request looks the same
from the outside, but the layers behind it are different: the capacity
coordinator holds a ghost reservation of a run that ended, the host cap of the
target host is an old value, the fleet wide backend/provider budget is spent,
or the local policy of the host runs more turns than it allows.  Every layer
lives in a different file on a different host, so the manual triage costs one
ssh per layer.

This module judges every run that waits longer than ``MIN_QUEUE_AGE_S`` and
names the layer that blocks it.  The verdicts are a fixed enum:

``blocked-by-ghost-reservation``
    The host still holds the reservation of a run that ended (a ghost).  The
    residual run is named, because releasing it is the fix.  A ghost only
    counts when it really fills the admission capacity of the waiting run and
    when releasing it lifts that limit.
``blocked-by-host-cap``
    The reservations of the host reached the host limit of the coordinator
    topology (``host:<host>``).  A limit that is smaller than the number of
    lanes of the host is reported as a probably stale value.
``blocked-by-fleet-cap``
    The fleet wide budget is spent: ``total``, the backend or the provider.
``blocked-by-local-policy``
    The local policy of the target host allows fewer concurrent turns than the
    host currently runs.
``unknown``
    No layer matched, or the evidence is not sufficient to name one.  The
    reason is never guessed; a high share of unknown verdicts asks for a human.

The judgement mirrors the admission path of ``bin/openswarm``
(``reserve_provider_slot`` and the coordinator): a run is admitted when every
counter of its concurrency keys is below its cap, so a run is blocked as soon
as one counter reaches its cap.  A run that already holds a reservation was
admitted by the coordinator, so the admission limits never block it.

The ghost judgement needs a *proof* that the run is over.  The recovery path
of the coordinator only frees a reservation when the runner and every recorded
executor of the run have stopped, so a residual whose executor still lives is
not a ghost, and a residual whose liveness evidence is missing is not a ghost
either: both end in ``unknown`` instead of a wrong name.

``diagnose(runs, reservations, limits, policies, ...)``
    The section: one row per queued run, the counts per verdict and the hint
    for a human.
``write_health_section(path, section)`` / ``append_brain_log(path, section)``
    Write the section into the ``health.json`` snapshot and into the brain log.
``main``
    The CLI of the section, for the health loop::

        python3 openswarm/lib/dispatch/queued.py --runs ~/.claude/openswarm/runs \\
            --reservations coordinator-status.json --topology topology.json \\
            --health-json ~/phanes-ctl/autodispatch/health.json \\
            --brain-log ~/phanes-ctl/brain/log.md

Standard library only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

__all__ = [
    "GHOST_GRACE_S",
    "LIVENESS_ALIVE",
    "LIVENESS_GONE",
    "LIVENESS_UNKNOWN",
    "MIN_QUEUE_AGE_S",
    "REASONS",
    "REASON_FLEET_CAP",
    "REASON_GHOST",
    "REASON_HOST_CAP",
    "REASON_LOCAL_POLICY",
    "REASON_UNKNOWN",
    "RECOVERY_TTL_S",
    "TERMINAL_STATES",
    "UNKNOWN_HUMAN_RATIO",
    "admission_keys",
    "append_brain_log",
    "brain_log_lines",
    "diagnose",
    "executor_alive",
    "ghost_reservations",
    "judge",
    "lane_hosts",
    "liveness",
    "load_limits",
    "load_reservations",
    "load_run_states",
    "main",
    "policy_caps",
    "queued_runs",
    "records_from_snapshot",
    "records_from_states",
    "snapshot_has_liveness",
    "write_health_section",
]

# A run that waits less than this is normal queueing, not a blockage.
MIN_QUEUE_AGE_S = 600

REASON_GHOST = "blocked-by-ghost-reservation"
REASON_HOST_CAP = "blocked-by-host-cap"
REASON_FLEET_CAP = "blocked-by-fleet-cap"
REASON_LOCAL_POLICY = "blocked-by-local-policy"
REASON_UNKNOWN = "unknown"

# The order of the enum is the order of the judgement: the most specific
# layer first, the unknown verdict last.
REASONS = (REASON_GHOST, REASON_HOST_CAP, REASON_FLEET_CAP, REASON_LOCAL_POLICY, REASON_UNKNOWN)

# The run states that mean the run is over.  A reservation of such a run that
# the coordinator still holds is a ghost candidate.
TERMINAL_STATES = ("finished", "failed", "unroutable", "dry-run", "dead", "stopped", "budget_rejected")

# The recovery TTL of the coordinator (policy fleet.recovery_ttl_seconds): a
# reservation older than this is only a ghost when its run is really gone.
RECOVERY_TTL_S = 600

# A release that is still in flight must never be reported as a ghost.
GHOST_GRACE_S = 60

# Above this share of unknown verdicts the section asks for a human.
UNKNOWN_HUMAN_RATIO = 0.5

# The liveness of a run: the executor runs, the executor is gone, or the
# evidence is missing.
LIVENESS_ALIVE = "alive"
LIVENESS_GONE = "gone"
LIVENESS_UNKNOWN = "unknown"


def epoch(value):
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


def _read_json(path):
    """Read a JSON file; None when it is missing or unparsable."""
    try:
        with open(os.fspath(path), encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, UnicodeError):
        return None


def pid_alive(pid):
    """Whether a pid is still running; False when the pid is not a number."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _group_alive(pgid):
    """Whether a process group still exists."""
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def executor_alive(status):
    """Whether an executor of the run still runs (the runner does not count).

    A resident lane keeps its runner alive between turns, so the runner proves
    nothing about the run.  The executor group does: it is the work itself.
    A run without a recorded executor has nothing running.
    """
    if not isinstance(status, dict):
        return False
    records = [{"pid": status.get("executor_pid"), "pgid": status.get("executor_pgid")}]
    records.extend(row for row in (status.get("executor_descendants") or [])
                   if isinstance(row, dict))
    for record in records:
        if pid_alive(record.get("pid")) or _group_alive(record.get("pgid")):
            return True
    return False


def liveness(record):
    """The liveness of a run record: alive, gone or unknown.

    ``unknown`` means the evidence is missing: the host did not report whether
    the executor of the run still runs.  Such a run is never a ghost.
    """
    value = record.get("executor_alive")
    if value is True:
        return LIVENESS_ALIVE
    if value is False:
        return LIVENESS_GONE
    return LIVENESS_UNKNOWN


# --------------------------------------------------------------------- input


def load_run_states(runs_dir):
    """Read every run status below a runs directory: ``{run_id: status}``.

    A broken status file is skipped: one unreadable run must not hide the
    other runs of the box.
    """
    states = {}
    try:
        names = sorted(os.listdir(runs_dir))
    except OSError:
        return states
    for name in names:
        status = _read_json(os.path.join(runs_dir, name, "status.json"))
        if isinstance(status, dict):
            states[name] = status
    return states


def load_reservations(answer):
    """The reservations of a coordinator status answer.

    The answer is the JSON of the ``status`` operation of ``bin/fleet-capacity``
    (``{"ok": true, "reservations": [...]}``); a bare list of reservations is
    accepted as well.
    """
    if isinstance(answer, dict):
        rows = answer.get("reservations")
    else:
        rows = answer
    if not isinstance(rows, list):
        return []
    reservations = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        reservations.append({
            "host": row.get("host"),
            "run_id": str(row.get("run_id") or ""),
            "backend": row.get("backend"),
            "provider": row.get("provider"),
            "created": epoch(row.get("created")),
        })
    return reservations


def load_limits(answer, topology=None):
    """The limits of the coordinator topology.

    The coordinator reports its own limits with the status answer; a
    ``topology.json`` file is the fallback for an older coordinator.
    """
    limits = {}
    if isinstance(answer, dict) and isinstance(answer.get("limits"), dict):
        limits.update(answer["limits"])
    if not limits and isinstance(topology, dict) and isinstance(topology.get("limits"), dict):
        limits.update(topology["limits"])
    return {key: value for key, value in limits.items()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0}


def policy_caps(policy):
    """The local concurrency caps of a policy (``concurrency``)."""
    caps = policy.get("concurrency") if isinstance(policy, dict) else None
    if not isinstance(caps, dict):
        return {}
    return {key: value for key, value in caps.items()
            if isinstance(value, int) and not isinstance(value, bool) and value > 0}


def lane_hosts(lanes):
    """The lanes of a lanes file: ``({lane: host}, {host: lane count})``."""
    hosts, counts = {}, {}
    rows = lanes.get("lanes") if isinstance(lanes, dict) else None
    for lane in rows if isinstance(rows, list) else []:
        if not isinstance(lane, dict):
            continue
        name = lane.get("name") or lane.get("lane")
        host = lane.get("host")
        if not isinstance(name, str) or not name:
            continue
        if isinstance(host, str) and host:
            hosts[name] = host
            counts[host] = counts.get(host, 0) + 1
    return hosts, counts


def _run_keys(record):
    """The concurrency keys of a run: the provider, the backend and the total."""
    keys = record.get("concurrency_keys")
    if isinstance(keys, list) and keys:
        return [str(key) for key in keys]
    return [key for key in (record.get("provider"), record.get("backend"), "total") if key]


def records_from_states(states, host=None, lane_hosts_map=None):
    """Normalise run states into run records.

    The liveness of the executor is measured here: the box knows its own
    processes, so the ghost judgement gets a real proof and never a guess.
    """
    records = []
    for name, status in (states or {}).items():
        lane = status.get("lane")
        run_host = status.get("host")
        if not run_host and lane and lane_hosts_map:
            run_host = lane_hosts_map.get(lane)
        if not run_host:
            run_host = host
        records.append({
            "run_id": str(status.get("id") or name),
            "host": run_host,
            "lane": lane,
            "backend": status.get("backend"),
            "provider": status.get("provider"),
            "state": status.get("state"),
            "started": epoch(status.get("started")) or epoch(status.get("attempt_started")),
            "concurrency_keys": status.get("concurrency_keys") or [],
            "queue_reason": status.get("queue_reason"),
            "executor_alive": executor_alive(status),
        })
    return records


def records_from_snapshot(host, snapshot, now=None):
    """The run records of one host snapshot (``fleet _snapshot``).

    The snapshot lists the runs that are not over (``active_runs``) and, since
    the liveness evidence of the ended runs was added, the runs that are over
    (``ended_runs``) with the proof whether their executor still runs.  A
    snapshot without ``ended_runs`` comes from a host that cannot prove
    anything about the ended runs: the caller keeps such a host out of the
    known hosts, so no ghost is derived from it.
    """
    moment = time.time() if now is None else float(now)
    records = []
    for run in (snapshot or {}).get("active_runs") or []:
        if not isinstance(run, dict):
            continue
        started = epoch(run.get("started"))
        if started is None:
            elapsed = run.get("elapsed_min")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
                started = moment - float(elapsed) * 60.0
        records.append({
            "run_id": str(run.get("id") or ""),
            "host": host,
            "lane": run.get("lane"),
            "backend": run.get("backend"),
            "provider": run.get("provider"),
            "state": run.get("state"),
            "started": started,
            "concurrency_keys": run.get("concurrency_keys") or [],
            "queue_reason": run.get("queue_reason"),
            "executor_alive": True,  # an active run still works
        })
    for run in (snapshot or {}).get("ended_runs") or []:
        if not isinstance(run, dict):
            continue
        records.append({
            "run_id": str(run.get("id") or ""),
            "host": host,
            "lane": run.get("lane"),
            "backend": run.get("backend"),
            "provider": run.get("provider"),
            "state": run.get("state"),
            "started": epoch(run.get("started")),
            "concurrency_keys": run.get("concurrency_keys") or [],
            "queue_reason": run.get("queue_reason"),
            "executor_alive": run.get("executor_alive"),
        })
    return records


def snapshot_has_liveness(snapshot):
    """Whether a host snapshot carries the liveness evidence of the ended runs."""
    return isinstance(snapshot, dict) and isinstance(snapshot.get("ended_runs"), list)


# ------------------------------------------------------------------ counting


def _reservation_keys(reservation):
    """The counters a reservation occupies (mirrors the coordinator)."""
    keys = {"total"}
    host = reservation.get("host")
    if host:
        keys.add("host:" + str(host))
    backend = reservation.get("backend")
    if backend:
        keys.add(str(backend))
    provider = reservation.get("provider")
    if provider:
        keys.add(str(provider))
    return keys


def usage_counts(reservations, exclude_run=None, exclude_ids=()):
    """How many reservations occupy every counter of the fleet.

    ``exclude_run`` leaves the reservation of the judged run out: it already
    holds its slot and must not block itself.  ``exclude_ids`` leaves the
    reservations of other runs out (the ghosts, the residuals).
    """
    counts = {}
    for reservation in reservations or []:
        run_id = str(reservation.get("run_id") or "")
        if exclude_run and run_id == str(exclude_run):
            continue
        if run_id in exclude_ids:
            continue
        for key in _reservation_keys(reservation):
            counts[key] = counts.get(key, 0) + 1
    return counts


def admission_keys(record):
    """The counters the coordinator counts for a run (its admission keys).

    The order is the order of the admission path of the coordinator: the
    host of the run comes first, then the fleet wide budget.
    """
    keys = []
    host = record.get("host")
    candidates = ["host:" + str(host)] if host else []
    candidates += ["total", record.get("backend"), record.get("provider")]
    for key in candidates:
        if key and str(key) not in keys:
            keys.append(str(key))
    return keys


def ghost_reservations(reservations, runs_by_id, known_hosts, now,
                        ttl_s=RECOVERY_TTL_S, grace_s=GHOST_GRACE_S):
    """The reservations of runs that ended and left a stale slot behind.

    A reservation is only a ghost when there is evidence that its run is over
    *and* that nothing of it runs any more: the run record is terminal and its
    executor is stopped.  A run whose executor still lives is not a ghost (the
    slot is in use).

    The second return value are the slots that cannot be classified: the run
    ended, but the host did not prove that its executor stopped.  They may be
    stale, so a name based on them would be a guess: the verdict of the waiting
    run stays ``unknown``.  A reservation whose run the host does not report at
    all has insufficient evidence and is classified as uncertain.
    """
    ghosts, uncertain = [], []
    for reservation in reservations or []:
        run_id = str(reservation.get("run_id") or "")
        host = reservation.get("host")
        created = epoch(reservation.get("created"))
        age = None if created is None else max(0.0, float(now) - created)
        record = runs_by_id.get(run_id)
        if record is not None:
            if record.get("state") not in TERMINAL_STATES:
                continue  # the run is alive: its reservation is not a ghost
            evidence = liveness(record)
            if evidence == LIVENESS_ALIVE:
                continue  # the executor still runs: the slot is in use
            if evidence == LIVENESS_UNKNOWN:
                uncertain.append({**reservation, "ghost_evidence": "executor-unknown",
                                  "age_s": age})
                continue
            kind = "ended"
        else:
            # Absence from a snapshot does not prove the run or executor stopped,
            # even when the host was reachable and the reservation is old.
            uncertain.append({**reservation, "ghost_evidence": "record-missing",
                              "age_s": age})
            continue
        if age is not None and age < grace_s:
            continue  # the release may still be in flight
        ghosts.append({**reservation, "ghost_evidence": kind, "age_s": age})
    return ghosts, uncertain


# ----------------------------------------------------------------- judgement


def _cap(limits, key):
    """The cap of a counter, None when the counter is not limited."""
    value = limits.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _key_label(key, record):
    """The label of an admission counter for the detail line."""
    host = record.get("host")
    if key == "total":
        return "全舰队预留"
    if host and key == "host:" + str(host):
        return f"{host} 主机预留"
    backend = record.get("backend")
    if backend and key == str(backend):
        return f"全舰队 后端 {key} 用量"
    return f"全舰队 provider {key} 用量"


def queued_runs(records, now, min_age_s=MIN_QUEUE_AGE_S):
    """The runs that are queued and wait longer than ``min_age_s``.

    A run of unknown age is left out: without an age there is no evidence that
    it waits longer than the threshold.
    """
    waiting = []
    for record in records or []:
        if record.get("state") != "queued":
            continue
        started = epoch(record.get("started"))
        if started is None:
            continue
        age = float(now) - started
        if age < min_age_s:
            continue
        waiting.append({**record, "queued_s": age})
    waiting.sort(key=lambda row: -row["queued_s"])
    return waiting


def judge(record, context):
    """The blocking verdict of one queued run.

    The layers are judged in the order of the enum: a ghost reservation that
    fills the admission capacity first (releasing it is the fix), then the
    host cap of the topology, then the fleet wide budget, then the local
    policy of the host.  A run that already holds a reservation was admitted
    by the coordinator, so the admission limits never block it.
    """
    limits = context["limits"]
    reservations = context["reservations"]
    host = record.get("host")
    run_id = record.get("run_id")
    own = [row for row in reservations if str(row.get("run_id") or "") == str(run_id)]
    reserved = bool(own)
    ghosts = context["ghosts"]
    uncertain = context["uncertain"]
    ghost_ids = {str(row.get("run_id") or "") for row in ghosts}
    uncertain_ids = {str(row.get("run_id") or "") for row in uncertain}
    counts = usage_counts(reservations, exclude_run=run_id)
    without_ghosts = usage_counts(reservations, exclude_run=run_id, exclude_ids=ghost_ids)
    without_uncertain = usage_counts(reservations, exclude_run=run_id,
                                      exclude_ids=ghost_ids | uncertain_ids)
    keys = admission_keys(record)
    numbers = {
        "host_reservations": counts.get("host:" + str(host), 0) if host else None,
        "host_limit": _cap(limits, "host:" + str(host)) if host else None,
        "fleet_total": counts.get("total", 0),
        "fleet_total_limit": _cap(limits, "total"),
        "backend_usage": counts.get(str(record.get("backend")), 0) if record.get("backend") else None,
        "backend_limit": _cap(limits, str(record.get("backend"))) if record.get("backend") else None,
        "provider_usage": counts.get(str(record.get("provider")), 0) if record.get("provider") else None,
        "provider_limit": _cap(limits, str(record.get("provider"))) if record.get("provider") else None,
        "host_running": (context["local"].get(host, {}).get("total") if host else None),
        "host_policy_limit": None,
        "reserved": reserved,
    }

    if not reserved:
        # 1) a ghost reservation fills the admission capacity of this run and
        #    releasing it lifts the limit: releasing it is the fix.
        for key in keys:
            limit = _cap(limits, key)
            if limit is None:
                continue
            used = counts.get(key, 0)
            if used < limit:
                continue
            blockers = [row for row in ghosts if key in _reservation_keys(row)]
            if not blockers or without_ghosts.get(key, 0) >= limit:
                continue
            names = ", ".join(str(row.get("run_id") or "?") for row in blockers)
            detail = (f"{_key_label(key, record)} {used}/{limit} 已满，"
                       f"其中 {len(blockers)} 条为已结束 run 残留（{names}）")
            return {"reason": REASON_GHOST, "detail": detail, "reserved": reserved,
                    "ghost_runs": [str(row.get("run_id") or "?") for row in blockers],
                    "numbers": numbers}
        # 2) a slot fills the capacity but its staleness is not proven: the
        #    name would be a guess, so the verdict stays unknown.
        for key in keys:
            limit = _cap(limits, key)
            if limit is None:
                continue
            used = counts.get(key, 0)
            if used < limit:
                continue
            blockers = [row for row in uncertain if key in _reservation_keys(row)]
            if not blockers or without_uncertain.get(key, 0) >= limit:
                continue
            names = ", ".join(str(row.get("run_id") or "?") for row in blockers)
            detail = (f"{_key_label(key, record)} {used}/{limit} 已满，"
                       f"其中 {len(blockers)} 条残留 run 的存活证据不足（{names}），需人工确认")
            return {"reason": REASON_UNKNOWN, "detail": detail, "reserved": reserved,
                    "ghost_runs": [], "numbers": numbers}
        # 3) the host cap of the coordinator topology.
        for key in keys:
            limit = _cap(limits, key)
            if limit is None:
                continue
            used = counts.get(key, 0)
            if used < limit:
                continue
            if key.startswith("host:"):
                detail = f"{_key_label(key, record)} {used}/{limit} 已满"
                lanes = context["lane_counts"].get(host)
                if lanes is not None and lanes > limit:
                    detail += f"；topology 主机上限 {limit} 小于 {host} 的 lane 数 {lanes}，疑似旧值"
                return {"reason": REASON_HOST_CAP, "detail": detail, "reserved": reserved,
                        "ghost_runs": [], "numbers": numbers}
            # 4) the fleet wide budget: the total, the backend or the provider.
            return {"reason": REASON_FLEET_CAP,
                    "detail": f"{_key_label(key, record)} {used}/{limit} 已满",
                    "reserved": reserved, "ghost_runs": [], "numbers": numbers}

    # 5) the local policy of the target host.
    caps = context["policies"].get(host) or {}
    running = context["local"].get(host, {})
    for key in _run_keys(record):
        limit = caps.get(key)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            continue
        used = running.get(key, 0)
        numbers["host_running"] = used
        numbers["host_policy_limit"] = limit
        if used >= limit:
            detail = f"{host} 本机 policy 并发 {used}/{limit} 已满（{key}）"
            return {"reason": REASON_LOCAL_POLICY, "detail": detail, "reserved": reserved,
                    "ghost_runs": [], "numbers": numbers}

    # 6) no layer matched: the reason is not guessed.
    if reserved:
        detail = (f"{host or '未知主机'} 的 run 已持有协调器预留（准入已通过），"
                  "阻塞不在准入层，需人工排查")
    else:
        detail = f"{host or '未知主机'} 未命中任何已知上限，需人工排查"
    return {"reason": REASON_UNKNOWN, "detail": detail, "reserved": reserved,
            "ghost_runs": [], "numbers": numbers}


def diagnose(runs, reservations=None, limits=None, policies=None, known_hosts=None,
             lane_counts=None, now=None, min_age_s=MIN_QUEUE_AGE_S,
             ttl_s=RECOVERY_TTL_S, grace_s=GHOST_GRACE_S, coordinator_error=None):
    """The queued diagnosis section of the health check.

    ``runs``
        The run records of every host (see ``records_from_states`` and
        ``records_from_snapshot``).
    ``reservations``
        The reservations of the capacity coordinator.
    ``limits``
        The limits of the coordinator topology (``total``, ``host:<host>``,
        the backends and the providers).
    ``policies``
        The local concurrency caps per host (``{host: {key: cap}}``).
    ``known_hosts``
        The hosts whose run list is complete *and* whose snapshot carries the
        liveness evidence; only they can prove a ghost.
    ``lane_counts``
        The number of lanes per host, to spot a host limit that is too old.

    The section holds one row per run that waits longer than ``min_age_s``,
    the number of runs per verdict and the hint for a human.
    """
    moment = time.time() if now is None else float(now)
    records = [row for row in (runs or []) if isinstance(row, dict)]
    reservations = list(reservations or [])
    limits = dict(limits or {})
    policies = dict(policies or {})
    known_hosts = set(known_hosts or ())
    lane_counts = dict(lane_counts or {})
    runs_by_id = {}
    for record in records:
        runs_by_id[str(record.get("run_id") or "")] = record
    ghosts, uncertain = ghost_reservations(reservations, runs_by_id, known_hosts, moment,
                                           ttl_s, grace_s)
    local = {}
    for record in records:
        host = record.get("host")
        if not host or record.get("state") != "running":
            continue
        counts = local.setdefault(host, {})
        for key in _run_keys(record):
            counts[key] = counts.get(key, 0) + 1
    context = {"now": moment, "limits": limits, "reservations": reservations,
               "policies": policies, "ghosts": ghosts, "uncertain": uncertain,
               "local": local, "lane_counts": lane_counts}
    rows = []
    for record in queued_runs(records, moment, min_age_s):
        verdict = judge(record, context)
        rows.append({
            "run_id": record.get("run_id"),
            "host": record.get("host"),
            "lane": record.get("lane"),
            "backend": record.get("backend"),
            "provider": record.get("provider"),
            "queued_minutes": round(record["queued_s"] / 60.0, 1),
            "queue_reason": record.get("queue_reason"),
            "reason": verdict["reason"],
            "detail": verdict["detail"],
            "reserved": verdict["reserved"],
            "ghost_runs": verdict["ghost_runs"],
            "numbers": verdict.get("numbers") or {},
        })
    by_reason = {reason: 0 for reason in REASONS}
    for row in rows:
        by_reason[row["reason"]] += 1
    unknown = by_reason[REASON_UNKNOWN]
    total = len(rows)
    ratio = (unknown / total) if total else 0.0
    needs_human = bool(total) and unknown > 0 and ratio >= UNKNOWN_HUMAN_RATIO
    note = ""
    if needs_human:
        note = (f"{unknown}/{total} 条 queued run 的阻塞原因未知（占比 {ratio:.0%}），"
                "需人工排查（本节不臆测原因）")
    section = {
        "at": moment,
        "min_age_s": min_age_s,
        "queued": total,
        "by_reason": by_reason,
        "unknown": unknown,
        "unknown_ratio": round(ratio, 3),
        "needs_human": needs_human,
        "note": note,
        "ghosts": [{"host": ghost.get("host"), "run_id": ghost.get("run_id"),
                    "evidence": ghost.get("ghost_evidence"),
                    "age_minutes": round(ghost["age_s"] / 60.0, 1) if ghost.get("age_s") is not None else None}
                   for ghost in ghosts],
        "residuals": [{"host": row.get("host"), "run_id": row.get("run_id"),
                       "evidence": row.get("ghost_evidence"),
                       "age_minutes": round(row["age_s"] / 60.0, 1) if row.get("age_s") is not None else None}
                      for row in uncertain],
        "runs": rows,
    }
    if coordinator_error:
        section["coordinator_error"] = str(coordinator_error)
    return section


# ------------------------------------------------------------------- output


def brain_log_lines(section, now=None):
    """The brain log lines of a queued diagnosis section."""
    moment = time.time() if now is None else float(now)
    stamp = time.strftime("%H:%M", time.localtime(moment))
    rows = section.get("runs") or []
    lines = []
    for row in rows:
        lines.append(f"- {stamp} UTC+8 queued: {row.get('run_id')} {row.get('reason')} {row.get('detail')}")
    if not lines:
        return []
    counts = ", ".join(f"{reason}={count}" for reason, count in (section.get("by_reason") or {}).items() if count)
    lines.append(f"- {stamp} UTC+8 queued: {section.get('queued', len(rows))} 条 queued run 超过 "
                 f"{int((section.get('min_age_s') or MIN_QUEUE_AGE_S) / 60)} 分钟；{counts}")
    note = section.get("note")
    if note:
        lines.append(f"- {stamp} UTC+8 queued: {note}")
    return lines


def write_health_section(path, section):
    """Write the queued section into a health.json snapshot.

    The other fields of the snapshot belong to the health check and are kept.
    A tick without a blocked run removes the section again, so a stale
    diagnosis never survives in the file.  The file is only written when its
    content changes.
    """
    target = os.fspath(path)
    payload = {}
    try:
        with open(target, encoding="utf-8") as stream:
            loaded = json.load(stream)
        if isinstance(loaded, dict):
            payload = loaded
    except (OSError, ValueError, UnicodeError):
        payload = {}
    if section.get("runs"):
        payload["queued"] = section
    else:
        payload.pop("queued", None)
    text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
    try:
        with open(target, encoding="utf-8") as stream:
            if stream.read() == text:
                return False
    except (OSError, UnicodeError):
        pass
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as stream:
        stream.write(text)
    return True


def append_brain_log(path, section):
    """Append the queued diagnosis to the brain log; no line without a run."""
    lines = brain_log_lines(section)
    if not lines:
        return 0
    parent = os.path.dirname(os.path.abspath(os.fspath(path)))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(os.fspath(path), "a", encoding="utf-8") as stream:
        for line in lines:
            stream.write(line + "\n")
    return len(lines)


# ---------------------------------------------------------------------- CLI


def _policy_argument(value):
    """Split a ``HOST=FILE`` policy argument into its host and its file."""
    if "=" in value:
        host, _, path = value.partition("=")
        if host and path:
            return host, path
    return "local", value


def main(argv=None):
    parser = argparse.ArgumentParser(prog="queued.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", action="append", default=[], metavar="DIR",
                        help="runs directory to scan (repeatable)")
    parser.add_argument("--host", default="local",
                        help="host the scanned runs belong to (default local)")
    parser.add_argument("--reservations", metavar="FILE",
                        help="coordinator status answer (JSON) with the reservations")
    parser.add_argument("--topology", metavar="FILE",
                        help="coordinator topology.json with the limits")
    parser.add_argument("--policy", action="append", default=[], metavar="[HOST=]FILE",
                        help="local policy.json with the concurrency caps (repeatable)")
    parser.add_argument("--lanes", metavar="FILE", help="lanes.json with the lane hosts")
    parser.add_argument("--health-json", metavar="FILE", help="health.json to write the section into")
    parser.add_argument("--brain-log", metavar="FILE", help="brain log to append the section to")
    parser.add_argument("--min-age", type=float, default=MIN_QUEUE_AGE_S,
                        help="queued age in seconds that counts as blocked (default 600)")
    parser.add_argument("--now", type=float, default=None,
                        help="treat this epoch as now (to replay a recorded sample)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print the section as JSON")
    args = parser.parse_args(argv)

    records, known_hosts = [], set()
    lane_host_map, lane_counts = {}, {}
    for directory in args.runs:
        states = load_run_states(directory)
        if not states:
            continue
        known_hosts.add(args.host)
        records.extend(records_from_states(states, host=args.host, lane_hosts_map=lane_host_map))
    if args.lanes:
        lanes = _read_json(args.lanes)
        lane_host_map, lane_counts = lane_hosts(lanes)
    policies = {}
    for value in args.policy:
        host, path = _policy_argument(value)
        policy = _read_json(path)
        caps = policy_caps(policy)
        if caps:
            policies[host] = caps
    answer = _read_json(args.reservations) if args.reservations else None
    topology = _read_json(args.topology) if args.topology else None
    reservations = load_reservations(answer)
    limits = load_limits(answer, topology)
    section = diagnose(records, reservations=reservations, limits=limits, policies=policies,
                       known_hosts=known_hosts, lane_counts=lane_counts, min_age_s=args.min_age,
                       now=args.now)
    if args.health_json:
        write_health_section(args.health_json, section)
    if args.brain_log:
        append_brain_log(args.brain_log, section)
    if args.as_json:
        print(json.dumps(section, indent=2, ensure_ascii=False))
    else:
        for row in section["runs"]:
            print(f"{row['run_id']}  {row['reason']:<28} {row['detail']}")
        if section["note"]:
            print(section["note"])
        if not section["runs"]:
            print("no queued run waits longer than %gs" % section["min_age_s"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
