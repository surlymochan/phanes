#!/usr/bin/env python3
"""Unit tests for the stuck lane detector (Refs #1825).

A lane whose runs keep ending `budget_exceeded` without a single event is stuck: the run
was admitted (no queue wait) and the resident pi RPC waits for a sandbox child that never
makes progress. The detector reads the ledger and the real event counts of the lane host,
never the `running` state of the health snapshot, and the repair is narrow: only the
resident pi RPC process tree of that lane is terminated.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)

SCRATCH = Path(__file__).resolve().parents[2] / ".openswarm-tmp"
BUDGET = {"status": "budget_exceeded", "error_class": "budget_exceeded", "branch": None, "commit": None}
LANE = "M17"


def run_snippet(source, args, env=None):
    """Run one remote snippet locally and return its parsed answer."""
    environment = {**os.environ, **(env or {})}
    result = subprocess.run([sys.executable, "-", *args], input=source, capture_output=True,
                            text=True, timeout=60, env=environment)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


class FakeLaneHost:
    """A fake lane host: a ledger, the run directories and their event files."""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

    def add_run(self, run_id, status="budget_exceeded", events=0, queue_reason=None, lane=LANE):
        """One finished run of the lane: a ledger row, a status and its event file."""
        directory = self.root / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        status_row = {"state": "finished", "status": status, "lane": lane, "updated": "2026-11-03 18:39:00"}
        if queue_reason is not None:
            status_row["queue_reason"] = queue_reason
        (directory / "status.json").write_text(json.dumps(status_row))
        event_file = directory / "events.1.pi.jsonl"
        event_file.write_text("".join(json.dumps({"type": "event", "n": i}) + "\n" for i in range(events)))
        with open(self.root / "ledger.jsonl", "a") as stream:
            stream.write(json.dumps({"id": run_id, "ts": "2026-11-03 18:39:00", "lane": lane,
                                     "status": status, "error_class": None}) + "\n")
        return run_id


class LaneStuckProbeTest(unittest.TestCase):
    """The probe reads the real event counts and the state of the lane's pi RPC."""

    def setUp(self):
        SCRATCH.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.host = FakeLaneHost(Path(self.tmp.name) / "home")
        self.addCleanup(self.cleanup_processes)
        self.children = []

    def cleanup_processes(self):
        """Kill the dummy processes the test started."""
        for process in self.children:
            try: process.kill()
            except OSError: pass

    def spawn_rpc(self, lane=LANE):
        """A dummy resident pi RPC: it waits for a `bwrap` child that never moves."""
        code = ("import subprocess,sys;"
                "subprocess.call([sys.executable,'-c','import time; time.sleep(300)','bwrap'])")
        process = subprocess.Popen([sys.executable, "-c", code, "--session-dir", f"/lanes/{lane}/pi-sessions"])
        self.children.append(process)
        return process

    def test_probe_counts_the_events_and_the_queue_reason(self):
        self.host.add_run("run-1", events=0)
        self.host.add_run("run-2", events=0, queue_reason="provider concurrency limit")
        self.host.add_run("run-3", events=7)
        answer = run_snippet(AUTODISPATCH.LANE_STUCK_PROBE, [LANE, "3"],
                             env={"OPENSWARM_HOME": str(self.host.root), "AD_LANE_STUCK_SAMPLE_S": "0.2"})
        rows = {row["run_id"]: row for row in answer["runs"]}
        self.assertEqual(rows["run-1"]["events"], 0)
        self.assertEqual(rows["run-2"]["queue_reason"], "provider concurrency limit")
        self.assertEqual(rows["run-3"]["events"], 7)
        self.assertEqual(rows["run-1"]["queue_reason"], None)

    def test_probe_finds_the_lane_rpc_and_its_sandbox_child(self):
        process = self.spawn_rpc()
        time.sleep(0.5)
        answer = run_snippet(AUTODISPATCH.LANE_STUCK_PROBE, [LANE, "3"],
                             env={"OPENSWARM_HOME": str(self.host.root), "AD_LANE_STUCK_SAMPLE_S": "0.2"})
        rpc = answer["rpc"]
        self.assertIsNotNone(rpc, "the resident pi RPC of the lane must be found")
        self.assertEqual(rpc["pid"], process.pid)
        self.assertIn("do_wait", rpc["wchan"])
        sandbox = [child for child in rpc["children"] if "bwrap" in child["cmdline"]]
        self.assertTrue(sandbox, rpc["children"])
        self.assertFalse(any(child["progress"] for child in sandbox))

    def test_kill_terminates_the_lane_tree_only(self):
        process = self.spawn_rpc()
        time.sleep(0.5)
        answer = run_snippet(AUTODISPATCH.LANE_STUCK_KILL, [LANE, str(process.pid)])
        self.assertIn(process.pid, answer["terminated"])
        self.assertEqual(process.wait(timeout=10), -15, "the lane's pi RPC must be terminated")

    def test_kill_refuses_a_process_of_another_lane(self):
        process = self.spawn_rpc(lane="M19")
        time.sleep(0.5)
        answer = run_snippet(AUTODISPATCH.LANE_STUCK_KILL, [LANE, str(process.pid)])
        self.assertEqual(answer["terminated"], [])
        self.assertEqual(answer["skipped"], process.pid)
        self.assertIsNone(process.poll(), "a process of another lane must not be touched")


class LaneStuckVerdictTest(unittest.TestCase):
    """The verdict itself: 0-event budget runs without a queue wait, and a stuck pi RPC."""

    def test_three_empty_budget_runs_are_stuck(self):
        runs = [{"run_id": f"run-{i}", "status": "budget_exceeded", "events": 0, "queue_reason": None}
                for i in range(3)]
        self.assertTrue(AUTODISPATCH.lane_stuck_verdict(runs))

    def test_queued_runs_are_not_stuck(self):
        """The M17 case of 2026-11-03: the runs only waited for the provider limit."""
        runs = [{"run_id": f"run-{i}", "status": "budget_exceeded", "events": 0,
                 "queue_reason": "provider concurrency limit"} for i in range(4)]
        self.assertFalse(AUTODISPATCH.lane_stuck_verdict(runs))

    def test_a_run_with_events_is_not_stuck(self):
        runs = [{"run_id": "run-0", "status": "budget_exceeded", "events": 0, "queue_reason": None},
                {"run_id": "run-1", "status": "budget_exceeded", "events": 4, "queue_reason": None},
                {"run_id": "run-2", "status": "budget_exceeded", "events": 0, "queue_reason": None}]
        self.assertFalse(AUTODISPATCH.lane_stuck_verdict(runs))

    def test_an_unknown_event_count_is_not_stuck(self):
        runs = [{"run_id": f"run-{i}", "status": "budget_exceeded", "events": None, "queue_reason": None}
                for i in range(3)]
        self.assertFalse(AUTODISPATCH.lane_stuck_verdict(runs))

    def test_two_runs_are_not_enough(self):
        runs = [{"run_id": f"run-{i}", "status": "budget_exceeded", "events": 0, "queue_reason": None}
                for i in range(2)]
        self.assertFalse(AUTODISPATCH.lane_stuck_verdict(runs))

    def test_rpc_tree_stuck_needs_do_wait_and_a_frozen_sandbox(self):
        frozen = {"pid": 2, "cmdline": "bwrap ...", "progress": False}
        moving = {"pid": 3, "cmdline": "bwrap ...", "progress": True}
        self.assertTrue(AUTODISPATCH.rpc_tree_stuck({"wchan": "do_wait", "children": [frozen]}))
        self.assertFalse(AUTODISPATCH.rpc_tree_stuck({"wchan": "hrtimer_nanosleep", "children": [frozen]}))
        self.assertFalse(AUTODISPATCH.rpc_tree_stuck({"wchan": "do_wait", "children": [moving]}))
        self.assertFalse(AUTODISPATCH.rpc_tree_stuck({"wchan": "do_wait", "children": []}))
        self.assertFalse(AUTODISPATCH.rpc_tree_stuck(None))


class LaneStuckNoteTest(unittest.TestCase):
    """The dispatcher side: one cleanup per stuck episode, an audit row and an alert."""

    def setUp(self):
        SCRATCH.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.orig_d, self.orig_brain = AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG
        self.orig_gh, self.orig_sh = AUTODISPATCH.gh, AUTODISPATCH.sh
        AUTODISPATCH.D = str(self.dir)
        AUTODISPATCH.BRAIN_LOG = str(self.dir / "brain.log")
        AUTODISPATCH.gh = lambda args, timeout=60: (0, "", "")
        self.kills = []
        self.probe_answer = {"lane": LANE, "runs": [], "rpc": None}
        self.addCleanup(self.restore)

    def restore(self):
        AUTODISPATCH.D, AUTODISPATCH.BRAIN_LOG = self.orig_d, self.orig_brain
        AUTODISPATCH.gh, AUTODISPATCH.sh = self.orig_gh, self.orig_sh

    def fake_sh(self, cmd, timeout=120, inp=None):
        """Answer the probe and the cleanup of the detector."""
        if inp and "lane-stuck-probe" in inp:
            return 0, json.dumps(self.probe_answer), ""
        if inp and "lane-stuck-kill" in inp:
            self.kills.append(cmd[-1])
            return 0, json.dumps({"terminated": [4242], "skipped": None, "reason": "terminated"}), ""
        return 0, "", ""

    def stuck_probe(self, count=3, queue_reason=None, pid=4242):
        """The probe answer of a lane whose last `count` runs were empty budget runs."""
        runs = [{"run_id": f"run-{i}", "status": "budget_exceeded", "error_class": "budget_exceeded",
                 "events": 0, "queue_reason": queue_reason} for i in range(count)]
        rpc = {"pid": pid, "cmdline": "pi --session-dir /lanes/M17/pi-sessions", "state": "S",
               "wchan": "do_wait", "children": [{"pid": pid + 1, "cmdline": "bwrap ...", "progress": False}]}
        return {"lane": LANE, "runs": runs, "rpc": rpc}

    def end_run(self, index, info=BUDGET):
        """Let the dispatcher finish one run of the lane."""
        st = {"issue": 1800 + index, "run": f"run-{index}", "host": "h", "base": "b" * 40}
        return AUTODISPATCH.finish(LANE, {"host": "h"}, st, info, {})

    def test_three_empty_budget_runs_trigger_one_cleanup(self):
        AUTODISPATCH.sh = self.fake_sh
        self.probe_answer = self.stuck_probe()
        for index in range(3):
            self.end_run(index)
        self.assertEqual(len(self.kills), 1, "the stuck episode must trigger exactly one cleanup")
        audit = (self.dir / "lane-stuck-audit.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(audit), 1, audit)
        row = json.loads(audit[0])
        self.assertEqual(row["action"], "cleanup")
        self.assertEqual(row["lane"], LANE)
        self.assertEqual(row["terminated"], [4242])
        alert = (self.dir / "health.alert").read_text()
        self.assertIn("alert=lane_stuck", alert)
        self.assertIn(f"lane={LANE}", alert)
        brain = (self.dir / "brain.log").read_text()
        self.assertIn("stuck", brain)
        state = json.loads((self.dir / "lane-stuck.json").read_text())
        self.assertTrue(state[LANE]["cleaned"])

    def test_a_fourth_empty_run_does_not_clean_the_same_episode_twice(self):
        AUTODISPATCH.sh = self.fake_sh
        self.probe_answer = self.stuck_probe()
        for index in range(4):
            self.end_run(index)
        self.assertEqual(len(self.kills), 1, "one stuck episode means one cleanup")

    def test_queued_runs_do_not_trigger_a_cleanup(self):
        AUTODISPATCH.sh = self.fake_sh
        self.probe_answer = self.stuck_probe(queue_reason="provider concurrency limit")
        for index in range(4):
            self.end_run(index)
        self.assertEqual(self.kills, [], "a run that only waited in the queue is not a stuck lane")
        self.assertFalse((self.dir / "lane-stuck.json").exists())

    def test_a_running_rpc_is_not_touched(self):
        AUTODISPATCH.sh = self.fake_sh
        answer = self.stuck_probe()
        answer["rpc"]["children"][0]["progress"] = True
        self.probe_answer = answer
        for index in range(3):
            self.end_run(index)
        self.assertEqual(self.kills, [], "a sandbox that still moves is not a stuck lane")

    def test_a_run_with_events_clears_the_episode(self):
        AUTODISPATCH.sh = self.fake_sh
        self.probe_answer = self.stuck_probe()
        for index in range(3):
            self.end_run(index)
        self.assertEqual(len(self.kills), 1)
        # the lane works again: the next probe reports events and the episode is over
        answer = self.stuck_probe()
        answer["runs"][-1]["events"] = 12
        self.probe_answer = answer
        self.end_run(3)
        state = json.loads((self.dir / "lane-stuck.json").read_text())
        self.assertNotIn(LANE, state, "a run with events ends the stuck episode")

    def test_the_failed_card_is_not_dispatched_again(self):
        """The cleanup never re-dispatches the card that failed."""
        AUTODISPATCH.sh = self.fake_sh
        self.probe_answer = self.stuck_probe()
        calls = []
        AUTODISPATCH.gh = lambda args, timeout=60: (calls.append(args), (0, "", ""))[1]
        for index in range(3):
            self.end_run(index)
        self.assertEqual(self.kills and len(self.kills), 1)
        self.assertFalse([args for args in calls if args[:2] == ["issue", "edit"]
                          and "lane-ready" in args], "the failed card must not become dispatchable again")


if __name__ == "__main__":
    unittest.main()
