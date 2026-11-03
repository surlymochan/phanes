#!/usr/bin/env python3
"""Tick-level contract check for the lane idle gap of the autodispatcher (Refs #1688).

A lane that finished a run is idle until it gets its next card.  When the pool is not
empty and no capacity is in the way, that gap is pure scheduling delay: the dispatcher
therefore hands the lane its next card right after the harvest instead of waiting for
the next tick, and it measures the gap so a long one can be explained.

The check drives the real ``tick()`` of the autodispatcher against a fake fleet (no
network, no ssh, no GitHub) and covers:

  * the run ends -> the same lane gets its next card in the very same tick;
  * a lane that was paused while its run was running gets no card (the re-dispatch
    runs the same pause and host checks as the ordinary dispatch);
  * no card in the pool -> no dispatch, no error, no retry loop;
  * the idle metric starts at the *real* end of the run (a harvest that came late does
    not shorten the gap), and a gap of more than five minutes is written to the brain
    log with its reason;
  * the dispatcher downtime is counted inside the idle window, and the window of the
    next gap starts empty: the downtime of one gap never explains the next one.

Run it offline:

    python3 tools/check-ad-idle-redispatch.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import tempfile
import time as time_module
import unittest
import warnings
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"
SCRATCH = Path(os.environ.get("TMPDIR") or (ROOT / ".openswarm-tmp"))
LANE = "GH2"
HOST = "lane-host"
START = 1_800_000_000.0  # a fixed clock: the idle gaps are measured against it
TICK_S = 60.0            # the dispatch cadence of the fleet


def load_module(name="autodispatch_idle"):
    """Load a fresh copy of the autodispatcher (every test patches its own copy)."""
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    """The clock of the test: it only moves when the test moves it."""

    def __init__(self, start=START):
        self.now = float(start)

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now

    def stamp(self, offset=0.0):
        """The heartbeat stamp of the clock (the format the dispatcher writes)."""
        return time_module.strftime("%Y-%m-%d %H:%M:%S", time_module.localtime(self.now + offset))

    def __getattr__(self, name):
        return getattr(time_module, name)


class Sandbox:
    """A fake fleet: the issue table, the `gh` calls, the ssh shell and the lane states."""

    def __init__(self, root: Path, clock: Clock):
        self.root = root
        self.clock = clock
        self.issues = {
            42: {"title": "#42 the first card", "body": "", "labels": ["lane-ready", "in-progress"],
                 "state": "OPEN"},
            43: {"title": "#43 the second card", "body": "", "labels": ["lane-ready"], "state": "OPEN"},
        }
        self.remote_state = {}   # lane -> the remote status the dispatcher reads
        self.assigned = []       # (lane, issue) of every successful assignment
        self.pending = None      # the task that was uploaded for the next assignment
        self.assign_error = ""   # the host rejects the assignment with this message
        self.gh_calls = []
        root.mkdir(parents=True, exist_ok=True)

    # --- the AD's `gh` -------------------------------------------------------------------
    def gh(self, args, timeout=60):
        self.gh_calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            rows = [{"number": number, "title": issue["title"], "body": issue["body"],
                     "labels": list(issue["labels"]), "state": issue["state"]}
                    for number, issue in sorted(self.issues.items())]
            return 0, json.dumps(rows), ""
        if args[:2] == ["issue", "view"]:
            number = int(args[2])
            issue = self.issues.get(number)
            if issue is None:
                return 1, "", "gh: could not read the issue"
            return 0, json.dumps({"number": number, "state": issue["state"],
                                  "labels": [{"name": name} for name in issue["labels"]]}), ""
        if args[:2] == ["issue", "edit"]:
            issue = self.issues[int(args[2])]
            for flag in ("--add-label", "--remove-label"):
                if flag not in args:
                    continue
                label = args[args.index(flag) + 1]
                if flag == "--add-label" and label not in issue["labels"]:
                    issue["labels"].append(label)
                if flag == "--remove-label" and label in issue["labels"]:
                    issue["labels"].remove(label)
            return 0, "", ""
        if args[:2] == ["pr", "create"]:
            return 0, "https://github.com/surlymochan/phanes/pull/1", ""
        return 0, "", ""

    # --- the AD's `sh` -------------------------------------------------------------------
    def sh(self, cmd, timeout=120, inp=None):
        if isinstance(cmd, str):
            return 0, "", ""
        argv = list(cmd)
        if argv[:2] == ["git", "ls-remote"]:
            return 0, f"{'a' * 40}\trefs/heads/main\n", ""
        if "log" in argv:
            fmt = next((arg for arg in argv if arg.startswith("--format=")), "")
            if fmt == "--format=%s":
                return 0, "#42 #43 the card title\n", ""
            if fmt == "--format=%B":
                return 0, "Closes #42\n\nCloses #43\n\n验收：fake tests passed\n", ""
        if argv[:1] == ["ssh"]:
            command = argv[-1]
            if command.startswith("cat > /tmp/task-"):
                lane = command.split("/tmp/task-", 1)[1].split(".md", 1)[0]
                issue = re.search(r"#(\d+)", inp or "")
                if issue:
                    self.pending = (lane, int(issue.group(1)))
            if "lane assign" in command:
                if self.assign_error:
                    return 1, "", self.assign_error
                if self.pending:
                    self.assigned.append(self.pending)
                return 0, json.dumps({"id": "run-43"}), ""
        return 0, "", ""

    # --- the AD's `remote` ---------------------------------------------------------------
    def remote(self, lane, cfg, run=""):
        return self.remote_state.get(lane, {"lane": "idle"})

    def add_issue(self, number, labels=("lane-ready",)):
        """Put a new dispatchable card into the pool."""
        self.issues[number] = {"title": f"#{number} a new card", "body": "",
                               "labels": list(labels), "state": "OPEN"}

    def clear_pool(self):
        """Empty the pool (no card is dispatchable any more)."""
        for issue in self.issues.values():
            issue["labels"] = ["lane-ready", "in-progress"]

    def brain_log(self) -> str:
        path = Path(self.root / "brain-log.md")
        return path.read_text() if path.exists() else ""


def prepare(module, sandbox: Sandbox):
    """Point the AD at the sandbox: no real state dir, no real log, no gh, no fleet."""
    module.D = str(sandbox.root / "state")
    os.makedirs(module.D, exist_ok=True)
    module.BRAIN_LOG = str(sandbox.root / "brain-log.md")
    module.time = sandbox.clock
    module.gh = sandbox.gh
    module.sh = sandbox.sh
    module.remote = sandbox.remote
    module.replenish = lambda k, state: []
    module.LOW = 0
    module.BACKOFF.clear()
    module.IDLE_LOG_MIN_S = 300


class IdleRedispatchTest(unittest.TestCase):
    """The idle gap of a lane: the re-dispatch, the pause check and the idle metric."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="ad-idle-", dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.clock = Clock()
        self.sandbox = Sandbox(self.root, self.clock)
        self.module = load_module(f"autodispatch_idle_{id(self)}")
        prepare(self.module, self.sandbox)
        self.lanes = {LANE: {"host": HOST, "backend": "pi", "repo": "/tmp/fleet",
                             "fleet": "/tmp/openswarm-fleet"}}
        # the pause store of the operator must not leak into the test
        old_home = os.environ.get("OPENSWARM_HOME")
        os.environ["OPENSWARM_HOME"] = str(self.root / "pauses")
        self.addCleanup(self._restore_home, old_home)

    def _restore_home(self, old_home):
        if old_home is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = old_home

    # --- helpers -------------------------------------------------------------------------
    def busy_state(self, issue=42, run="run-42"):
        """The state of a lane that is running a card."""
        return {LANE: {"issue": issue, "run": run, "host": HOST, "base": "a" * 40,
                       "at": "2026-11-02 21:00:00", "verify": None}}

    def finished_run(self, ended_at=None, lane_state="busy"):
        """The remote status of a lane whose run has ended."""
        return {"lane": lane_state,
                "run": {"status": "done", "branch": f"fleet/{LANE}", "commit": "deadbeef",
                        "repo": "/tmp/fleet", "backend": "pi", "model": "pi",
                        "ended_at": self.clock.now - 5 if ended_at is None else ended_at}}

    def tick(self, state):
        # the dispatcher writes the snapshot without closing it: the warning is pre-existing
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            self.module.tick(self.lanes, state, 0)
        return state

    def advance(self, seconds, dispatcher_alive=True):
        """Move the clock; a running dispatcher refreshes its heartbeat while it sleeps."""
        self.clock.advance(seconds)
        if dispatcher_alive:
            (Path(self.module.D) / "heartbeat").write_text(self.clock.stamp() + "\n")

    def lanes_status(self):
        path = Path(self.module.D) / "lanes-status.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def stale_heartbeat(self, seconds):
        """Pretend the dispatcher was down for `seconds` (its heartbeat is that old)."""
        (Path(self.module.D) / "heartbeat").write_text(self.clock.stamp(-seconds) + "\n")

    # --- the re-dispatch -----------------------------------------------------------------
    def test_the_next_card_is_dispatched_in_the_same_tick_as_the_run_end(self):
        state = self.busy_state()
        # the host still calls the lane busy: only the re-dispatch after the harvest
        # can give it a card in this tick
        self.sandbox.remote_state[LANE] = self.finished_run(lane_state="busy")
        self.tick(state)
        self.assertEqual([lane for lane, _ in self.sandbox.assigned], [LANE])
        self.assertEqual(state[LANE]["issue"], 43)
        self.assertEqual(state[LANE]["run"], "run-43")

    def test_a_paused_lane_gets_no_card_after_its_run_ended(self):
        # the lane was paused while its run was running (Refs #1688 review)
        (Path(self.module.D) / f"PAUSE-{LANE}").write_text("")
        state = self.busy_state()
        self.sandbox.remote_state[LANE] = self.finished_run(lane_state="busy")
        self.tick(state)
        self.assertEqual(self.sandbox.assigned, [])
        self.assertNotIn(LANE, state)
        # the gap stays open and the paused time is accounted
        self.sandbox.remote_state[LANE] = {"lane": "idle"}
        self.advance(TICK_S)
        self.tick(state)
        window = state["_idle"][LANE]
        self.assertGreaterEqual(window["paused_s"], TICK_S)
        self.assertEqual(window["no_card_s"], 0.0)

    def test_a_lane_the_host_still_calls_busy_does_not_back_the_host_off(self):
        state = self.busy_state()
        self.sandbox.remote_state[LANE] = self.finished_run(lane_state="busy")
        self.sandbox.assign_error = f"lane {LANE} is busy (busy)"
        self.tick(state)
        self.assertEqual(self.sandbox.assigned, [])
        self.assertTrue(self.module.host_ok(HOST), "a busy lane must not back the host off")
        # the card is free again: the next tick can take it
        self.assertNotIn("in-progress", self.sandbox.issues[43]["labels"])

    def test_no_card_leaves_the_lane_idle_without_an_error(self):
        self.sandbox.clear_pool()
        state = self.busy_state()
        self.sandbox.remote_state[LANE] = self.finished_run(lane_state="busy")
        self.tick(state)
        self.assertEqual(self.sandbox.assigned, [])
        self.assertNotIn(LANE, state)
        self.assertIn(LANE, state["_idle"])
        # a second tick without a card must not dispatch anything either
        self.sandbox.remote_state[LANE] = {"lane": "idle"}
        self.advance(TICK_S)
        self.tick(state)
        self.assertEqual(self.sandbox.assigned, [])

    # --- the idle metric -----------------------------------------------------------------
    def test_the_idle_metric_starts_at_the_real_end_of_the_run(self):
        # the run ended ten minutes ago, the harvest came late: the gap is ten minutes
        state = self.busy_state()
        self.sandbox.remote_state[LANE] = self.finished_run(ended_at=self.clock.now - 600)
        self.tick(state)
        record = state["_idle_last"][LANE]
        self.assertAlmostEqual(record["idle_s"], 600.0, delta=2.0)
        status = self.lanes_status()
        self.assertAlmostEqual(status["_idle_s"][LANE], 600.0, delta=2.0)
        self.assertIn(LANE, status["_idle_reason"])
        # a gap of more than five minutes is written to the brain log with its reason
        self.assertIn(f"{LANE} idle 600s before dispatch", self.sandbox.brain_log())

    def test_a_short_gap_is_not_written_to_the_brain_log(self):
        state = self.busy_state()
        self.sandbox.remote_state[LANE] = self.finished_run(ended_at=self.clock.now - 30)
        self.tick(state)
        self.assertNotIn("before dispatch", self.sandbox.brain_log())

    def test_the_downtime_of_the_dispatcher_is_the_reason_of_a_long_gap(self):
        # the dispatcher was down while the run ended: the gap is the downtime
        self.stale_heartbeat(900)
        state = self.busy_state()
        self.sandbox.remote_state[LANE] = self.finished_run(ended_at=self.clock.now - 900)
        self.tick(state)
        record = state["_idle_last"][LANE]
        self.assertEqual(record["reason"], "dispatcher not running")
        self.assertAlmostEqual(record["idle_s"], 900.0, delta=2.0)
        self.assertIn("reason: dispatcher not running", self.sandbox.brain_log())

    def test_a_capacity_failure_is_the_reason_of_the_gap(self):
        state = self.busy_state()
        self.sandbox.remote_state[LANE] = self.finished_run(ended_at=self.clock.now)
        # the fleet refuses the assignment: the lane keeps its card and stays idle
        self.sandbox.assign_error = "fleet capacity exhausted"
        self.tick(state)
        self.assertEqual(self.sandbox.assigned, [])
        self.assertNotIn(LANE, state)
        self.assertIn(LANE, state["_idle"])
        self.assertNotIn("in-progress", self.sandbox.issues[43]["labels"])
        # the next tick cannot dispatch either
        self.sandbox.remote_state[LANE] = {"lane": "idle"}
        self.advance(360)
        self.tick(state)
        window = state["_idle"][LANE]
        self.assertGreaterEqual(window["no_cap_s"], 360)
        self.assertEqual(window["no_card_s"], 0.0)
        # the capacity comes back: the lane gets its card and the reason is named
        self.sandbox.assign_error = ""
        self.advance(360)
        self.tick(state)
        record = state["_idle_last"][LANE]
        self.assertEqual(record["reason"], "no capacity")
        self.assertIn("reason: no capacity", self.sandbox.brain_log())

    def test_the_health_snapshot_carries_the_idle_fields(self):
        state = self.busy_state()
        self.sandbox.clear_pool()
        self.sandbox.remote_state[LANE] = self.finished_run(ended_at=self.clock.now - 120)
        self.tick(state)
        status = self.lanes_status()
        self.assertIn("_idle_s", status)
        self.assertIn("_idle_reason", status)
        self.assertIn("_idle_open_s", status)
        # the lane is still waiting for a card: the open gap is visible in the snapshot
        self.assertGreaterEqual(status["_idle_open_s"][LANE], 120.0)
        self.assertGreaterEqual(status["_idle_worst_s"], 120.0)
        self.assertEqual(status["_busy"], 0)

    def test_the_next_window_does_not_inherit_the_downtime_of_the_previous_one(self):
        # window 1: the dispatcher was down when the run ended
        self.stale_heartbeat(900)
        state = self.busy_state()
        self.sandbox.remote_state[LANE] = self.finished_run(ended_at=self.clock.now - 900)
        self.tick(state)
        self.assertEqual(state["_idle_last"][LANE]["reason"], "dispatcher not running")
        # window 2: the dispatcher runs, but the pool is empty for five minutes
        self.sandbox.clear_pool()
        self.sandbox.remote_state[LANE] = self.finished_run(ended_at=self.clock.now)
        self.tick(state)
        self.sandbox.remote_state[LANE] = {"lane": "idle"}
        self.advance(360)
        self.tick(state)
        self.sandbox.add_issue(44)
        self.tick(state)
        record = state["_idle_last"][LANE]
        self.assertEqual(record["reason"], "no card")
        self.assertGreaterEqual(record["idle_s"], 360.0)
        self.assertIn("reason: no card", self.sandbox.brain_log())


if __name__ == "__main__":
    unittest.main(verbosity=2)
