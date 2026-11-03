"""Busy entry expiry in the dispatcher (Refs #1786).

A busy entry whose last trustworthy activity is older than the limit is marked
stale: the mark is an overlay on top of the run tracking, it never removes the
run record and it survives the ticks until the run it marks is resolved or an
operator handles it.

The trustworthy activity is the progress stamp the host writes when the run
really moves (a child event or a provider request).  These tests cover the
whole path: the host writes the stamp, the probe reads it, and the dispatcher
judges it.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "dispatch_engine_busy_stale_test", os.path.join(str(ROOT), "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

_bspec = importlib.util.spec_from_file_location(
    "dispatch_busy_stale_test", os.path.join(str(ROOT), "lib", "dispatch", "busy_stale.py"))
BUSY = importlib.util.module_from_spec(_bspec)
_bspec.loader.exec_module(BUSY)

xs = _load.load()

LIMIT = 600.0          # the limit of the fixture: ten minutes without activity
T0 = 1_700_000_000.0   # the moment the fixture dispatches its run


class PolicyTests(unittest.TestCase):
    """The pure policy of the expiry."""

    def test_a_stamp_is_read_from_a_number_and_from_text(self):
        self.assertEqual(BUSY.stamp(12.5), 12.5)
        self.assertEqual(BUSY.stamp("12.5"), 12.5)
        self.assertIsNone(BUSY.stamp("not a stamp"))
        self.assertIsNone(BUSY.stamp(None))
        self.assertIsNone(BUSY.stamp(True))

    def test_the_activity_is_the_progress_record_the_host_writes(self):
        now = 1000.0
        first = BUSY.observe(None, {"activity": 500.0}, now, dispatched_at=100.0)
        self.assertEqual(first["at"], 500.0)
        self.assertEqual(first["source"], "host")
        # The host keeps writing its progress record: the run is alive.
        moved = BUSY.observe(first, {"activity": 900.0}, now + 60, dispatched_at=100.0)
        self.assertEqual(moved["at"], 900.0)
        self.assertEqual(moved["since"], now + 60)
        # The host stops writing it: the last reading stands and ages.
        same = BUSY.observe(moved, {"activity": 900.0}, now + 120, dispatched_at=100.0)
        self.assertEqual(same["at"], 900.0)
        self.assertEqual(same["since"], now + 60)

    def test_a_host_that_is_gone_keeps_the_last_reading(self):
        first = BUSY.observe(None, {"activity": 500.0}, 1000.0, dispatched_at=100.0)
        gone = BUSY.observe(first, {"error": "ssh status timeout"}, 1060.0, dispatched_at=100.0)
        self.assertEqual(gone["at"], 500.0)
        self.assertEqual(gone["source"], "host")
        self.assertEqual(gone["since"], first["since"])

    def test_a_lane_whose_host_never_answered_falls_back_to_the_dispatch(self):
        """The M6 case: the host is gone before it ever reported a progress."""
        record = BUSY.observe(None, {"error": "ssh status timeout"}, 1000.0, dispatched_at=100.0)
        self.assertEqual(record["since"], 100.0)
        self.assertEqual(record["source"], "dispatch")
        self.assertTrue(BUSY.judge(record, 1000.0 + 700, LIMIT)["stale"])

    def test_a_host_that_answers_without_progress_is_not_evidence(self):
        """A host that cannot report a progress stamp must not expire a busy entry."""
        record = BUSY.observe(None, {"activity": None}, 1000.0, dispatched_at=100.0)
        self.assertEqual(record, {})
        self.assertIsNone(BUSY.judge(record, 1000.0 + 700, LIMIT)["idle_s"])
        self.assertFalse(BUSY.judge(record, 1000.0 + 700, LIMIT)["stale"])

    def test_an_unknown_reading_is_not_evidence(self):
        verdict = BUSY.judge({}, 1000.0, LIMIT)
        self.assertIsNone(verdict["idle_s"])
        self.assertFalse(verdict["stale"])

    def test_the_limit_switches_the_check_off(self):
        record = {"at": 0.0, "since": 0.0, "stamp": None, "source": "dispatch"}
        self.assertFalse(BUSY.judge(record, 100000.0, 0)["stale"])

    def test_a_host_clock_ahead_does_not_refresh_the_activity(self):
        """A host clock that runs ahead must not keep a dead run alive.

        The host repeats the same (future) stamp every tick.  The stamp does not
        move, so the silence keeps growing and the entry expires.
        """
        record = BUSY.observe(None, {"activity": 5000.0}, 1000.0, dispatched_at=1000.0)
        self.assertEqual(record["at"], 5000.0)
        for tick in range(1, 6):
            record = BUSY.observe(record, {"activity": 5000.0}, 1000.0 + tick * 60, dispatched_at=1000.0)
            self.assertEqual(record["since"], 1000.0, "the activity was refreshed by a repeated stamp")
        verdict = BUSY.judge(record, 1000.0 + 5 * 60, LIMIT)
        self.assertEqual(verdict["idle_s"], 5 * 60)
        self.assertFalse(verdict["stale"])

    def test_a_host_clock_behind_does_not_fake_a_silence(self):
        """A host clock that lags must not make a working run look stale.

        The stamp of the host is an hour behind ours, but it moves: the run is
        alive, so the silence (measured on our clock) restarts.
        """
        record = BUSY.observe(None, {"activity": 100.0}, 1000.0, dispatched_at=100.0)
        for tick in range(1, 6):
            record = BUSY.observe(record, {"activity": 100.0 + tick * 60}, 1000.0 + tick * 60,
                                  dispatched_at=100.0)
            self.assertEqual(record["since"], 1000.0 + tick * 60)
            self.assertFalse(BUSY.judge(record, 1000.0 + tick * 60, LIMIT)["stale"])

    def test_the_silence_is_measured_on_our_own_clock(self):
        record = {"at": 500.0, "since": 900.0, "stamp": 500.0, "source": "host"}
        verdict = BUSY.judge(record, 1000.0, LIMIT)
        self.assertEqual(verdict["idle_s"], 100.0)
        self.assertEqual(verdict["activity_at"], 500.0)


class ProbeTests(unittest.TestCase):
    """The production probe: what the dispatcher reads from a host."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, "lanes", "M6"))
        os.makedirs(os.path.join(self.home, "runs", "run-1361"))
        with open(os.path.join(self.home, "lanes", "M6", "state.json"), "w", encoding="utf-8") as stream:
            json.dump({"state": "busy", "current_run": "run-1361", "updated": T0}, stream)

    def write_run_status(self, **fields):
        with open(os.path.join(self.home, "runs", "run-1361", "status.json"), "w", encoding="utf-8") as stream:
            json.dump(fields, stream)

    def probe(self):
        proc = subprocess.run([sys.executable, "-", "M6", "run-1361", self.home],
                              input=E.REMOTE_STATUS, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_the_probe_reads_the_progress_stamp_and_not_the_status_stamp(self):
        """The "updated" stamp of the status file is not activity.

        The housekeeping writes of the host (the descendant scan) refresh it
        every few seconds, so a stuck run whose process is still alive would
        look busy for ever.  The probe has to report the progress stamp.
        """
        self.write_run_status(state="running", runner_pid=os.getpid(),
                              updated=T0 + 3600, last_activity_at=T0)
        out = self.probe()
        self.assertEqual(out["activity"], T0)
        self.assertEqual(out["run_state"], "running")

    def test_the_probe_reports_no_activity_when_the_host_writes_none(self):
        self.write_run_status(state="running", runner_pid=os.getpid(), updated=T0 + 3600)
        out = self.probe()
        self.assertIsNone(out["activity"])

    def test_the_probe_reports_the_finish_of_a_finished_run(self):
        with open(os.path.join(self.home, "runs", "run-1361", "result.json"), "w", encoding="utf-8") as stream:
            json.dump({"status": "done", "branch": "fleet/M6-issue1361", "commit": "b" * 40,
                       "finished_at": T0 + 120}, stream)
        self.write_run_status(state="finished", updated=T0 + 3600, last_activity_at=T0)
        out = self.probe()
        self.assertEqual(out["activity"], T0 + 120)


class HostStatusTests(unittest.TestCase):
    """The production status write path: what the host records as activity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.rdir = self.root / "run"
        self.rdir.mkdir()

    def status(self):
        with open(self.rdir / "status.json", encoding="utf-8") as stream:
            return json.load(stream)

    def test_a_housekeeping_write_does_not_refresh_the_progress_stamp(self):
        """The descendant scan rewrites the status file; that is not activity."""
        xs.set_status(str(self.rdir), last_activity_at=T0)
        for _ in range(5):
            xs.set_status(str(self.rdir), executor_descendants=[])
        status = self.status()
        self.assertEqual(status["last_activity_at"], T0)
        self.assertGreater(status["updated"], T0, "the housekeeping write did not move the status stamp")

    def test_the_progress_stamp_never_moves_backwards(self):
        self.assertEqual(xs.lane_activity_stamp(str(self.rdir), T0 + 60), T0 + 60)
        xs.set_status(str(self.rdir), last_activity_at=T0 + 60)
        # A clock that jumped backwards must not make a dead run look alive.
        self.assertEqual(xs.lane_activity_stamp(str(self.rdir), T0), T0 + 60)
        self.assertEqual(xs.lane_activity_stamp(str(self.rdir), T0 + 120), T0 + 120)
        self.assertIsNone(xs.lane_activity_stamp(str(self.rdir), None))

    def test_a_run_that_keeps_working_records_its_progress(self):
        """The lane loop writes the stamp of its last child event."""
        clock = FakeClock()
        child = FakeChild(clock, step=60.0, event_every=1, finish_after=3)
        self.run_turn(child, clock)
        status = self.status()
        self.assertGreater(status["last_activity_at"], T0, "the progress stamp of the run did not move")
        self.assertLessEqual(status["last_activity_at"], clock.now)

    def test_a_run_that_stays_alive_without_events_keeps_its_stamp(self):
        """A stuck run keeps its last stamp: the dispatcher can expire it."""
        clock = FakeClock()
        child = FakeChild(clock, step=60.0)
        self.run_turn(child, clock, budget_minutes=0.05)
        status = self.status()
        self.assertEqual(status["last_activity_at"], T0, "a run without events moved its progress stamp")
        self.assertGreater(status["updated"], T0, "the status file was not written at all")

    def run_turn(self, child, clock, budget_minutes=60, policy=None):
        lane = {"name": "M6", "backend": "pi", "model": "fake-model", "provider": "fake-provider",
                "repo": str(self.root)}
        spec = {"run_id": "busy-stale-test", "task": "wait for the executor", "title": "busy stale test",
                "started": clock.now, "budget_minutes": budget_minutes}
        ws = {"path": str(self.root), "root": str(self.root)}
        with mock.patch.object(xs, "NOW", clock), \
                mock.patch.object(xs, "load_policy", return_value=policy or {"lane": {"stall_after_s": 0}}):
            xs.lane_run_turn(lane, child, spec, "do the task", ws, str(self.rdir), None, {}, [])


class FakeClock:
    """A clock the fake child advances, so the lane loop sees time pass without waiting."""

    def __init__(self, start=T0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class FakeChild:
    """A lane child that stays alive and reports an event every `event_every` polls.

    `finish_after` ends the turn with a normal report, so the lane loop returns.
    """

    def __init__(self, clock, step=60.0, event_every=0, finish_after=None):
        self.clock = clock
        self.step = step
        self.event_every = event_every
        self.finish_after = finish_after
        self.lane = {"backend": "pi"}
        self.pid = None
        self.pgid = None
        self.sent = []
        self.pending = []
        self.polls = 0
        self.aborted = False
        self.closed = False
        self.alive_flag = True

    def alive(self):
        return self.alive_flag

    def send(self, obj):
        self.sent.append(obj)

    def wait_response(self, req_id, timeout=30, event_file=None):
        return {}

    def stderr_text(self):
        return ""

    def close(self):
        self.closed = True
        self.alive_flag = False

    def next_event(self, timeout=0):
        self.clock.advance(self.step)
        if self.pending:
            return self.pending.pop(0)
        self.polls += 1
        if self.finish_after is not None and self.polls >= self.finish_after:
            self.pending.extend(self.report_events())
            return self.pending.pop(0)
        if self.event_every and self.polls % self.event_every == 0:
            return {"type": "message_update", "message": {"role": "assistant", "content": []}}
        return False

    def report_events(self):
        report = {"status": "done", "summary": "fake child finished", "files_changed": [],
                  "tests": "none", "followups": []}
        message = {"role": "assistant", "content": [{"type": "text", "text": json.dumps(report)}],
                   "usage": {"input": 1, "output": 1}, "stopReason": "stop"}
        return [{"type": "message_end", "message": message}, {"type": "agent_end", "messages": [message]}]


class BusyStaleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)
        self.config_path = os.path.join(self.tmp.name, "dispatch.json")
        with open(self.config_path, "w", encoding="utf-8") as stream:
            json.dump({"repo": "a/b", "local_repo": self.tmp.name, "state_dir": self.state_dir,
                       "busy_stale": {"max_idle_s": LIMIT},
                       "ssh": {"control_dir": os.path.join(self.tmp.name, "cm")}}, stream)
        self.cfg = E.load_config(self.config_path)
        self.now = T0
        self.d = E.Dispatcher(self.cfg, out=open(os.devnull, "w"), clock=lambda: self.now)
        self.addCleanup(self.d.out.close)
        self.lanes = {"M6": {"backend": "pi", "host": "m6"}}
        self.probe_result = {"M6": {"lane": "busy", "run": "running"}}
        self.harvested = []
        self.queue_error = None
        self.d.probe = lambda _lanes, _state: dict(self.probe_result)
        self.d.harvest = lambda *_args: None
        self.d.sweep_leaked_inprogress = lambda *_args: None
        self.d.dispatch_reviews = lambda *_args: None
        self.d.assign_issues = lambda *_args: None
        self.d.replenish = lambda *_args: None

        def queue():
            if self.queue_error:
                raise RuntimeError(self.queue_error)
            return []

        self.d.queue = queue

    def busy_state(self, run="run-1361", issue=1361):
        return {"M6": {"issue": issue, "run": run, "host": "m6", "at": "2026-11-03 05:00:00",
                       "dispatched_at": self.now, "base": "a" * 40}}

    def tick(self, state=None, advance=60.0):
        self.now += advance
        self.d.tick(self.lanes, self.state if state is None else state)
        return self.status()

    def status(self):
        with open(self.d.path("lanes-status.json"), encoding="utf-8") as stream:
            return json.load(stream)

    def state_file(self):
        with open(self.d.path("state.json"), encoding="utf-8") as stream:
            return json.load(stream)


class BusyStaleTests(BusyStaleBase):
    def test_a_long_run_that_keeps_working_is_never_marked_stale(self):
        """A run with activity in every tick must not be mistaken for a zombie."""
        state = self.busy_state()
        for _ in range(20):                       # 20 minutes of work, ten times the limit
            self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": self.now}}
            self.tick(state)
        entry = self.status()["M6"]
        self.assertNotIn("stale", entry)
        self.assertEqual(self.status()["_summary"]["stale"], 0)

    def test_a_run_without_any_activity_is_marked_stale(self):
        state = self.busy_state()
        self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": T0}}
        for minute in range(1, 10):        # nine minutes of silence: still inside the limit
            self.tick(state)
            self.assertNotIn("stale", self.status()["M6"], "marked stale after %d minute(s)" % minute)
        self.tick(state)                   # the tenth minute crosses the limit
        entry = self.status()["M6"]
        self.assertTrue(entry["stale"])
        self.assertGreaterEqual(entry["stale_idle_s"], int(LIMIT))
        self.assertEqual(self.status()["_summary"]["stale"], 1)

    def test_a_live_run_that_does_nothing_is_marked_stale(self):
        """The host answers, the run is alive, but it does not move: it is stale."""
        state = self.busy_state()
        self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": T0,
                                    "run_state": "running", "runner_alive": True}}
        for minute in range(1, 10):
            self.tick(state)
            self.assertNotIn("stale", self.status()["M6"], "marked stale after %d minute(s)" % minute)
        self.tick(state)
        self.assertTrue(self.status()["M6"]["stale"])
        self.assertEqual(self.status()["_summary"]["stale"], 1)

    def test_a_host_that_keeps_rewriting_its_status_does_not_hide_the_silence(self):
        """The housekeeping writes of the host are not activity (Refs #1786).

        The host refreshes the "updated" stamp of the run status every few
        seconds (the descendant scan), but the progress stamp stays where it
        was: the busy entry has to expire anyway.
        """
        state = self.busy_state()
        for minute in range(1, 11):
            self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": T0,
                                        "updated": self.now}}
            self.tick(state)
        self.assertTrue(self.status()["M6"]["stale"])

    def test_a_lost_host_is_expired_by_the_last_known_activity(self):
        """The M6 case: the host is gone, the busy entry must not stay busy for ever."""
        state = self.busy_state()
        self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": T0}}
        self.tick(state)                   # the host is still there
        self.probe_result = {"M6": {"error": "ssh status timeout"}}
        for minute in range(1, 9):         # eight minutes without contact: still inside the limit
            self.tick(state)
            self.assertNotIn("stale", self.status()["M6"], "marked stale after %d minute(s)" % minute)
        self.tick(state)
        self.assertTrue(self.status()["M6"]["stale"])

    def test_a_host_that_never_answered_is_expired_by_the_dispatch(self):
        """The host was never reachable: the dispatch is all we know about the run."""
        state = self.busy_state()
        self.probe_result = {"M6": {"error": "host in backoff", "skip": True}}
        for minute in range(1, 10):
            self.tick(state)
            self.assertNotIn("stale", self.status()["M6"], "marked stale after %d minute(s)" % minute)
        self.tick(state)
        entry = self.status()["M6"]
        self.assertTrue(entry["stale"])
        self.assertEqual(entry["stale_source"], "dispatch")

    def test_a_host_without_a_progress_stamp_is_not_expired(self):
        """A host that cannot report a progress stamp never expires a busy entry."""
        state = self.busy_state()
        self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": None}}
        for _ in range(20):
            self.tick(state)
            self.assertNotIn("stale", self.status()["M6"])

    def test_the_mark_survives_the_ticks_until_the_run_is_resolved(self):
        state = self.busy_state()
        self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": T0}}
        for _ in range(11):
            self.tick(state)
        first = self.status()["M6"]
        self.assertTrue(first["stale"])
        for _ in range(5):                        # the mark must not die with the tick
            self.tick(state)
        later = self.status()["M6"]
        self.assertTrue(later["stale"])
        self.assertEqual(later["stale_since"], first["stale_since"])
        self.assertEqual(self.state_file()["_stale"]["M6"]["run"], "run-1361")

    def test_the_mark_does_not_touch_the_run_record(self):
        state = self.busy_state()
        self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": T0}}
        for _ in range(11):
            self.tick(state)
        self.assertTrue(self.status()["M6"]["stale"])
        self.assertEqual(state["M6"]["run"], "run-1361")
        self.assertEqual(state["M6"]["issue"], 1361)

    def test_the_mark_survives_a_lane_that_shows_life_again(self):
        """A short recovery must not make the review prompt vanish."""
        state = self.busy_state()
        self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": T0}}
        for _ in range(11):
            self.tick(state)
        self.assertTrue(self.status()["M6"]["stale"])
        self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": self.now}}
        self.tick(state)
        entry = self.status()["M6"]
        self.assertTrue(entry["stale"], "the mark vanished when the lane showed life again")
        self.assertIsNotNone(entry["stale_recovered_at"])
        self.assertEqual(self.status()["_summary"]["stale"], 1)
        # The run itself is untouched: it is still busy and still harvestable.
        self.assertEqual(state["M6"]["run"], "run-1361")

    def test_an_explicit_handling_drops_the_mark(self):
        """`openswarm dispatch stale --clear M6` handles the mark for good."""
        state = self.busy_state()
        self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": T0}}
        for _ in range(11):
            self.tick(state)
        self.assertTrue(self.status()["M6"]["stale"])

        args = argparse.Namespace(config=self.config_path, clear="M6")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(E.cmd_stale(args), 0)
        self.tick(state)
        entry = self.status()["M6"]
        self.assertNotIn("stale", entry)
        self.assertEqual(self.status()["_summary"]["stale"], 0)
        self.assertNotIn("M6", self.state_file().get("_stale", {}))
        # The run is untouched: a handled mark never costs the fleet a result.
        self.assertEqual(state["M6"]["run"], "run-1361")
        # The handled mark does not come back while the run lasts.
        self.tick(state)
        self.assertNotIn("stale", self.status()["M6"])

    def test_a_recovered_host_is_harvested_normally(self):
        """The mark must not cost the fleet the result of the run."""
        state = self.busy_state()
        self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": T0}}
        for _ in range(11):
            self.tick(state)
        self.assertTrue(self.status()["M6"]["stale"])

        def finish(lane, lc, st, payload, _state):
            self.harvested.append((lane, st["run"], st["issue"]))
            return True

        self.d.harvest = E.Dispatcher.harvest.__get__(self.d)
        self.d.finish = finish
        self.probe_result = {"M6": {"lane": "idle", "run": {"status": "done", "branch": "fleet/M6-issue1361",
                                                            "commit": "b" * 40, "backend": "pi"}}}
        self.tick(state)
        self.assertEqual(self.harvested, [("M6", "run-1361", 1361)])
        self.assertNotIn("M6", state)            # the run is resolved: the busy entry is gone
        self.assertNotIn("stale", self.status()["M6"])
        self.assertEqual(self.status()["_summary"]["stale"], 0)

    def test_the_expiry_runs_even_when_the_queue_query_fails(self):
        """A broken `gh` must not freeze the lane status (and its expiry with it)."""
        state = self.busy_state()
        self.queue_error = "gh: could not resolve host"
        self.probe_result = {"M6": {"lane": "busy", "run": "running", "activity": T0}}
        for _ in range(11):
            self.tick(state)
        status = self.status()
        self.assertTrue(status["M6"]["stale"])
        self.assertEqual(status["_summary"]["stale"], 1)
        self.assertIn("could not resolve host", status["_queue_error"])
        self.assertIsNone(status["_ready"])


if __name__ == "__main__":
    unittest.main()
