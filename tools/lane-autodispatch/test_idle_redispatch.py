#!/usr/bin/env python3
"""Reproduction of the lane idle gap of the autodispatcher (Refs #1688).

A lane that finished a run stays idle until it gets its next card.  When the pool is
not empty and no capacity is in the way, that gap is pure scheduling delay: the
dispatcher has to hand the lane its next card right after the harvest instead of
waiting for the next tick, and it has to measure the gap so that a long one can be
explained.

The tests below drive the real ``tick()`` of the autodispatcher against a fake fleet
(no network, no ssh, no GitHub) and they state the contract of the issue:

  * the lane whose run just ended gets its next card in the very same tick;
  * a lane that was paused while its run was running (or whose host is in backoff)
    gets no card;
  * without a card in the pool the lane stays idle: no error, no retry loop;
  * the idle gap is measured from the *real* end of the run (a harvest that came late
    and a dispatcher that was down do not shorten it) and the health output carries it;
  * a gap of more than five minutes is written to the brain log with its reason;
  * the downtime of the dispatcher belongs to the window it happened in: the next
    window starts empty and is explained by its own reason (no card, no capacity, ...).

The liveness of the dispatcher is its heartbeat file (``$D/heartbeat``, written by the
AD between the ticks): the gap between the heartbeat and the tick is the downtime of
the dispatcher itself.

The remote status is read by the *real* remote snippet of the dispatcher, running
against a fake executor home: the end time of a run has to be picked up by that
snippet, the dispatcher cannot invent it.

On the baseline every test of this file fails: the dispatcher neither re-dispatches a
lane after its harvest nor measures the idle gap of a lane.

Run it offline:

    cd tools/lane-autodispatch && python3 -m unittest test_idle_redispatch -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time as time_module
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("autodispatch.py")
SCRATCH = Path(__file__).resolve().parents[2] / ".swrm-tmp"
LANE = "GH2"
OTHER_LANE = "CX9"
HOST = "lane-host"
OTHER_HOST = "lane-host-2"
# The clock of the test starts at a whole second of a fixed date, so that the stamp of
# the heartbeat ("%F %T") and its mtime round-trip exactly (the dispatcher reads both).
START = time_module.mktime(time_module.strptime("2027-01-15 08:00:00", "%Y-%m-%d %H:%M:%S"))
TICK_S = 60.0             # the dispatch cadence of the fleet
IDLE_LOG_MIN_S = 300.0    # 5 minutes: the threshold of the brain log line
# The reason of a long gap names one of these (the wording may differ, the meaning may not).
REASON_WORDS = ("no card", "no capacity", "capacity", "dispatcher not running",
                "dispatcher not dispatching", "dispatcher down", "dispatcher was down",
                "not running", "paused", "无卡", "无容量", "未运行", "暂停")
# The field of the remote status that carries the end time of a run.
END_TIME_KEY_RE = re.compile(r"(end|finish|complet|clos)", re.I)


def load_module(name):
    """Load a fresh copy of the autodispatcher (every test patches its own copy)."""
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def to_epoch(value):
    """The epoch seconds of a number, an epoch string or a timestamp string."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return float(time_module.mktime(time_module.strptime(text, pattern)))
        except ValueError:
            continue
    try:
        import datetime
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def walk(node, path=()):
    """Every leaf of the health snapshot, with the path that leads to it."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, path + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, path + (str(index),))
    else:
        yield path, node


def lane_numbers(status, lane):
    """The numbers of the health snapshot that belong to `lane`."""
    found = []
    for path, value in walk(status):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if any(lane in part for part in path):
            found.append((path, float(value)))
    return found


def lane_reason(status, lane):
    """The reason the health snapshot gives for the idle gap of `lane` ("" when none)."""
    for path, value in walk(status):
        if not isinstance(value, str) or not value.strip():
            continue
        if not any(lane in part for part in path):
            continue
        if any("reason" in part.lower() or "why" in part.lower() for part in path):
            return value
    return ""


class Clock:
    """The clock of the test: it only moves when the test moves it."""

    def __init__(self, start=START):
        self.now = float(start)

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now

    def localtime(self, seconds=None):
        return time_module.localtime(self.now if seconds is None else seconds)

    def strftime(self, fmt, stamp=None):
        return time_module.strftime(fmt, self.localtime() if stamp is None else stamp)

    def stamp(self, at=None):
        """The heartbeat stamp of the clock (the format the dispatcher writes)."""
        at = self.now if at is None else float(at)
        return self.strftime("%Y-%m-%d %H:%M:%S", self.localtime(at))

    def __getattr__(self, name):
        return getattr(time_module, name)


class Fleet:
    """A fake fleet: the issue table, the `gh` calls, the `ssh` calls, the executor home."""

    def __init__(self, root, clock):
        self.root = root
        self.clock = clock
        self.module = None
        self.home = root / "executor"
        self.issues = {
            42: {"title": "#42 the card of GH2", "body": "", "labels": ["lane-ready", "in-progress"]},
            43: {"title": "#43 the next card", "body": "", "labels": ["lane-ready"]},
        }

    # --- the AD's `gh` -------------------------------------------------------------------
    def gh(self, args, timeout=60):
        if args[:2] == ["issue", "list"]:
            rows = [{"number": number, "title": issue["title"], "body": issue["body"],
                     "labels": list(issue["labels"]), "state": "OPEN"}
                    for number, issue in sorted(self.issues.items())]
            return 0, json.dumps(rows), ""
        if args[:2] == ["issue", "view"]:
            issue = self.issues.get(int(args[2]))
            if issue is None:
                return 1, "", "gh: no such issue"
            return 0, json.dumps({"number": int(args[2]), "state": "OPEN",
                                  "labels": [{"name": name} for name in issue["labels"]]}), ""
        if args[:2] == ["issue", "edit"]:
            issue = self.issues.get(int(args[2]))
            if issue is not None:
                for flag in ("--add-label", "--remove-label"):
                    if flag not in args:
                        continue
                    label = args[args.index(flag) + 1]
                    if flag == "--add-label" and label not in issue["labels"]:
                        issue["labels"].append(label)
                    elif flag == "--remove-label" and label in issue["labels"]:
                        issue["labels"].remove(label)
            return 0, "", ""
        return 0, "", ""

    # --- the AD's `sh` -------------------------------------------------------------------
    def sh(self, cmd, timeout=120, inp=None):
        command = cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd)
        if "ls-remote" in command:
            return 0, "%s\trefs/heads/main\n" % ("a" * 40), ""
        match = re.search(r"#(\d+)", command)
        if match:  # the `lane assign` call: the fleet answers with the id of the run
            return 0, json.dumps({"id": "run-%s" % match.group(1)}), ""
        return 0, "", ""

    # --- the AD's `remote` ---------------------------------------------------------------
    def remote(self, lane, cfg, run=""):
        """The status of the lane, read by the real remote snippet of the dispatcher.

        The snippet runs against the fake executor home, so the end time of the run has
        to be picked up by the snippet itself: the dispatcher cannot invent it.
        """
        if not run:
            return {"lane": "idle"}
        env = dict(os.environ)
        env["OPENSWARM_HOME"] = str(self.home)
        env["XSWARM_HOME"] = str(self.home)
        env["HOME"] = str(self.home)
        proc = subprocess.run([sys.executable, "-", lane, run], input=self.module.REMOTE_STATUS,
                              capture_output=True, text=True, env=env, timeout=60)
        if proc.returncode:
            return {"error": proc.stderr.strip()[-200:]}
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            return {"error": proc.stdout.strip()[-200:]}

    # --- the pool ------------------------------------------------------------------------
    def empty_pool(self):
        """Empty the pool (no card is dispatchable any more)."""
        for issue in self.issues.values():
            if "in-progress" not in issue["labels"]:
                issue["labels"].append("in-progress")

    def add_issue(self, number):
        """Put a new dispatchable card into the pool."""
        self.issues[number] = {"title": "#%d a new card" % number, "body": "",
                                "labels": ["lane-ready"]}


class IdleGapTest(unittest.TestCase):
    """The idle gap of a lane: the re-dispatch, the pause check and the idle metric."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="ad-idle-", dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.clock = Clock()
        self.module = load_module("autodispatch_idle_%d" % id(self))
        self.fleet = Fleet(self.root, self.clock)
        self.fleet.module = self.module
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.brain_path = self.root / "brain-log.md"
        self.brain_path.write_text("")
        # the AD is pointed at the sandbox: no real state dir, no real log, no real fleet
        self.module.D = str(self.state_dir)
        self.module.BRAIN_LOG = str(self.brain_path)
        self.module.gh = self.fleet.gh
        self.module.sh = self.fleet.sh
        self.module.remote = self.fleet.remote
        self.module.replenish = lambda k, state: []
        self.module.BACKOFF.clear()
        self.module.time = self.clock
        # the pause store of the operator must not leak into the test
        old_home = os.environ.get("OPENSWARM_HOME")
        os.environ["OPENSWARM_HOME"] = str(self.root)
        self.addCleanup(self._restore_home, old_home)
        # one lane by default: a card can only go to the lane under test
        self.lanes = {
            LANE: {"host": HOST, "backend": "pi", "repo": "/tmp/fleet", "fleet": "/tmp/swrm-fleet"},
        }
        self.state = {}

    def add_lane(self, lane, host, issue):
        """Add a second lane to the fleet (with a card of its own)."""
        self.lanes[lane] = {"host": host, "backend": "pi", "repo": "/tmp/fleet",
                            "fleet": "/tmp/swrm-fleet"}
        self.fleet.issues[issue] = {"title": "#%d the card of %s" % (issue, lane), "body": "",
                                    "labels": ["lane-ready", "in-progress"]}

    def _restore_home(self, old_home):
        if old_home is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = old_home

    # --- helpers -------------------------------------------------------------------------
    def running(self, lane=LANE, issue=42, run="run-42", host=HOST):
        """The state of a lane that is running a card."""
        return {lane: {"issue": issue, "run": run, "host": host, "base": "a" * 40,
                       "at": self.clock.stamp(), "verify": None}}

    def set_lane(self, lane, lane_state, run=None, ended_at=None):
        """The lane state of the executor (what the host reports about the lane)."""
        lane_dir = self.fleet.home / "lanes" / lane
        lane_dir.mkdir(parents=True, exist_ok=True)
        row = {"state": lane_state}
        if run:
            row["current_run"] = run
        if ended_at is not None:
            # the last activity of the lane is the one before the run ended: it is not
            # the end of the run and must not be mistaken for it
            row["last_activity"] = float(ended_at) - 30.0
        (lane_dir / "state.json").write_text(json.dumps(row))

    def finish_run(self, run, lane=LANE, lane_state="busy", ended_at=None):
        """The executor files of a run that ended (its result and its status)."""
        ended = self.clock.now if ended_at is None else float(ended_at)
        run_dir = self.fleet.home / "runs" / run
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.json").write_text(json.dumps({
            "id": run, "status": "done", "branch": None, "commit": None,
            "workspace": {"repo": "/tmp/fleet"}, "backend": "pi", "model": "pi",
            "error_class": None, "result": {"summary": "", "tests": ""},
            "finished_at": ended, "ended_at": ended}))
        (run_dir / "status.json").write_text(json.dumps({
            "id": run, "state": "finished", "status": "done", "finished": ended,
            "ended_at": ended, "updated": ended}))
        self.set_lane(lane, lane_state, run, ended)
        return ended

    def write_heartbeat(self, at=None):
        """Write the heartbeat of the dispatcher: the stamp of its last life."""
        at = self.clock.now if at is None else float(at)
        path = self.state_dir / "heartbeat"
        path.write_text(self.clock.stamp(at) + "\n")
        os.utime(path, (at, at))

    def stale_heartbeat(self, seconds):
        """Pretend the dispatcher was down for `seconds` (its heartbeat is that old)."""
        self.write_heartbeat(self.clock.now - seconds)

    def advance(self, seconds, alive=True):
        """Move the clock; a dispatcher that stays alive refreshes its heartbeat."""
        self.clock.advance(seconds)
        if alive:
            self.write_heartbeat()

    def tick(self, state=None):
        """One tick of the real dispatcher against the fake fleet."""
        state = self.state if state is None else state
        self.module.tick(self.lanes, state, 0)
        return state

    def health(self):
        """The health snapshot of the lanes (lanes-status.json)."""
        path = self.state_dir / "lanes-status.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def brain(self):
        """The brain log of the dispatcher."""
        return self.brain_path.read_text() if self.brain_path.exists() else ""

    def idle_lines(self, lane):
        """The brain log lines about the idle gap of `lane` (oldest first)."""
        return [line for line in self.brain().splitlines()
                if lane.lower() in line.lower()
                and any(word in line.lower() for word in REASON_WORDS)]

    def idle_line(self, lane):
        """The last brain log line about the idle gap of `lane` (None when there is none)."""
        lines = self.idle_lines(lane)
        return lines[-1] if lines else None

    # --- the re-dispatch -----------------------------------------------------------------
    def test_the_lane_gets_its_next_card_in_the_tick_where_its_run_ended(self):
        state = self.running()
        # the run ended: the host has not reaped the lane yet, it still calls it busy
        self.finish_run("run-42", lane=LANE, lane_state="busy")
        self.tick(state)
        self.assertEqual(state.get(LANE, {}).get("issue"), 43,
                         "the lane whose run just ended must get its next card in the same tick")
        self.assertIn("in-progress", self.fleet.issues[43]["labels"])

    def test_a_paused_lane_gets_no_card_after_its_run_ended(self):
        # the lane was paused while its run was running
        (self.state_dir / ("PAUSE-%s" % LANE)).write_text("")
        self.add_lane(OTHER_LANE, OTHER_HOST, 44)
        state = self.running()
        state.update(self.running(lane=OTHER_LANE, issue=44, run="run-44", host=OTHER_HOST))
        self.finish_run("run-42", lane=LANE, lane_state="busy")
        self.finish_run("run-44", lane=OTHER_LANE, lane_state="busy")
        self.tick(state)
        self.assertNotEqual((state.get(LANE) or {}).get("issue"), 43,
                            "a paused lane must not be dispatched")
        self.assertEqual(state.get(OTHER_LANE, {}).get("issue"), 43,
                         "the free lane must get its card in the same tick")

    def test_a_lane_of_a_backed_off_host_gets_no_card_after_its_run_ended(self):
        self.add_lane(OTHER_LANE, OTHER_HOST, 44)
        state = self.running()
        state.update(self.running(lane=OTHER_LANE, issue=44, run="run-44", host=OTHER_HOST))
        # the host of the lane is in backoff: it is not polled at all
        self.module.BACKOFF[HOST] = {"n": 2, "until": self.clock.now + 600}
        self.finish_run("run-42", lane=LANE, lane_state="busy")
        self.finish_run("run-44", lane=OTHER_LANE, lane_state="busy")
        self.tick(state)
        self.assertNotEqual((state.get(LANE) or {}).get("issue"), 43,
                            "a lane of a backed off host must not be dispatched")
        self.assertEqual(state.get(OTHER_LANE, {}).get("issue"), 43,
                         "the lane of the healthy host must get its card in the same tick")

    def test_without_a_card_the_lane_stays_idle_without_an_error_or_a_loop(self):
        # the pool is empty
        self.fleet.empty_pool()
        state = self.running()
        self.finish_run("run-42", lane=LANE, lane_state="busy")
        self.tick(state)
        self.assertNotEqual((state.get(LANE) or {}).get("issue"), 43,
                            "no card in the pool: the lane stays idle")
        # the gap keeps growing while the lane waits for a card
        self.advance(IDLE_LOG_MIN_S * 2)
        self.tick(state)
        gaps = lane_numbers(self.health(), LANE)
        self.assertTrue(gaps, "the idle gap of the waiting lane must be visible in the health output")
        self.assertGreaterEqual(max(value for _, value in gaps), IDLE_LOG_MIN_S * 2 - 5.0)
        # a second tick without a card neither dispatches nor loops
        self.advance(TICK_S)
        self.tick(state)
        self.assertNotEqual((state.get(LANE) or {}).get("issue"), 43,
                            "no card in the pool: the lane stays idle")

    # --- the idle metric -----------------------------------------------------------------
    def test_the_idle_gap_is_measured_from_the_real_end_of_the_run(self):
        state = self.running()
        # the run ended ten minutes ago, the harvest comes late: the gap is ten minutes
        self.finish_run("run-42", lane=LANE, lane_state="busy", ended_at=self.clock.now - 600)
        self.tick(state)
        gaps = lane_numbers(self.health(), LANE)
        self.assertTrue(gaps, "the health output must carry the last idle gap of every lane")
        gap = max(value for _, value in gaps)
        self.assertGreaterEqual(gap, 595.0,
                                "the idle gap must be measured from the end of the run, not from the harvest")
        self.assertLessEqual(gap, 660.0, "the idle gap must not be longer than the real gap")

    def test_the_remote_status_reports_the_end_time_of_the_run(self):
        ended = self.clock.now - 600
        self.finish_run("run-42", lane=LANE, lane_state="busy", ended_at=ended)
        status = self.fleet.remote(LANE, self.lanes[LANE], "run-42")
        run = status.get("run")
        self.assertIsInstance(run, dict, "the remote status must carry the finished run")
        found = []
        for path, value in walk(run):
            if not END_TIME_KEY_RE.search(path[-1]):
                continue
            stamp = to_epoch(value)
            if stamp is None:
                continue
            found.append((path, stamp))
        self.assertTrue(found, "the remote status must carry the end time of the run: %r" % (run,))
        path, stamp = found[0]
        self.assertAlmostEqual(stamp, ended, delta=5.0,
                               msg="the end time of the run is wrong: %r" % (path,))

    def test_a_gap_of_more_than_five_minutes_is_logged_with_its_reason(self):
        state = self.running()
        # the run ended ten minutes ago while the dispatcher was down: the gap is long
        self.stale_heartbeat(900)
        self.finish_run("run-42", lane=LANE, lane_state="busy", ended_at=self.clock.now - 600)
        self.tick(state)
        line = self.idle_line(LANE)
        self.assertIsNotNone(line, "a gap of more than five minutes must be written to the brain log")
        self.assertTrue(any(word in line.lower() for word in REASON_WORDS),
                        "the brain log line must name the reason of the gap: %r" % (line,))

    def test_the_downtime_of_the_dispatcher_explains_a_long_gap(self):
        # the dispatcher was down while the run ended: the gap is its downtime
        self.stale_heartbeat(900)
        state = self.running()
        self.finish_run("run-42", lane=LANE, lane_state="busy", ended_at=self.clock.now - 900)
        self.tick(state)
        reason = lane_reason(self.health(), LANE) or self.idle_line(LANE) or ""
        self.assertTrue(reason, "the health output must name the reason of the gap")
        self.assertIn("dispatcher", reason.lower(),
                      "the gap must be explained by the downtime of the dispatcher")
        gaps = lane_numbers(self.health(), LANE)
        self.assertGreaterEqual(max((value for _, value in gaps), default=0.0), 895.0,
                                "the gap must not be shortened to the harvest time")

    def test_the_next_idle_window_does_not_inherit_the_downtime_of_the_previous_one(self):
        # window 1: the dispatcher was down when the run ended
        self.stale_heartbeat(900)
        state = self.running()
        self.finish_run("run-42", lane=LANE, lane_state="busy", ended_at=self.clock.now - 900)
        self.tick(state)
        first = lane_reason(self.health(), LANE)
        self.assertIn("dispatcher", first.lower(), "the first gap is the downtime of the dispatcher")
        # the lane runs its next card while the pool is empty
        self.fleet.empty_pool()
        self.advance(TICK_S)
        self.finish_run("run-43", lane=LANE, lane_state="busy")
        self.tick(state)
        # the host reaps the lane: it waits for a card that the empty pool does not have
        self.advance(IDLE_LOG_MIN_S * 2)
        self.tick(state)
        # a card shows up: the second window closes and names its own reason
        self.fleet.add_issue(45)
        self.tick(state)
        second = lane_reason(self.health(), LANE)
        self.assertTrue(second, "the health output must name the reason of the gap")
        self.assertIn("card", second.lower(), "the second gap is explained by the empty pool")
        self.assertNotIn("dispatcher", second.lower(),
                         "the downtime of the first window must not explain the second one")
        line = self.idle_line(LANE)
        self.assertIsNotNone(line, "a gap of more than five minutes must be written to the brain log")
        self.assertNotIn("dispatcher", line.lower(),
                         "the downtime of the first window must not explain the second one")


if __name__ == "__main__":
    unittest.main(verbosity=2)
