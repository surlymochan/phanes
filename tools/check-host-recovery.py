#!/usr/bin/env python3
"""Offline contract: a host that comes back releases its hold and checks its runs (Refs #1673).

The 2026-11-02 incident: the box was unreachable twice.  While it was gone the
control plane wrote ``PAUSE-HOST-box``; when the box came back nobody released
the hold, so the host stayed paused, and the run that was in flight (M6, issue
#1361) was lost for 44 minutes before a human noticed.

This check replays that scenario against the shipped control loop
(``tools/lane-autodispatch/autodispatch.py``) with fixtures only: the ssh probe,
the GitHub calls and the clock are replaced, so nothing outside the temporary
directory is read, written or removed.

  * the tick observes the reachability of every host (it probes them over ssh
    anyway) and acts on the down -> up edge;
  * an automatic hold that the unreachable host caused is released when the host
    answers again -- the recovery edge is the release condition, no pause TTL is
    waited for;
  * a manual hold, a machine cooldown, a disk/memory hold and an automatic hold
    of an unknown cause survive: the host answering ssh again says nothing about
    them, and the write time of a hold says nothing about its cause;
  * a run that was lost goes back to the queue with the labels of the orphan
    flow (``in-progress`` removed, ``lane-ready`` added), a run that survived
    keeps its lane;
  * a failed label edit keeps the lane record, so the issue is never lost: a
    later tick retries the requeue;
  * every recovery is appended to ``host-recoveries.jsonl``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"

# 2026-11-02 00:00 UTC, the day of the incident.
MIDNIGHT = 1793577600.0
# The first outage: 08:12 -> 08:44 (the box was unreachable for 32 minutes).
DOWN_AT = MIDNIGHT + 8 * 3600 + 12 * 60
UP_AT = MIDNIGHT + 8 * 3600 + 44 * 60
# The dispatch stamp of the run that was in flight (M6, #1361).
DISPATCHED_AT = MIDNIGHT + 8 * 3600 + 5 * 60


def stamp(moment: float) -> str:
    """Return the control plane dispatch stamp of a moment."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment))


def load_runtime():
    """Load the control loop of the box (the runtime copy of the repo mirror)."""
    spec = importlib.util.spec_from_file_location("autodispatch_runtime", RUNTIME)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {RUNTIME}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(name: str, condition: bool) -> None:
    """Assert one contract of the recovery."""
    if not condition:
        raise AssertionError(name)


class Fixture:
    """A fake autodispatch state dir; every write stays inside it."""

    def __init__(self, runtime, root: Path) -> None:
        self.runtime = runtime
        self.root = root.resolve()
        self.state = self.root / "autodispatch"
        self.state.mkdir(parents=True)
        self.calls: list[list[str]] = []
        self.edit_fails = False
        self.now = DOWN_AT

    # -- fixtures ---------------------------------------------------------
    def reset_state(self) -> None:
        """Write the control plane state of the incident again (and forget the calls)."""
        self.calls.clear()
        log_file = self.state / "host-recoveries.jsonl"
        if log_file.exists():
            log_file.unlink()
        (self.state / "state.json").write_text(json.dumps({
            "M6": {"issue": 1361, "run": "run-1361", "host": "box", "base": "abc123",
                   "at": stamp(DISPATCHED_AT)},
            "M7": {"issue": 1362, "run": "run-1362", "host": "ib1", "base": "abc123",
                   "at": stamp(DISPATCHED_AT)},
            "_done": [1350],
        }, indent=1) + "\n", encoding="utf-8")

    def write_hold(self, body: str, host: str = "box") -> Path:
        """Write a PAUSE-HOST marker in the fake state dir."""
        path = self.state / ("PAUSE-HOST-" + host)
        path.write_text(body, encoding="utf-8")
        return path

    def hold_exists(self, host: str = "box") -> bool:
        """Whether the fake state dir still holds the host."""
        return (self.state / ("PAUSE-HOST-" + host)).exists()

    def state_data(self) -> dict:
        """Read the control plane state of the fixture."""
        return json.loads((self.state / "state.json").read_text(encoding="utf-8"))

    # -- fakes ------------------------------------------------------------
    def gh(self, args, timeout=60):
        """Record the GitHub calls; answer the issue reads.

        `edit_fails` makes the label edit fail, the way a broken GitHub call does."""
        self.calls.append(list(args))
        if args[:2] == ["issue", "view"]:
            return 0, json.dumps({"state": "OPEN", "labels": [{"name": "in-progress"}]}), ""
        if args[:2] == ["issue", "edit"] and self.edit_fails:
            return 1, "", "gh: could not add label: HTTP 502"
        return 0, "", ""

    def install(self):
        """Replace the state dir, the logger, GitHub and the clock."""
        runtime = self.runtime
        runtime.D = str(self.state)
        runtime.log = lambda *args, **kwargs: None
        runtime.gh = self.gh
        runtime.time.time = lambda: self.now
        runtime.BACKOFF.clear()

    def tick(self, box_probe, ib1_probe=None) -> None:
        """One tick of the control loop with a fixed ssh probe per host.

        The lane of the other host (ib1) stays busy: the recovery must not touch
        the runs of a host that did not go away."""
        runtime = self.runtime
        other = ib1_probe or {"lane": "busy", "current_run": "run-1362", "run": "running",
                              "runner_alive": True}

        def probe(lane, cfg, run=""):
            return box_probe if cfg["host"] == "box" else other

        with mock.patch.object(runtime, "remote", side_effect=probe), \
             mock.patch.object(runtime, "queue", return_value=[]), \
             mock.patch.object(runtime, "replenish", return_value=[]):
            lanes = {"M6": {"host": "box", "backend": "pi"}, "M7": {"host": "ib1", "backend": "pi"}}
            runtime.tick(lanes, runtime.load(f"{runtime.D}/state.json", {}), 0)

    def edits(self) -> list[list[str]]:
        """The label edits the control loop made."""
        return [call for call in self.calls if call[:2] == ["issue", "edit"]]


def main() -> None:
    runtime = load_runtime()
    # the fixture lives beside the script: the check never writes outside the repository
    with tempfile.TemporaryDirectory(prefix="ad-host-recovery-", dir=Path(__file__).resolve().parent) as raw_dir:
        fixture = Fixture(runtime, Path(raw_dir))
        fixture.install()
        fixture.reset_state()

        # 1) the incident: the box goes down, the control plane writes the hold,
        #    and the box comes back.
        fixture.write_hold("auto: ssh streak 3\n")
        fixture.now = DOWN_AT
        fixture.tick({"error": "ssh: connect to host box port 22: No route to host"})
        check("the outage is observed", fixture.state_data()["_hosts"]["box"]["reachable"] is False)
        fixture.now = UP_AT
        fixture.tick({"lane": "idle", "current_run": None, "run": "running", "runner_alive": False})
        check("the automatic hold is released", not fixture.hold_exists())
        edits = fixture.edits()
        check("the lost run is requeued", len(edits) == 1)
        check("in-progress is removed", "in-progress" in edits[0])
        check("lane-ready is added", "lane-ready" in edits[0])
        state = fixture.state_data()
        check("the lost lane is released", "M6" not in state)
        check("the lane of another host is untouched", "M7" in state)
        check("the bookkeeping survives", state["_done"] == [1350])
        print("OK: a recovered host releases its automatic hold and requeues the lost run")

        # 2) the recovery is recorded in the state dir
        records = [json.loads(line) for line in
                   (fixture.state / runtime.RECOVERY_LOG).read_text(encoding="utf-8").splitlines()]
        check("the recovery is logged", len(records) == 1)
        check("the recovery names the host", records[0]["host"] == "box")
        check("the recovery names the moment", records[0]["recovered_at"] == UP_AT)
        print("OK: every recovery is appended to host-recoveries.jsonl")

        # 3) a manual hold outlives the outage
        fixture.reset_state()
        fixture.write_hold("operator hold\n")
        fixture.now = DOWN_AT
        fixture.tick({"error": "ssh: connect to host box port 22: No route to host"})
        fixture.now = UP_AT
        fixture.tick({"lane": "idle", "current_run": None, "run": "running", "runner_alive": False})
        check("a manual hold survives", fixture.hold_exists())
        print("OK: a manual PAUSE-HOST hold is never released by the recovery")

        # 4) a disk hold belongs to the health path: ssh says nothing about the disk
        fixture.reset_state()
        fixture.write_hold("auto: disk_hard\n")
        fixture.now = DOWN_AT
        fixture.tick({"error": "ssh: connect to host box port 22: No route to host"})
        fixture.now = UP_AT
        fixture.tick({"lane": "idle", "current_run": None, "run": "running", "runner_alive": False})
        check("a disk hold survives", fixture.hold_exists())
        print("OK: a disk hold is kept when the host answers again")

        # 5) a machine cooldown has its own deadline
        fixture.reset_state()
        fixture.write_hold(f"auto:cooldown:until={UP_AT + 600}\n")
        fixture.now = DOWN_AT
        fixture.tick({"error": "ssh: connect to host box port 22: No route to host"})
        fixture.now = UP_AT
        fixture.tick({"lane": "idle", "current_run": None, "run": "running", "runner_alive": False})
        check("a cooldown hold survives", fixture.hold_exists())
        print("OK: an auto:cooldown hold is kept")

        # 6) a run that survived the outage keeps its lane
        fixture.reset_state()
        fixture.now = DOWN_AT
        fixture.tick({"error": "ssh: connect to host box port 22: No route to host"})
        fixture.now = UP_AT
        fixture.tick({"lane": "busy", "current_run": "run-1361", "run": "running", "runner_alive": True})
        check("a surviving run keeps its lane", "M6" in fixture.state_data())
        check("a surviving run is not requeued", fixture.edits() == [])
        print("OK: a run that survived the outage keeps its lane")

        # 7) the second outage of the incident is detected on its own
        fixture.reset_state()
        fixture.now = DOWN_AT
        fixture.tick({"error": "ssh: connect to host box port 22: No route to host"})
        fixture.now = UP_AT
        fixture.tick({"lane": "idle", "current_run": None, "run": "running", "runner_alive": False})
        fixture.now = UP_AT + 60
        fixture.tick({"error": "ssh: connect to host box port 22: No route to host"})
        fixture.now = UP_AT + 3600
        fixture.tick({"lane": "idle", "current_run": None, "run": "running", "runner_alive": False})
        records = [json.loads(line) for line in
                   (fixture.state / runtime.RECOVERY_LOG).read_text(encoding="utf-8").splitlines()]
        check("every down -> up edge is handled", len(records) == 2)
        check("the second outage is measured on its own", records[1]["down_s"] == 3540)
        print("OK: every down -> up edge is handled on its own")

        # 8) an automatic hold of an unknown cause is not ours to release: the host answering
        #    ssh again only cures the holds it caused
        fixture.reset_state()
        fixture.write_hold("auto: unknown reason\n")
        fixture.now = DOWN_AT
        fixture.tick({"error": "ssh: connect to host box port 22: No route to host"})
        fixture.now = UP_AT
        fixture.tick({"lane": "idle", "current_run": None, "run": "running", "runner_alive": False})
        check("an unknown hold survives", fixture.hold_exists())
        print("OK: an automatic hold of an unknown cause is kept")

        # 9) a failed label edit keeps the lane: the issue is never lost, a later tick retries
        fixture.reset_state()
        fixture.edit_fails = True
        fixture.now = DOWN_AT
        fixture.tick({"error": "ssh: connect to host box port 22: No route to host"})
        fixture.now = UP_AT
        fixture.tick({"lane": "idle", "current_run": None, "run": "running", "runner_alive": False})
        check("the lane survives a failed label edit", "M6" in fixture.state_data())
        fixture.edit_fails = False
        fixture.now = UP_AT + 60
        fixture.tick({"lane": "idle", "current_run": None, "run": "running", "runner_alive": False})
        check("the retry releases the lane", "M6" not in fixture.state_data())
        print("OK: a failed label edit keeps the lane and a later tick retries the requeue")

    print("host recovery detection: OK")


if __name__ == "__main__":
    main()
