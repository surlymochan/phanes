#!/usr/bin/env python3
"""Reproduction of the missing verify-queue metrics (Refs #1989, parent #1785).

The verification reservation of the queue already exists (`AD_VERIFY_SHARE_ENABLED`,
`queue_class_limits()`, the work-class cap of #1864), but nothing measures it: the
state of the dispatcher carries no cycle of the verification cards, no wait time and
no proof that the reserved share was really used.  #1785 (P2-3) asks for the
reserved share to be provable and #1989 asks for the numbers of the verification
queue:

  * four time points per verify card: `queued`, `dispatched`, `evidence_ready`,
    `verdict` — the wait is `dispatched - queued`, the cycle is `verdict - queued`;
  * the health snapshot carries the wait/cycle histogram buckets, the verify deficit
    (the reserved verify lanes that are still free), the share the verify class
    really got and the number of times the work class hit its cap;
  * the numbers survive a restart: they are restored from the state, not counted
    again from zero.

The contract that the tests below pin (the implementation follows this file):

  * the time points live in the persistent state of the dispatcher, keyed by the
    issue number of the card, so a restart finds them again;
  * the snapshot (`lanes-status.json`) carries the metrics under keys that mention
    `verify` and `wait` / `cycle` / `deficit` / `share`, and the work-class cap
    under a key that mentions `cap`;
  * the verify deficit is the `verify_slots` of the admission (the reserved verify
    lanes that are still free) — the same number the queue-share log line prints;
  * the actual share is the share of the dispatches the verify class really got
    (verify dispatches / all dispatches of the measured window);
  * the wait histogram has one sample per dispatched verify card, the cycle
    histogram one sample per verify card with a verdict; an ordinary work card
    never enters either of them;
  * the verdict of a card is a fact of the merge gate, not of the dispatcher: the
    dispatcher observes it on the card (the card is closed or it carries a verdict
    label) and stamps the moment it saw it.  A card that left the queue before its
    verdict (the harvest stamps `lane-pr` on it) is still polled — that is exactly
    the case the cycle has to cover.

The tests drive the real `tick()` of the dispatcher against a fake fleet (no
network, no ssh, no GitHub) with a clock that only moves when the test moves it:
queue → dispatch → harvest → verdict.

One caveat of the batch base: `pause_reason` is defined twice in `autodispatch.py`
(the reason of a legacy marker body shadows the lane-level one), so `status_snapshot()`
raises a TypeError on every tick — the same crash that already breaks
`test_idle_redispatch.py` on this base.  The pause view says nothing about the verify
metrics, so the harness stubs it out when (and only when) the collision is there; the
reproduction then isolates the missing metrics instead of an unrelated crash.

On the baseline every test of this file fails: the dispatcher records no time point
of a verify card and the snapshot carries no verify metric at all.

验收：`python3 -m pytest -q tools/lane-autodispatch/test_verify_metrics.py`
"""
from __future__ import annotations

import importlib.util
import inspect
import json
from card_fixtures import fixture_json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time as time_module
import unittest
from unittest import mock
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("autodispatch.py")
SCRATCH = Path(__file__).resolve().parents[2] / ".swrm-tmp"  # gitignored scratch root

REVIEW_LANE = "CX2"     # a review lane: the only kind of lane a verify card may take
WORK_LANE = "CX1"       # an ordinary lane
REVIEW_HOST = "lane-host-2"
WORK_HOST = "lane-host-1"
RUNNING_ISSUE = 90      # the ordinary card that is already in flight on the review lane
VERIFY_ISSUE = 101      # the verify card of the scenario
WORK_ISSUE = 102        # an ordinary work card
REVIEW_EVIDENCE = {
    "version": 1,
    "ci": {"conclusion": "success", "workflow": "tests", "url": "https://example.com/ci"},
    "tests": {"summary": "passed"},
    "diff": {"files": 1, "insertions": 1, "deletions": 0},
    "acceptance": [{"text": "Review completed", "ok": True}],
}
# The clock of the test starts at a whole second of a fixed date.
START = time_module.mktime(time_module.strptime("2027-01-15 08:00:00", "%Y-%m-%d %H:%M:%S"))
WAIT_S = 300.0          # the verify card waits for the busy review lane
RUN_S = 600.0           # the run of the verify card
VERDICT_LAG_S = 60.0    # the verdict is observed in the tick after the harvest
# The body of a valid verify card (the label plus the heading plus the fields).
VERIFY_BODY = "## Verify Intent\n目标 PR: #1785\n证据要求:\n- [ ] 测试结果\n- [ ] 复现步骤"

# The time points of a verify card and the accepted names of their keys.
TIME_POINTS = {
    "queued": ("queued", "queued_at"),
    "dispatched": ("dispatched", "dispatched_at"),
    "evidence_ready": ("evidence_ready", "evidence_ready_at", "evidence"),
    "verdict": ("verdict", "verdict_at"),
}


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


def point(record, name):
    """The value of one time point of a record (None when the record does not have it)."""
    for key in TIME_POINTS[name]:
        if key in record:
            return record[key]
    return None


def verify_records(state):
    """The verify cards the state tracks: {issue number: record}.

    The record of a card holds its four time points; the state may keep them in a
    mapping keyed by the issue number or in a list of records that name their card.
    """
    records = {}

    def visit(node, key=None):
        if isinstance(node, dict):
            number = node.get("issue") or node.get("n") or node.get("number")
            if number is not None and point(node, "queued") is not None:
                records.setdefault(int(number), node)
            for child_key, value in node.items():
                if isinstance(value, dict) and point(value, "queued") is not None:
                    try:
                        records.setdefault(int(child_key), value)
                    except (TypeError, ValueError):
                        pass
                visit(value, child_key)
        elif isinstance(node, list):
            for item in node:
                visit(item, key)

    visit(state)
    return records


def histogram_pairs(value):
    """The (bucket, count) pairs of a histogram of the snapshot."""
    if isinstance(value, dict):
        return [(str(bucket), float(count)) for bucket, count in value.items()
                if isinstance(count, (int, float)) and not isinstance(count, bool)]
    if isinstance(value, list):
        pairs = []
        for item in value:
            if isinstance(item, dict):
                label = next((item[key] for key in ("bucket", "label", "range", "name") if key in item), None)
                count = next((item[key] for key in ("count", "n", "value") if key in item), None)
                if label is not None and isinstance(count, (int, float)) and not isinstance(count, bool):
                    pairs.append((str(label), float(count)))
            elif isinstance(item, (list, tuple)) and len(item) == 2 \
                    and isinstance(item[1], (int, float)) and not isinstance(item[1], bool):
                pairs.append((str(item[0]), float(item[1])))
        return pairs
    return []


def bucket_bounds(label):
    """The (low, high) seconds of a bucket label ("0-60", "60-300", "300+", "<=300")."""
    numbers = [float(number) for number in re.findall(r"\d+(?:\.\d+)?", label)]
    if not numbers:
        return None
    if len(numbers) >= 2:
        return numbers[0], numbers[1]
    if "+" in label or ">" in label:
        return numbers[0], float("inf")
    return 0.0, numbers[0]


def bucket_count(histogram, seconds):
    """The count of the bucket of the histogram that holds `seconds`.

    A value on a bucket border belongs to two buckets; the largest count wins: the
    bucket that really holds the sample is the one that counts it.
    """
    counts = []
    for label, count in histogram_pairs(histogram):
        bounds = bucket_bounds(label)
        if bounds and bounds[0] <= seconds <= bounds[1]:
            counts.append(count)
    return max(counts) if counts else None


def sample_count(histogram):
    """The number of samples of a histogram (a cumulative histogram counts its last bucket)."""
    pairs = histogram_pairs(histogram)
    if not pairs:
        return 0
    cumulative = all("<" in label or label.lower().startswith("le") for label, _ in pairs)
    if cumulative:
        return max(count for _, count in pairs)
    return sum(count for _, count in pairs)


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
        self.base = ""
        self.commits = {}   # head sha -> issue number of the run
        self.issues = {
            RUNNING_ISSUE: {"title": "#90 the work card in flight", "body": "",
                            "labels": ["lane-ready", "in-progress"], "state": "OPEN"},
            VERIFY_ISSUE: {"title": "#101 the verify card", "body": VERIFY_BODY,
                           "labels": ["lane-ready", "verify-intent"], "state": "OPEN"},
            WORK_ISSUE: {"title": "#102 the ordinary work card", "body": "",
                         "labels": ["lane-ready"], "state": "OPEN"},
        }

    # --- the AD's `gh` -------------------------------------------------------------------
    def gh(self, args, timeout=60):
        if args[:2] == ["issue", "list"]:
            rows = [{"number": number, "title": issue["title"], "body": issue["body"],
                     "labels": list(issue["labels"]), "state": issue["state"],
                     "createdAt": issue.get("createdAt")}
                    for number, issue in sorted(self.issues.items()) if issue["state"] == "OPEN"]
            return 0, fixture_json(rows), ""
        if args[:2] == ["issue", "view"]:
            issue = self.issues.get(int(args[2]))
            if issue is None:
                return 1, "", "gh: no such issue"
            return 0, fixture_json({"number": int(args[2]), "state": issue["state"], "body": issue["body"],
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
        if args[:2] == ["pr", "create"]:
            return 0, "https://github.com/surlymochan/phanes/pull/900", ""
        return 0, "", ""

    # --- the AD's `sh` -------------------------------------------------------------------
    def sh(self, cmd, timeout=120, inp=None):
        command = cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd)
        if "ls-remote" in command:
            return 0, "%s\trefs/heads/main\n" % self.base, ""
        if "--format=%s" in command or "--format=%B" in command:
            number = self.commits.get(command.split()[-1])
            if number is None:
                return 0, "", ""
            if "--format=%s" in command:
                return 0, "#%d the card of the lane" % number, ""
            return 0, "Closes #%d\n\n验收：python3 -m pytest -q\n" % number, ""
        if " push " in command or "push -q" in command:
            return 0, "", ""
        match = re.search(r"#(\d+)", command)
        if match:  # the `lane assign` call: the fleet answers with the id of the run
            return 0, json.dumps({"id": "run-%s" % match.group(1)}), ""
        return 0, "", ""

    # --- the AD's `remote` ---------------------------------------------------------------
    def remote(self, lane, cfg, run=""):
        """The status of the lane, read by the real remote snippet of the dispatcher."""
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
    def set_verdict(self, number):
        """The merge gate judged the card: it is closed and carries a verdict label."""
        issue = self.issues[number]
        issue["state"] = "CLOSED"
        for label in ("verdict", "verdict:pass"):
            if label not in issue["labels"]:
                issue["labels"].append(label)


def git(repo, *args):
    """Run a git command in the local repository of the harvest."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or "git %s failed" % args[0])
    return proc.stdout.strip()


def pause_view_is_broken(module):
    """True when the base shadows the lane-level `pause_reason` with the marker helper.

    The batch base defines `pause_reason` twice: the later definition (the reason of a
    legacy marker body) shadows the lane-level one, so `status_snapshot()` raises a
    TypeError on every tick — the same crash that breaks `test_idle_redispatch.py` on
    the batch base.  The pause view says nothing about the verify metrics, so the
    harness stubs it out when (and only when) the collision is there: the reproduction
    then isolates the missing metrics instead of an unrelated crash.
    """
    try:
        parameters = inspect.signature(module.pause_reason).parameters
    except (TypeError, ValueError):
        return True
    return len(parameters) < 4


def make_repo(root):
    """The local repository of the harvest: one base commit and one branch per run."""
    repo = root / "local-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "lane@example.invalid")
    git(repo, "config", "user.name", "lane")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def add_run_branch(repo, base, issue, lane):
    """The branch of a finished run: one commit on top of the base (the harvest reads it)."""
    branch = "fleet/%s-issue%d-run-%d" % (lane, issue, issue)
    git(repo, "checkout", "-q", "-b", branch, base)
    (repo / ("card-%d.md" % issue)).write_text("# %d\n" % issue)
    git(repo, "add", ("card-%d.md" % issue))
    git(repo, "commit", "-q", "-m", "Closes #%d\n\n验收：python3 -m pytest -q" % issue)
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/fleet/%s" % lane, head)
    return head


class VerifyMetricsTest(unittest.TestCase):
    """The metrics of the verification queue: the cycle, the wait and the reserved share."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="ad-verify-metrics-", dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.clock = Clock()
        self.repo, self.base = make_repo(self.root)
        self.fleet = Fleet(self.root, self.clock)
        # the reservation is switched on explicitly (the policy of #1794): the switch is
        # read when the dispatcher is imported, so it has to be set before the import
        old_share = os.environ.get("AD_VERIFY_SHARE_ENABLED")
        os.environ["AD_VERIFY_SHARE_ENABLED"] = "1"
        self.addCleanup(self._restore_env, "AD_VERIFY_SHARE_ENABLED", old_share)
        old_home = os.environ.get("OPENSWARM_HOME")
        os.environ["OPENSWARM_HOME"] = str(self.root)
        self.addCleanup(self._restore_env, "OPENSWARM_HOME", old_home)
        self.module = load_module("autodispatch_verify_metrics_%d" % id(self))
        self.fleet.module = self.module
        self.fleet.base = self.base
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.brain_path = self.root / "brain-log.md"
        self.brain_path.write_text("")
        self.patch(self.module)
        # one review lane (the only kind of lane a verify card may take) and one work lane
        self.lanes = {
            REVIEW_LANE: {"host": REVIEW_HOST, "backend": "codex", "repo": "/tmp/fleet",
                          "fleet": "/tmp/swrm-fleet"},
            WORK_LANE: {"host": WORK_HOST, "backend": "pi", "repo": "/tmp/fleet",
                        "fleet": "/tmp/swrm-fleet"},
        }
        # the review lane is busy with an ordinary card: the verify card has to wait
        self.state = {REVIEW_LANE: {"issue": RUNNING_ISSUE, "run": "run-%d" % RUNNING_ISSUE,
                                    "host": REVIEW_HOST, "base": self.base,
                                    "at": self.clock.stamp(), "verify": None, "class": "work"}}
        self.times = {}
        self.write_heartbeat()
        self.set_lane(REVIEW_LANE, "busy", "run-%d" % RUNNING_ISSUE)
        self.set_lane(WORK_LANE, "idle")

    # --- the sandbox ---------------------------------------------------------------------
    def patch(self, module):
        """Point a copy of the dispatcher at the sandbox (no real state, log or fleet)."""
        module.D = str(self.state_dir)
        module.BRAIN_LOG = str(self.brain_path)
        module.LOCAL = str(self.repo)
        module.gh = self.fleet.gh
        module.sh = self.fleet.sh
        module.remote = self.fleet.remote
        module.replenish = lambda k, state: []
        # the host memory preflight is not under test here: every host has enough memory
        module.available_memory_mb = lambda host: 4096
        module.BACKOFF.clear()
        module.time = self.clock
        if pause_view_is_broken(module):
            # the base crashes the snapshot on the shadowed `pause_reason`; no lane of the
            # scenario is paused, so an empty pause view is what the fixed code returns
            module.pause_reasons = lambda lanes, records=None, now=None: {}

    def _restore_env(self, name, old):
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old

    # --- the fake executor --------------------------------------------------------------
    def set_lane(self, lane, lane_state, run=None):
        """The lane state of the executor (what the host reports about the lane)."""
        lane_dir = self.fleet.home / "lanes" / lane
        lane_dir.mkdir(parents=True, exist_ok=True)
        row = {"state": lane_state}
        if run:
            row["current_run"] = run
        (lane_dir / "state.json").write_text(json.dumps(row))

    def finish_run(self, issue, lane, with_commit=False, ended_at=None):
        """The executor files of a run that ended (its result and its status)."""
        ended = self.clock.now if ended_at is None else float(ended_at)
        run = "run-%d" % issue
        run_dir = self.fleet.home / "runs" / run
        run_dir.mkdir(parents=True, exist_ok=True)
        result = {"id": run, "status": "done", "branch": None, "commit": None,
                  "workspace": {"repo": "/tmp/fleet"}, "backend": "codex", "model": "codex",
                  "error_class": None, "result": {"summary": "", "tests": ""},
                  "verify": {"rc": 0}, "finished_at": ended, "ended_at": ended}
        if with_commit:
            head = add_run_branch(self.repo, self.base, issue, lane)
            # the harvest reads the subject and the message of the head it pushed
            self.fleet.commits[head] = issue
            self.fleet.commits["refs/fleet/%s" % lane] = issue
            result["branch"] = "fleet/%s-issue%d-run-%d" % (lane, issue, issue)
            result["commit"] = head
            if issue == VERIFY_ISSUE:
                result["evidence"] = REVIEW_EVIDENCE
        (run_dir / "result.json").write_text(json.dumps(result))
        (run_dir / "status.json").write_text(json.dumps({
            "id": run, "state": "finished", "status": "done", "finished": ended,
            "ended_at": ended, "updated": ended}))
        self.set_lane(lane, "busy", run)
        return ended

    def write_heartbeat(self, at=None):
        """Write the heartbeat of the dispatcher: the stamp of its last life."""
        at = self.clock.now if at is None else float(at)
        path = self.state_dir / "heartbeat"
        path.write_text(self.clock.stamp(at) + "\n")
        os.utime(path, (at, at))

    def advance(self, seconds):
        """Move the clock; the dispatcher stays alive and refreshes its heartbeat."""
        self.clock.advance(seconds)
        self.write_heartbeat()

    # --- the dispatcher ------------------------------------------------------------------
    def tick(self, state=None, module=None):
        """One tick of the real dispatcher against the fake fleet."""
        state = self.state if state is None else state
        (module or self.module).tick(self.lanes, state, 0)
        return state

    def snapshot(self):
        """The health snapshot of the lanes (lanes-status.json)."""
        path = self.state_dir / "lanes-status.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def metric(self, status, *groups):
        """The value of the snapshot entry whose key mentions one word of every group."""
        for key in sorted(status):
            name = str(key).lower()
            if all(any(word in name for word in group) for group in groups):
                return status[key]
        return None

    def deficit(self, status=None):
        """The verify deficit of the snapshot (the reserved verify lanes that are free)."""
        return self.metric(self.snapshot() if status is None else status, ("verify",), ("deficit",))

    def share(self, status=None):
        """The share the verify class really got."""
        return self.metric(self.snapshot() if status is None else status, ("verify",), ("share",))

    def cap_hits(self, status=None):
        """The number of times the work class hit its cap."""
        return self.metric(self.snapshot() if status is None else status,
                           ("cap",), ("hit", "block", "count", "work"))

    def wait_histogram(self, status=None):
        """The histogram of the wait (queued → dispatched)."""
        return self.metric(self.snapshot() if status is None else status, ("verify",), ("wait",))

    def cycle_histogram(self, status=None):
        """The histogram of the cycle (queued → verdict)."""
        return self.metric(self.snapshot() if status is None else status, ("verify",), ("cycle",))

    # --- the scenario -------------------------------------------------------------------
    def step_queue(self):
        """The verify card enters the queue while the review lane is busy."""
        self.times["queued"] = self.clock.now
        self.tick()

    def step_dispatch(self):
        """The review lane is free again: the verify card takes the reserved lane."""
        self.advance(WAIT_S)
        self.finish_run(RUNNING_ISSUE, REVIEW_LANE)
        self.times["dispatched"] = self.clock.now
        self.tick()

    def step_harvest(self):
        """The run of the verify card ends: the evidence package is harvested."""
        self.advance(RUN_S)
        self.finish_run(VERIFY_ISSUE, REVIEW_LANE, with_commit=True)
        self.times["evidence"] = self.clock.now
        self.tick()

    def step_verdict(self):
        """The merge gate judged the card: the dispatcher sees the verdict."""
        # the ordinary work card ends in the same tick: its own lifecycle must not
        # add a sample to the metrics of the verify cards
        self.finish_run(WORK_ISSUE, WORK_LANE)
        self.fleet.set_verdict(VERIFY_ISSUE)
        self.advance(VERDICT_LAG_S)
        self.times["verdict"] = self.clock.now
        self.tick()

    def scenario(self):
        """queue → dispatch → harvest → verdict."""
        self.step_queue()
        self.step_dispatch()
        self.step_harvest()
        self.step_verdict()

    # --- the tests -----------------------------------------------------------------------
    def test_valid_review_evidence_is_harvested_without_redispatch(self):
        self.step_queue()
        self.step_dispatch()
        self.step_harvest()
        self.assertIn("lane-pr", self.fleet.issues[VERIFY_ISSUE]["labels"])
        self.assertIsNotNone(point(verify_records(self.state)[VERIFY_ISSUE], "evidence_ready"))
        self.tick()
        self.assertEqual(self.state[self.module.VERIFY_METRICS_KEY]["dispatches"]["verify"], 1)

    def test_missing_review_evidence_does_not_mark_evidence_ready(self):
        self.step_queue()
        self.step_dispatch()
        result = self.module.finish(REVIEW_LANE, self.lanes[REVIEW_LANE],
                                    self.state[REVIEW_LANE],
                                    {"branch": "branch", "commit": "commit", "repo": "repo"},
                                    self.state)
        self.assertEqual(result["status"], "pr-validation-failed")
        self.assertIsNone(point(verify_records(self.state)[VERIFY_ISSUE], "evidence_ready"))

    def test_queue_cycle_includes_time_before_the_dispatcher_discovers_the_card(self):
        created = self.clock.now
        self.fleet.issues[VERIFY_ISSUE]["createdAt"] = time_module.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time_module.gmtime(created))
        self.advance(3600)  # dispatcher was stopped or paused after the order
        self.scenario()
        record = verify_records(self.state)[VERIFY_ISSUE]
        self.assertEqual(to_epoch(point(record, "queued")), created)
        self.assertEqual(to_epoch(point(record, "dispatched")) - created, 3600 + WAIT_S)
        self.assertEqual(bucket_count(self.wait_histogram(), 3600 + WAIT_S), 1)
        self.assertEqual(bucket_count(self.cycle_histogram(),
                                      3600 + WAIT_S + RUN_S + VERDICT_LAG_S), 1)
        self.advance(60)
        self.tick()
        self.assertEqual(to_epoch(point(verify_records(self.state)[VERIFY_ISSUE], "queued")), created)

    def test_failed_verify_run_without_a_commit_has_no_ready_evidence(self):
        self.step_queue()
        self.step_dispatch()
        self.advance(RUN_S)
        result = self.module.finish(REVIEW_LANE, self.lanes[REVIEW_LANE],
                                    self.state[REVIEW_LANE],
                                    {"status": "failed", "branch": None, "commit": None,
                                     "error_class": "executor-failed", "evidence": REVIEW_EVIDENCE}, self.state)
        self.assertEqual(result["status"], "lane-failed")
        self.assertIsNone(point(verify_records(self.state)[VERIFY_ISSUE], "evidence_ready"))

    def test_verify_run_with_no_effective_change_has_no_ready_evidence(self):
        self.step_queue()
        self.step_dispatch()
        with mock.patch.object(self.module, "harvest_fetch", return_value=(True, "", 0)), \
             mock.patch.object(self.module, "filter_harvest", return_value=(self.base, [], True)):
            result = self.module.finish(REVIEW_LANE, self.lanes[REVIEW_LANE],
                                        self.state[REVIEW_LANE],
                                        {"branch": "branch", "commit": "commit", "repo": "repo",
                                         "evidence": REVIEW_EVIDENCE},
                                        self.state)
        self.assertEqual(result["status"], "lane-failed")
        self.assertIsNone(point(verify_records(self.state)[VERIFY_ISSUE], "evidence_ready"))

    def test_the_verify_card_records_the_four_time_points_of_its_life(self):
        self.scenario()
        records = verify_records(self.state)
        self.assertIn(VERIFY_ISSUE, records,
                      "the state must track the verify card #%d and its time points "
                      "(queued, dispatched, evidence_ready, verdict); it tracks %r"
                      % (VERIFY_ISSUE, sorted(records)))
        record = records[VERIFY_ISSUE]
        for name in ("queued", "dispatched", "evidence_ready", "verdict"):
            self.assertIsNotNone(point(record, name),
                                 "the record of the verify card #%d has no %s time point: %r"
                                 % (VERIFY_ISSUE, name, record))
        self.assertAlmostEqual(to_epoch(point(record, "queued")), self.times["queued"], delta=2.0,
                               msg="the card was queued at the tick that saw it")
        self.assertAlmostEqual(to_epoch(point(record, "dispatched")), self.times["dispatched"], delta=2.0,
                               msg="the card was dispatched in the tick that handed it to the lane")
        self.assertAlmostEqual(to_epoch(point(record, "evidence_ready")), self.times["evidence"], delta=2.0,
                               msg="the evidence was ready when the run of the card was harvested")
        self.assertAlmostEqual(to_epoch(point(record, "verdict")), self.times["verdict"], delta=2.0,
                               msg="the verdict was stamped when the dispatcher saw it")

    def test_the_wait_and_the_cycle_of_the_verify_card_land_in_the_histogram(self):
        self.scenario()
        wait = self.wait_histogram()
        self.assertIsNotNone(wait,
                              "the snapshot must carry the wait histogram of the verify cards; it carries %r"
                              % sorted(self.snapshot()))
        self.assertEqual(sample_count(wait), 1,
                         "exactly one verify card was dispatched, so the wait histogram has one sample: %r"
                         % histogram_pairs(wait))
        self.assertEqual(bucket_count(wait, WAIT_S), 1,
                         "the wait of the verify card (%gs) must land in the bucket that holds it: %r"
                         % (WAIT_S, histogram_pairs(wait)))
        cycle = self.cycle_histogram()
        self.assertIsNotNone(cycle,
                              "the snapshot must carry the cycle histogram of the verify cards; it carries %r"
                              % sorted(self.snapshot()))
        self.assertEqual(sample_count(cycle), 1,
                         "exactly one verify card got a verdict, so the cycle histogram has one sample: %r"
                         % histogram_pairs(cycle))
        total = WAIT_S + RUN_S + VERDICT_LAG_S
        self.assertEqual(bucket_count(cycle, total), 1,
                         "the cycle of the verify card (%gs) must land in the bucket that holds it: %r"
                         % (total, histogram_pairs(cycle)))

    def test_the_snapshot_shows_the_reserved_share_of_the_verify_class(self):
        # the verify card waits while a reserved verify lane is free
        self.step_queue()
        status = self.snapshot()
        self.assertEqual(self.deficit(status), 1,
                         "one reserved verify lane is free while the verify card waits, so the "
                         "verify deficit of the snapshot is 1; the snapshot carries %r"
                         % sorted(status))
        self.assertGreaterEqual(self.cap_hits(status) or 0, 1,
                                "the work class hit its cap while the verify card waited, so the "
                                "snapshot must count that hit; the snapshot carries %r" % sorted(status))
        # the verify card takes the reserved lane: the share it really got is visible
        self.step_dispatch()
        status = self.snapshot()
        share = self.share(status)
        self.assertIsNotNone(share,
                             "the snapshot must carry the share the verify class really got; it carries %r"
                             % sorted(status))
        self.assertAlmostEqual(float(share), 0.5, delta=0.01,
                               msg="one of the two dispatches of the window was a verify card, so the "
                                   "actual share of the verify class is 0.5")

    def test_verify_dispatch_refreshes_deficit_when_no_other_lane_is_idle(self):
        # Both lanes start busy with work. Only the review lane finishes, so no
        # later admission attempt can hide a stale pre-dispatch deficit.
        self.state[WORK_LANE] = {"issue": WORK_ISSUE, "run": "run-%d" % WORK_ISSUE,
                                 "host": WORK_HOST, "base": self.base,
                                 "at": self.clock.stamp(), "verify": None, "class": "work"}
        self.fleet.issues[WORK_ISSUE]["labels"].append("in-progress")
        self.set_lane(WORK_LANE, "busy", "run-%d" % WORK_ISSUE)
        self.step_queue()
        self.step_dispatch()
        self.assertEqual(self.state[REVIEW_LANE]["issue"], VERIFY_ISSUE)
        self.assertEqual(self.module.queue_class_limits(self.lanes, self.state, [])[0], 0)
        self.assertEqual(self.deficit(), 0,
                         "the successful verify dispatch filled the reserved slot")
        self.assertEqual(json.loads((self.state_dir / "state.json").read_text())
                         [self.module.VERIFY_METRICS_KEY]["deficit"], 0)
        self.advance(60)
        self.tick()
        self.assertEqual(self.deficit(), 0,
                         "the deficit must stay correct when both lanes remain busy")
        module = load_module("autodispatch_verify_deficit_restart_%d" % id(self))
        self.fleet.module = module
        self.patch(module)
        state = json.loads((self.state_dir / "state.json").read_text())
        self.tick(state, module=module)
        self.assertEqual(self.deficit(), 0,
                         "a restart must restore the post-dispatch deficit")

    def test_an_ordinary_work_card_does_not_pollute_the_verify_metrics(self):
        self.scenario()
        records = verify_records(self.state)
        self.assertNotIn(WORK_ISSUE, records,
                         "an ordinary work card is not a verify card and must not be tracked as one")
        self.assertNotIn(RUNNING_ISSUE, records,
                         "an ordinary work card is not a verify card and must not be tracked as one")
        wait = self.wait_histogram()
        cycle = self.cycle_histogram()
        self.assertIsNotNone(wait, "the snapshot must carry the wait histogram of the verify cards")
        self.assertIsNotNone(cycle, "the snapshot must carry the cycle histogram of the verify cards")
        self.assertEqual(sample_count(wait), 1,
                         "the two ordinary work cards of the scenario must not add a sample to the "
                         "wait histogram of the verify cards: %r" % histogram_pairs(wait))
        self.assertEqual(sample_count(cycle), 1,
                         "the two ordinary work cards of the scenario must not add a sample to the "
                         "cycle histogram of the verify cards: %r" % histogram_pairs(cycle))

    def test_the_metrics_survive_a_restart_of_the_dispatcher(self):
        self.scenario()
        before = self.snapshot()
        self.assertEqual(sample_count(self.wait_histogram(before)), 1,
                         "the wait histogram must hold the sample of the verify card before the restart")
        # the dispatcher restarts: a fresh copy of the module, the state from the disk
        module = load_module("autodispatch_verify_metrics_restart_%d" % id(self))
        self.fleet.module = module
        self.patch(module)
        state = json.loads((self.state_dir / "state.json").read_text())
        self.tick(state, module=module)
        after = self.snapshot()
        self.assertEqual(sample_count(self.wait_histogram(after)), 1,
                         "the wait histogram must be restored from the state after a restart, not "
                         "counted again from zero: %r" % histogram_pairs(self.wait_histogram(after)))
        self.assertEqual(sample_count(self.cycle_histogram(after)), 1,
                         "the cycle histogram must be restored from the state after a restart, not "
                         "counted again from zero: %r" % histogram_pairs(self.cycle_histogram(after)))
        self.assertAlmostEqual(float(self.share(after)), 0.5, delta=0.01,
                               msg="the share the verify class got must survive the restart")
        records = verify_records(state)
        self.assertIn(VERIFY_ISSUE, records,
                      "the restarted dispatcher must find the tracked verify card in the state again")


if __name__ == "__main__":
    unittest.main()
