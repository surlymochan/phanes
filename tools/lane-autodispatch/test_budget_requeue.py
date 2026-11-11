#!/usr/bin/env python3
"""Reproduction of the budget_exceeded requeue strategy of the autodispatcher (Refs #1668).

A run that dies because a backend budget window was spent (the MiniMax 5 hour Token
Plan window, for example) is not a card failure: the card never got a fair try and the
same card dies again while the window is closed.  The harvest used to label *every* run
that ended without a commit ``lane-failed``, so a budget death was mixed with a real
failure and the card had to be requeued by hand.

The contract of the issue:

  * the harvest tells a budget failure from a real failure: only a real failure keeps
    the ``lane-failed`` flow;
  * a budget-failed card is held instead of being labelled: it returns to the queue by
    itself once the backend window recovered;
  * the window state is asked for.  The real executor writes no top level ``resets_at``
    (see the review of PR #1679), so a fixed delay is not enough: a card must not go
    back into the queue while its window is still closed, and it stays held while the
    state of the window cannot be confirmed;
  * the number of budget failures of a card survives a requeue, so the configured cap
    is reached after the card died that often in a row, and the bookkeeping of a card is
    cleaned up when the card leaves the hold for good.

The tests drive the real ``tick()`` of the dispatcher against a fake fleet (no network,
no ssh, no GitHub) and they read the labels of the card and the state of the
dispatcher: the whole flow of a harvest (a run that ends without a commit) is covered,
not only a single function call.

The window state is published to the dispatcher through every channel a dispatcher may
use (a probe command, a command that asks for the quota of the backend, the usage file of
the run and the text of the run): a test must not depend on the channel the
implementation picks, only on the decision the dispatcher takes.

On the baseline the four tests of the budget flow fail: the dispatcher knows no budget
class at all, it labels every run that ended without a commit ``lane-failed``.  The two
tests that guard the old flow (a real failure is still a card failure, a card that
leaves the hold leaves no bookkeeping behind) pass on the baseline and must keep passing.

Run it offline:

    cd tools/lane-autodispatch && python3 -m unittest test_budget_requeue -v
"""
from __future__ import annotations

import importlib.util
import json
from card_fixtures import fixture_json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time as time_module
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("autodispatch.py")
# The scratch of the test: the TMPDIR of the run (the repo keeps the scratch of the
# tools inside the worktree, see tools/check-ad-base-fetch-on-miss.py), the scratch
# root of the repo as the fallback.
SCRATCH = Path(os.environ.get("TMPDIR") or (Path(__file__).resolve().parents[2] / ".swrm-tmp"))
LANE = "M17"
HOST = "lane-host"
ISSUE = 1392
# The clock of the test starts at a whole second, so that the stamps of the dispatcher
# ("%F %T") and the mtimes round-trip exactly.
START = time_module.mktime(time_module.strptime("2026-11-02 16:53:00", "%Y-%m-%d %H:%M:%S"))
# The labels that keep a card out of the dispatchable queue (the rule of the dispatcher).
BUSY_LABELS = {"in-progress", "lane-pr", "lane-failed"}
# The words of a window query: a command that asks for the window of the backend.
WINDOW_WORDS = ("budget", "probe", "window", "quota", "usage", "rate_limit", "rate-limit", "resets")
# The name of the field that remembers how often a card died on a budget window.
COUNT_RE = re.compile(r"tries|count|failures|attempts|hits|times", re.I)


def load_module(name):
    """Load a fresh copy of the autodispatcher (every test patches its own copy)."""
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dispatchable(labels):
    """True when the dispatcher may hand the card to a lane (the rule of the dispatcher)."""
    return "lane-ready" in labels and not labels & BUSY_LABELS


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
        at = self.now if at is None else float(at)
        return self.strftime("%Y-%m-%d %H:%M:%S", self.localtime(at))

    def __getattr__(self, name):
        return getattr(time_module, name)


class Fleet:
    """The fake fleet: the issue table, the `gh` calls, the `sh` calls, the executor home."""

    def __init__(self, root, clock):
        self.root = root
        self.clock = clock
        self.home = root / "executor"
        self.issues = {
            ISSUE: {"title": "#%d the card of %s" % (ISSUE, LANE), "body": "",
                    "labels": ["lane-ready", "in-progress"]},
        }
        self.dispatches = []       # the cards the dispatcher handed to a lane
        self.window_exhausted = True
        self.window_resets_at = None
        self.window_reads = 0      # how often the dispatcher asked for the window state

    # --- the window state of the backend --------------------------------------------------
    def window_answer(self):
        """The answer of the backend: is the window closed, and when does it recover?"""
        exhausted = bool(self.window_exhausted)
        resets_at = self.window_resets_at
        row = {"exhausted": exhausted, "resets_at": resets_at}
        return {"exhausted": exhausted, "resets_at": resets_at,
                "window": dict(row), "quotas": dict(row),
                "five_hour": {"used_percentage": 100 if exhausted else 0, "resets_at": resets_at},
                "seven_day": {"used_percentage": 100 if exhausted else 0, "resets_at": resets_at}}

    def window_closed(self, resets_at=None):
        """The window of the backend is spent (the default of the test)."""
        self.window_exhausted = True
        self.window_resets_at = resets_at
        self.write_window_file()

    def window_open(self, resets_at=None):
        """The window of the backend recovered."""
        self.window_exhausted = False
        self.window_resets_at = resets_at
        self.write_window_file()

    def write_window_file(self):
        """The window state the executor home publishes (the usage file of the run)."""
        path = self.root / "window.json"
        path.write_text(json.dumps(self.window_answer()))

    # --- the AD's `gh` --------------------------------------------------------------------
    def gh(self, args, timeout=60):
        if args[:2] == ["issue", "list"]:
            rows = []
            for number, issue in sorted(self.issues.items()):
                labels = list(issue["labels"])
                if "lane-ready" in args and "lane-ready" not in labels:
                    continue
                rows.append({"number": number, "title": issue["title"], "body": issue["body"],
                             "labels": labels, "state": "OPEN"})
            return 0, fixture_json(rows), ""
        if args[:2] == ["issue", "view"]:
            number = int(args[2])
            issue = self.issues.get(number)
            if issue is None:
                return 1, "", "gh: no such issue"
            return 0, json.dumps({"number": number, "state": "OPEN",
                                  "labels": [{"name": name} for name in issue["labels"]]}), ""
        if args[:2] == ["issue", "edit"]:
            number = int(args[2])
            issue = self.issues.get(number)
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
        if args[:2] == ["pr", "create"]:
            return 0, "https://github.com/surlymochan/phanes/pull/1\n", ""
        return 0, "", ""

    # --- the AD's `sh` --------------------------------------------------------------------
    def sh(self, cmd, timeout=120, inp=None):
        command = cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd)
        if "ls-remote" in command:
            return 0, "%s\trefs/heads/main\n" % ("a" * 40), ""
        if "cat > /tmp/task" in command:
            return 0, "", ""            # the task upload
        if "lane assign" in command:
            match = re.search(r"#(\d+)", command)
            number = int(match.group(1)) if match else 0
            self.dispatches.append(number)
            return 0, json.dumps({"id": "run-%d-%d" % (number, len(self.dispatches))}), ""
        if "cat-file" in command or "fetch -q" in command:
            return 0, "", ""            # the base commit check and its fetch
        if "git " in command:
            return 0, "", ""            # the push and the log of the harvest
        if any(word in command.lower() for word in WINDOW_WORDS):
            self.window_reads += 1
            return 0, json.dumps(self.window_answer()), ""
        return 0, "", ""

    # --- the AD's `remote` ----------------------------------------------------------------
    def remote(self, lane, cfg, run=""):
        """The status of the lane, read by the real remote snippet of the dispatcher.

        The snippet runs against the fake executor home, so the harvest sees the very
        files the executor writes (a result.json without a top level `resets_at`).
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

    # --- the executor home ---------------------------------------------------------------
    def set_lane(self, lane, lane_state, run=None, ended_at=None):
        """The lane state of the executor (what the host reports about the lane)."""
        lane_dir = self.home / "lanes" / lane
        lane_dir.mkdir(parents=True, exist_ok=True)
        row = {"state": lane_state}
        if run:
            row["current_run"] = run
        if ended_at is not None:
            row["last_activity"] = float(ended_at) - 30.0
        row["budget"] = self.window_answer()
        (lane_dir / "state.json").write_text(json.dumps(row))

    def end_run(self, run, status="budget_exceeded", error_class="budget_exceeded", summary=""):
        """The executor files of a run that ended without a commit."""
        ended = self.clock.now
        run_dir = self.home / "runs" / run
        run_dir.mkdir(parents=True, exist_ok=True)
        result = {"id": run, "status": status, "branch": None, "commit": None,
                  "workspace": {"repo": "/tmp/fleet"}, "backend": "pi", "model": "pi",
                  "error_class": error_class,
                  "result": {"summary": summary, "tests": ""},
                  "finished_at": ended, "ended_at": ended}
        # The real executor writes no top level `resets_at` (the review of PR #1679):
        # the recovery time of the window has to come from somewhere else.
        (run_dir / "result.json").write_text(json.dumps(result))
        (run_dir / "status.json").write_text(json.dumps({"id": run, "state": "finished",
                                                          "status": status, "finished": ended,
                                                          "ended_at": ended, "updated": ended}))
        (run_dir / "usage.json").write_text(json.dumps(self.window_answer()))
        self.set_lane(LANE, "busy", run, ended)
        return ended


class BudgetRequeueTest(unittest.TestCase):
    """The budget failures of a card: the classification, the hold and the requeue."""

    def setUp(self):
        self.root = Path(self.scratch())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.clock = Clock()
        # The cap of the consecutive budget failures of a card: the environment wins, so
        # the dispatcher and the test count the same failures.
        old_max = os.environ.get("AD_BUDGET_REQUEUE_MAX")
        os.environ["AD_BUDGET_REQUEUE_MAX"] = "2"
        self.addCleanup(self._restore_env, "AD_BUDGET_REQUEUE_MAX", old_max)
        self.module = load_module("autodispatch_budget_%d" % id(self))
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
        # the probe of the window state: a command that answers with the window of the
        # backend (the channel the dispatcher may use to ask for it)
        self.fleet.write_window_file()
        probe = self.root / "window-probe.sh"
        probe.write_text("#!/bin/sh\ncat %s\n" % shlex.quote(str(self.root / "window.json")))
        os.chmod(str(probe), 0o755)
        old_probe = os.environ.get("AD_BUDGET_PROBE_CMD")
        os.environ["AD_BUDGET_PROBE_CMD"] = shlex.quote(str(probe))
        self.addCleanup(self._restore_env, "AD_BUDGET_PROBE_CMD", old_probe)
        self.lanes = {LANE: {"host": HOST, "backend": "pi", "repo": "/tmp/fleet",
                             "fleet": "/tmp/swrm-fleet"}}
        self.state = {}

    def scratch(self):
        """A private scratch directory of the test.

        The scratch root of the worktree can disappear while the tests run (the
        sandbox of swrm removes it), so it is created again instead of failing.
        """
        for _ in range(5):
            try:
                SCRATCH.mkdir(parents=True, exist_ok=True)
                return tempfile.mkdtemp(prefix="ad-budget-", dir=str(SCRATCH))
            except FileNotFoundError:
                continue
        raise AssertionError("the scratch root of the budget test is gone")

    def _restore_home(self, old_home):
        if old_home is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = old_home

    def _restore_env(self, name, old):
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old

    # --- helpers -------------------------------------------------------------------------
    def running(self, issue=ISSUE, run="run-1"):
        """The state of a lane that runs a card."""
        return {LANE: {"issue": issue, "run": run, "host": HOST, "base": "a" * 40,
                       "at": self.clock.stamp(), "verify": None}}

    def current_run(self, state):
        """The run the lane is working on ("" when the lane is idle)."""
        return (state.get(LANE) or {}).get("run", "")

    def tick(self, state=None):
        """One tick of the real dispatcher against the fake fleet."""
        state = self.state if state is None else state
        self.module.tick(self.lanes, state, 0)
        return state

    def advance(self, seconds):
        """Move the clock; the dispatcher stays alive."""
        self.clock.advance(seconds)
        (self.state_dir / "heartbeat").write_text(self.clock.stamp() + "\n")

    def labels(self, issue=ISSUE):
        """The labels the card carries in GitHub."""
        return set(self.fleet.issues[issue]["labels"])

    def budget_cap(self):
        """The number of consecutive budget failures the dispatcher tolerates per card."""
        for name in dir(self.module):
            if not re.search(r"BUDGET.*MAX|MAX.*BUDGET|MAX.*HOLD|HOLD.*MAX", name):
                continue
            value = getattr(self.module, name)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return 2

    def holds(self, state):
        """Every place of the state that remembers the card (the holds and the counters).

        The dispatcher may keep the hold in the state or in a file of its state dir; both
        are looked at, so the test does not depend on the name of the field.
        """
        found = []

        def walk(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    if str(key) == str(ISSUE) and isinstance(value, (dict, int, float)):
                        found.append((path + (key,), value))
                    elif isinstance(value, dict) and value.get("issue") == ISSUE:
                        found.append((path + (key,), value))
                    walk(value, path + (key,))
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    if isinstance(value, dict) and value.get("issue") == ISSUE:
                        found.append((path + (index,), value))
                    walk(value, path + (index,))

        for key, value in state.items():
            if key in self.lanes:
                continue          # the state of the lane itself is not a hold
            walk(value, (key,))
        for path in sorted(self.state_dir.glob("*.json")):
            if path.name == "lanes-status.json":
                continue
            try:
                saved = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(saved, dict):
                continue
            for key, value in saved.items():
                if key in self.lanes:
                    continue      # the state of the lane itself is not a hold
                walk(value, (path.name, key))
        return found

    def counter(self, state):
        """The number of budget failures the dispatcher remembers for the card (0 when none)."""
        best = 0
        for _, value in self.holds(state):
            rows = value if isinstance(value, dict) else {"": value}
            for key, item in rows.items():
                if COUNT_RE.search(str(key)) and isinstance(item, int) and not isinstance(item, bool):
                    best = max(best, item)
        return best

    # --- the classification ---------------------------------------------------------------
    def test_a_budget_failure_is_not_a_card_failure(self):
        """A run that died on a spent window is held, not labelled lane-failed."""
        state = self.running()
        self.fleet.end_run("run-1", summary="Token Plan 用量上限")

        self.tick(state)

        labels = self.labels()
        self.assertNotIn("lane-failed", labels,
                         "a spent budget window is not a card failure: the card must be held")
        self.assertFalse(dispatchable(labels),
                         "the card must not go back into the queue while its window is closed")
        self.assertNotIn(LANE, state, "the lane slot must be free again after the harvest")
        self.assertEqual(self.fleet.dispatches, [],
                         "the card must not run again while the window of the backend is closed")
        self.assertTrue(self.holds(state),
                        "the dispatcher must remember the held card (and how often it failed)")

    def test_a_real_failure_keeps_the_lane_failed_flow(self):
        """A run that really failed is still a card failure (the old flow)."""
        state = self.running()
        self.fleet.end_run("run-1", status="failed", error_class="exec_error", summary="boom")

        self.tick(state)

        self.assertIn("lane-failed", self.labels(),
                      "a real failure keeps the lane-failed flow")
        self.assertEqual(self.holds(state), [],
                         "a real failure must not leave a hold behind")

    def test_exec_error_overrides_budget_words_in_the_summary(self):
        """Task wording cannot turn an explicitly classified execution error into a hold."""
        state = self.running()
        self.fleet.end_run("run-1", status="failed", error_class="exec_error",
                           summary="Could not implement budget window exhausted handling: tests failed")

        self.tick(state)

        self.assertIn("lane-failed", self.labels())
        self.assertNotIn(LANE, state, "a real failure must release its lane")
        self.assertEqual(self.holds(state), [], "a real failure must not retain budget history")
        state = json.loads((self.state_dir / "state.json").read_text())
        self.fleet.window_open()
        self.advance(2 * 3600)
        self.tick(state)

        self.assertIn("lane-failed", self.labels())
        self.assertEqual(self.fleet.dispatches, [], "window recovery must not retry a real failure")

    def test_non_budget_class_overrides_conflicting_budget_status(self):
        """An explicit quota classification takes precedence over other budget hints."""
        state = self.running()
        self.fleet.end_run("run-1", status="budget_exceeded", error_class="rate_limit",
                           summary="budget_exceeded: budget window exhausted")

        self.tick(state)

        self.assertIn("lane-failed", self.labels())
        self.assertNotIn(LANE, state)
        self.assertEqual(self.holds(state), [])

    def test_budget_hints_are_used_only_without_an_explicit_error_class(self):
        for error_class in (None, ""):
            for status, summary in (("budget_exceeded", ""),
                                    ("failed", "budget window exhausted")):
                with self.subTest(error_class=error_class, status=status):
                    self.assertTrue(self.module.budget_failure(
                        {"status": status, "error_class": error_class, "summary": summary}))

    # --- the window state -----------------------------------------------------------------
    def test_the_card_stays_held_while_the_window_is_exhausted(self):
        """The window is still closed: the card waits, it is not requeued on a timer.

        The real executor writes no top level `resets_at`, so the dispatcher has to ask
        for the state of the window.  A fixed delay is not enough: after two hours (far
        more than any fallback delay) the card is still held while the window is closed.
        """
        state = self.running()
        self.fleet.window_closed()
        self.fleet.end_run("run-1", summary="Token Plan 用量上限")

        self.tick(state)
        self.advance(2 * 3600)
        self.tick(state)

        labels = self.labels()
        self.assertNotIn("lane-failed", labels)
        self.assertFalse(dispatchable(labels),
                         "the card must stay held while the window of the backend is closed")
        self.assertEqual(self.fleet.dispatches, [],
                         "the card must not go back into the queue while its window is closed")

    def test_the_card_returns_to_the_queue_when_the_window_recovered(self):
        """The window recovered: the card goes back into the queue by itself."""
        state = self.running()
        self.fleet.window_closed()
        self.fleet.end_run("run-1", summary="Token Plan 用量上限; the window resets in 5m")

        self.tick(state)
        self.assertFalse(dispatchable(self.labels()),
                         "the card waits for the window of the backend")

        self.fleet.window_open()
        self.advance(10 * 60)
        self.tick(state)

        labels = self.labels()
        self.assertIn("lane-ready", labels,
                      "the card must return to the queue once the window recovered")
        self.assertNotIn("lane-failed", labels,
                         "a budget failure is never a card failure")
        self.assertEqual(len(self.fleet.dispatches), 1,
                         "the card must run again after the window recovered")

    # --- the count across a requeue --------------------------------------------------------
    def test_the_budget_failure_count_survives_a_requeue(self):
        """The card dies on the window again: the second failure counts (B2).

        The count of the budget failures of a card must survive the requeue, otherwise
        the cap is never reached and the card is dispatched for ever.
        """
        state = self.running()
        cap = self.budget_cap()
        counts = []
        for _ in range(cap):
            run = self.current_run(state)
            self.fleet.end_run(run, summary="Token Plan 用量上限; the window resets in 5m")
            self.tick(state)
            counts.append(self.counter(state))
            self.assertNotIn("lane-failed", self.labels(),
                             "the card is held while the window is closed")
            self.fleet.window_open()
            self.advance(10 * 60)
            self.tick(state)

        # the card ran again after the requeue: the count of the failures was not reset
        self.assertGreaterEqual(max(counts), 2,
                                "the number of budget failures of the card must survive a requeue")
        self.assertLess(len(self.fleet.dispatches), cap,
                        "the cap of the consecutive budget failures must be enforced")
        self.assertIn("lane-failed", self.labels(),
                      "a card that died on the window too often fails for good")
        self.assertFalse(dispatchable(self.labels()))

    def test_the_bookkeeping_of_a_card_is_cleaned_up_when_it_leaves_the_hold(self):
        """The hold and the counter of a card are gone when the card fails for good."""
        state = self.running()
        cap = self.budget_cap()
        for _ in range(cap + 1):
            run = self.current_run(state)
            if not run:
                break             # the card is not running any more: it failed for good
            self.fleet.end_run(run, summary="Token Plan 用量上限; the window resets in 5m")
            self.tick(state)
            self.fleet.window_open()
            self.advance(10 * 60)
            self.tick(state)
            if "lane-failed" in self.labels():
                break

        self.assertIn("lane-failed", self.labels())
        self.assertEqual(self.holds(state), [],
                         "the bookkeeping of a card that failed for good must be cleaned up")

    def write_health(self, minimax, age=0):
        path = self.state_dir / "health.json"
        path.write_text(json.dumps({"minimax": dict(minimax, observed_at=self.clock.time() - age)}))
        stamp = self.clock.time() - age
        os.utime(path, (stamp, stamp))

    def test_unobserved_health_hold_expires_once(self):
        os.environ.pop("AD_BUDGET_PROBE_CMD", None)
        old = os.environ.get("AD_BUDGET_HOLD_MAX_HOURS")
        os.environ["AD_BUDGET_HOLD_MAX_HOURS"] = "1"
        self.addCleanup(self._restore_env, "AD_BUDGET_HOLD_MAX_HOURS", old)
        for observed in (None, self.clock.time() - 1000):
            with self.subTest(observed_at=observed):
                self.fleet.issues[ISSUE]["labels"] = ["lane-ready", "in-progress"]
                state = self.running()
                self.fleet.end_run("run-1")
                self.tick(state)
                row = {"interval_pct": 80, "weekly_pct": 80}
                if observed is not None:
                    row["observed_at"] = observed
                (self.state_dir / "health.json").write_text(json.dumps({"minimax": row}))
                self.advance(3599)
                self.tick(state)
                self.assertIn("in-progress", self.labels())
                self.advance(2)
                messages = []
                original_log = self.module.log
                self.module.log = lambda message, **kwargs: messages.append(message)
                try:
                    self.tick(state)
                    self.assertIn("lane-failed", self.labels())
                    self.assertNotIn("in-progress", self.labels())
                    self.tick(state)
                finally:
                    self.module.log = original_log
                self.assertEqual(sum("hold_expired" in msg for msg in messages), 1)
                self.assertEqual(self.holds(state), [])

    def test_health_file_rewrite_does_not_refresh_quota_observation(self):
        os.environ.pop("AD_BUDGET_PROBE_CMD", None)
        self.write_health({"interval_pct": 80, "weekly_pct": 80}, age=300)
        state = self.running()
        self.fleet.end_run("run-1")
        self.tick(state)
        self.advance(1)
        path = self.state_dir / "health.json"
        data = json.loads(path.read_text())
        data["executor_tasks"] = {"worker": {}}
        path.write_text(json.dumps(data))
        self.tick(state)
        self.assertEqual(self.fleet.dispatches, [])
        self.assertIn("in-progress", self.labels())

    def test_default_health_source_releases_only_after_both_windows_recover(self):
        """No operator probe is needed for the deployed MiniMax remains snapshot."""
        os.environ.pop("AD_BUDGET_PROBE_CMD", None)
        state = self.running()
        self.fleet.end_run("run-1")
        self.tick(state)
        for short, weekly in ((0, 80), (80, 0)):
            self.advance(2 * 3600)
            self.write_health({"interval_pct": short, "weekly_pct": weekly})
            self.tick(state)
            self.assertEqual(self.fleet.dispatches, [])
            self.assertIn("in-progress", self.labels())
        self.write_health({"interval_pct": 80, "weekly_pct": 80})
        self.tick(state)
        self.assertEqual(self.fleet.dispatches, [ISSUE])
        self.assertNotIn("lane-failed", self.labels())

    def test_default_health_source_rejects_stale_or_unknown_windows(self):
        os.environ.pop("AD_BUDGET_PROBE_CMD", None)
        state = self.running()
        self.fleet.end_run("run-1")
        self.tick(state)
        for row, age in (({"interval_pct": 80, "weekly_pct": 80}, 901),
                         ({"weekly_pct": 80}, 0), ({"interval_pct": None, "weekly_pct": 80}, 0)):
            self.write_health(row, age)
            self.tick(state)
            self.assertEqual(self.fleet.dispatches, [])
            self.assertIn("in-progress", self.labels())

    def test_default_health_source_requires_a_snapshot_after_the_hold(self):
        """A recent snapshot from before the budget failure cannot prove recovery."""
        os.environ.pop("AD_BUDGET_PROBE_CMD", None)
        opened = {"interval_pct": 80, "weekly_pct": 80}
        self.write_health(opened, age=300)
        state = self.running()
        self.fleet.end_run("run-1")
        self.tick(state)
        hold = dict(state["_budget_requeue"][str(ISSUE)])
        for age in (300, 0):
            with self.subTest(snapshot_age=age):
                self.write_health(opened, age=age)
                state = json.loads((self.state_dir / "state.json").read_text())
                self.tick(state)
                self.assertEqual(state["_budget_requeue"].get(str(ISSUE)), hold)
                self.assertIn("in-progress", self.labels())
                self.assertEqual(self.fleet.dispatches, [])
        self.advance(1)
        self.write_health(opened)
        self.tick(state)
        self.assertNotIn(str(ISSUE), state["_budget_requeue"])
        self.assertEqual(self.fleet.dispatches, [ISSUE])
        self.assertNotIn("lane-failed", self.labels())

    def test_probe_selects_the_target_lane_and_backend(self):
        state = self.running()
        self.fleet.end_run("run-1")
        self.tick(state)
        window_file = self.root / "window.json"
        unknown = {"five_hour": None}
        opened = {"five_hour": {"used_percentage": 0}}
        for payload in (
            {"pi": unknown, "codex": opened},
            {"backends": {"pi": unknown, "codex": opened}},
            {"lanes": {LANE: unknown, "CX1": opened}},
            {"backend": "codex", **opened},
            {"lane": "M18", **opened},
            {"pi": {"five_hour": {"used_percentage": 0}, "seven_day": None}},
        ):
            with self.subTest(payload=payload):
                window_file.write_text(json.dumps(payload))
                self.tick(state)
                self.assertEqual(self.fleet.dispatches, [])
                self.assertIn("in-progress", self.labels())
        window_file.write_text(json.dumps({"pi": opened, "codex": unknown}))
        self.tick(state)
        self.assertEqual(self.fleet.dispatches, [ISSUE])

    def test_terminal_label_failure_is_retried_after_state_reload(self):
        """Harvest, requeue, redispatch, hit the cap, then recover a failed label write."""
        state = self.running()
        self.fleet.end_run("run-1")
        self.tick(state)
        self.fleet.window_open()
        self.tick(state)
        self.assertEqual(self.fleet.dispatches, [ISSUE])
        state = json.loads((self.state_dir / "state.json").read_text())
        self.fleet.end_run(self.current_run(state))
        self.tick(state)
        self.assertNotIn(LANE, state)
        self.assertGreaterEqual(self.counter(state), 2)
        gh = self.fleet.gh
        failed_writes = []

        def fail_terminal_label(args, timeout=60):
            if args[:2] == ["issue", "edit"] and "lane-failed" in args:
                failed_writes.append(list(args))
                return 1, "", "temporary GitHub failure"
            return gh(args, timeout)

        self.module.gh = fail_terminal_label
        for _ in range(2):
            state = json.loads((self.state_dir / "state.json").read_text())
            self.tick(state)
            self.assertIn("in-progress", self.labels())
            self.assertNotIn("lane-failed", self.labels())
            self.assertGreaterEqual(self.counter(state), 2)
            self.assertTrue(state["_budget_requeue"][str(ISSUE)]["terminal_pending"])
            self.assertNotIn(ISSUE, state["_done"])
            self.assertEqual(self.fleet.dispatches, [ISSUE])
        self.assertEqual(len(failed_writes), 2)
        self.module.gh = gh
        state = json.loads((self.state_dir / "state.json").read_text())
        self.tick(state)
        self.assertIn("lane-failed", self.labels())
        self.assertNotIn("in-progress", self.labels())
        self.assertEqual(self.holds(state), [])
        self.assertIn(ISSUE, state["_done"])


if __name__ == "__main__":
    unittest.main()
