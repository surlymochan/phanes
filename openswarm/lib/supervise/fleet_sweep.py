"""Fleet capacity ghost sweeper: reservations whose run is gone (Refs #1685).

The fleet capacity coordinator (``bin/fleet-capacity``) holds a reservation
until it is released explicitly, and a crash never reclaims it.  A run that
ends abnormally therefore leaves its slot behind: two placeholder reservations
of a cancelled worker were still holding host slots 20 hours later, and a run
that ended with ``exec_error`` blocked the next run of the same host for 23
minutes.  This module judges which reservations are ghosts.

``load_run(runs_root, run_id)``
    The evidence of one run: whether its directory exists, whether it has a
    readable status file, and the status itself.

``classify(reservation, run, now, grace_s=GRACE_SECONDS)``
    The decision for one reservation: ``release`` or ``keep``, plus the
    ``basis`` that produced it and the age of the reservation.  A run that is
    still alive is never released.  A release needs positive evidence:

    * ``run-missing`` - the run directory is gone (a placeholder reservation);
    * ``run-finished`` - the run published a terminal state and ended more
      than the grace ago;
    * ``runner-dead-stale`` - the runner process is gone *and* the status file
      has not been updated for longer than the grace.

    Everything else is kept: a live runner, a fresh status file, a missing or
    unreadable status file, a reservation that is younger than the grace, a
    run whose executor may have left an unrecorded detached child
    (``executor-scan-stale``) and a run without a runner to probe
    (``runner-unknown``).  When the evidence is incomplete the reservation
    stays.

``sweep(reservations, runs_root, now, grace_s)``
    The decisions for every reservation, in one pass.

The age of a reservation comes from the coordinator (``created``); the age of a
run comes from its status file (``finished`` for a terminal run, ``updated``
otherwise).  The clock and the liveness probe are injectable, so the decision
is reproducible offline.

Standard library only; Python 3.9+.
"""
import argparse
import json
import os
import sys
import time

__all__ = ["GRACE_SECONDS", "classify", "load_run", "main", "sweep"]

# A finished run keeps its slot for this many seconds, so a run that is still
# finalizing (or a status file that is still being written) is never mistaken
# for a ghost.
GRACE_SECONDS = 600

# The run states that mean the run is over.  A run that reached one of them
# published its result; its slot may be reclaimed once the grace passed.
TERMINAL_STATES = ("finished", "failed", "dead", "unroutable", "dry-run",
                    "budget_rejected", "stopped")

RELEASE = "release"
KEEP = "keep"


def pid_alive(pid):
    """Whether a process id is still running (best effort, never raises)."""
    if not pid or isinstance(pid, bool):
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError, OverflowError):
        return False


def _epoch(value):
    """A POSIX timestamp for a number, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def load_run(runs_root, run_id):
    """The evidence of one run: the directory, the status file and the status.

    A missing directory or an unreadable status file is reported as such; the
    caller decides what that means.
    """
    empty = {"exists": False, "status_exists": False, "status": None, "run_dir": None}
    if not isinstance(run_id, str) or not run_id or os.sep in run_id or run_id in (".", ".."):
        return empty
    run_dir = os.path.join(runs_root, run_id)
    if not os.path.isdir(run_dir):
        return empty
    status_path = os.path.join(run_dir, "status.json")
    if not os.path.isfile(status_path):
        return {"exists": True, "status_exists": False, "status": None, "run_dir": run_dir}
    try:
        with open(status_path, encoding="utf-8") as stream:
            status = json.load(stream)
    except (OSError, ValueError):
        return {"exists": True, "status_exists": False, "status": None, "run_dir": run_dir}
    if not isinstance(status, dict):
        return {"exists": True, "status_exists": False, "status": None, "run_dir": run_dir}
    return {"exists": True, "status_exists": True, "status": status, "run_dir": run_dir}


def classify(reservation, run, now, grace_s=GRACE_SECONDS, alive=None):
    """Decide what to do with one reservation.

    ``reservation`` is a row of the coordinator (``host``, ``run_id``,
    ``backend``, ``provider``, ``created``); ``run`` is the evidence of
    ``load_run``.  The answer is ``{"action", "basis", "age_s", "run_id",
    "host"}``: ``action`` is ``release`` or ``keep``.
    """
    alive = alive or pid_alive
    reservation = reservation if isinstance(reservation, dict) else {}
    run = run if isinstance(run, dict) else {}
    run_id = reservation.get("run_id")
    host = reservation.get("host")
    created = _epoch(reservation.get("created"))
    age = None if created is None else max(0.0, now - created)
    decision = {"action": KEEP, "basis": "unknown", "age_s": age, "run_id": run_id, "host": host}

    if age is not None and age < grace_s:
        # A reservation that was just taken cannot be a ghost yet.
        decision["basis"] = "reservation-fresh"
        return decision
    if not run.get("exists"):
        # The run directory is gone: the reservation is a placeholder.
        decision.update(action=RELEASE, basis="run-missing")
        return decision
    if not run.get("status_exists") or not isinstance(run.get("status"), dict):
        # No readable status: the evidence is incomplete, so nothing is touched.
        decision["basis"] = "status-unreadable"
        return decision

    status = run["status"]
    state = status.get("state")
    updated = _epoch(status.get("updated"))
    finished = _epoch(status.get("finished"))
    runner_pid = status.get("runner_pid")
    runner_alive = alive(runner_pid)
    if runner_alive:
        # A stale terminal marker can race a child that has not exited yet.
        # Process liveness wins; uncertainty always keeps the reservation.
        decision["basis"] = "runner-alive"
        return decision
    if status.get("executor_scan_stale"):
        # The executor exited long after the last descendant scan, so an
        # unrecorded detached child may still be alive: the slot stays.
        decision["basis"] = "executor-scan-stale"
        return decision
    if state in TERMINAL_STATES:
        ended = finished if finished is not None else updated
        if ended is None:
            decision["basis"] = "terminal-without-timestamp"
            return decision
        if now - ended > grace_s:
            decision.update(action=RELEASE, basis="run-finished")
            return decision
        decision["basis"] = "run-finished-grace"
        return decision
    if updated is None:
        decision["basis"] = "status-without-timestamp"
        return decision
    if now - updated <= grace_s:
        # The status file is still fresh: the run may only be starting up.
        decision["basis"] = "status-fresh"
        return decision
    if not isinstance(runner_pid, int) or isinstance(runner_pid, bool) or runner_pid <= 0:
        # No runner to probe: the run may still be alive somewhere else.
        decision["basis"] = "runner-unknown"
        return decision
    # The runner is gone and the status file is stale: the run is over.
    decision.update(action=RELEASE, basis="runner-dead-stale")
    return decision


def sweep(reservations, runs_root, now=None, grace_s=GRACE_SECONDS, alive=None):
    """The decisions for every reservation, in one pass."""
    now = time.time() if now is None else now
    rows = []
    for reservation in reservations or []:
        if not isinstance(reservation, dict):
            continue
        run = load_run(runs_root, reservation.get("run_id"))
        rows.append(classify(reservation, run, now, grace_s, alive=alive))
    return rows


def _read_reservations(path):
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    if isinstance(data, dict):
        data = data.get("reservations") or []
    if not isinstance(data, list):
        raise ValueError("reservations file must hold a list or a {'reservations': [...]} object")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline judgement of fleet capacity reservations (Refs #1685).")
    parser.add_argument("--runs", required=True, help="runs directory of the host")
    parser.add_argument("--reservations", required=True, help="JSON file with the coordinator reservations")
    parser.add_argument("--now", type=float, default=None, help="override 'now' (epoch seconds)")
    parser.add_argument("--grace-seconds", type=float, default=GRACE_SECONDS,
                        help=f"grace of a finished run (default: {GRACE_SECONDS})")
    parser.add_argument("--json", action="store_true", help="print the decisions as JSON")
    args = parser.parse_args(argv)
    try:
        reservations = _read_reservations(args.reservations)
    except (OSError, ValueError) as error:
        sys.stderr.write(f"cannot read the reservations: {error}\n")
        return 1
    rows = sweep(reservations, args.runs, now=args.now, grace_s=args.grace_seconds)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for row in rows:
        print(f"{row['action']:<7} {row['host']}/{row['run_id']} "
              f"(age {int(row['age_s']) if row['age_s'] is not None else '?'}s) - {row['basis']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
