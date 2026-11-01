#!/usr/bin/env python3
"""Offline contract check for the autodispatch harvest-fetch backoff (Refs #947).

`lane-autodispatch` harvests a finished lane branch with `git fetch`.  A broken
transfer (`fetch-pack: unexpected disconnect`, `bad pack header`, ...) is a
*transient* failure: the first N consecutive failures only log and back off
(exponential 30s -> 600s), and only the failure *past* that threshold is
allowed to label the issue `lane-failed`.  A `fatal` error (a retry cannot
help) is labelled `lane-failed` right away.

The check replays a fake failure sequence against the real autodispatch module
inside a scratch directory.  It never reads or writes the live lane state
(`~/phanes-ctl/autodispatch/state.json`).
"""
from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"

# Field signatures seen in M4#904: a truncated pack over a flaky ssh mux.
TRANSIENT_ERRORS = (
    "fetch-pack: unexpected disconnect while reading sideband packet",
    "fatal: bad pack header at offset 0",
    "error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly",
    "ssh: connect to host instabox port 22: Connection reset by peer",
)
FATAL_ERROR = "fatal: could not read from remote repository"

LANE = "M4"
ISSUE = 947
CFG = {"host": "instabox", "fleet": "fleet", "repo": "repo"}
INFO = {"branch": "fleet/M4-issue947-run947", "commit": "deadbeef", "status": "ok", "summary": "fake summary",
        "repo": "repo"}


def load_module():
    """Load the lane autodispatch module (the box runtime copy) as-is."""
    spec = importlib.util.spec_from_file_location("autodispatch_harvest_backoff", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TimeShim:
    """`time` proxy that records the backoff sleeps instead of waiting for them."""

    def __init__(self, real):
        self._real = real
        self.sleeps = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def sleep(self, seconds):
        self.sleeps.append(seconds)


class FakeHarvest:
    """Replay harvest fetch outcomes through the real `finish()` implementation."""

    def __init__(self, module, scratch):
        self.module = module
        self.scratch = pathlib.Path(scratch)
        self.logs = []
        self.gh_calls = []
        self.fetch_error = ""
        self.time = TimeShim(time)
        self.live_dir = module.D  # the real lane state dir, never written by this check
        module.D = str(self.scratch)
        module.time = self.time
        module.log = self._log
        module.sh = self._sh
        module.gh = self._gh

    def _log(self, msg, brain=True):
        self.logs.append(msg)

    def _sh(self, cmd, timeout=120, inp=None):
        if isinstance(cmd, list) and "fetch" in cmd:
            if self.fetch_error:
                return 1, "", self.fetch_error
            return 0, "", ""
        return 0, "", ""

    def _gh(self, args, timeout=60):
        self.gh_calls.append(list(args))
        return 0, "", ""

    def lane_failed_edits(self):
        return [c for c in self.gh_calls if "edit" in c and "lane-failed" in c]

    def comments(self):
        return [c for c in self.gh_calls if "comment" in c]


def new_state():
    return {LANE: {"issue": ISSUE, "run": "run-947", "host": CFG["host"], "base": "abcdef1234"}}


def check_classification(module):
    """Transient transfer errors are classified transient; everything else is fatal."""
    for error in TRANSIENT_ERRORS:
        assert module.classify_harvest_fetch_error(error) == "transient", error
        assert module.should_retry_harvest_fetch(error, 1) is True, error
    assert module.classify_harvest_fetch_error(FATAL_ERROR) == "fatal", FATAL_ERROR
    assert module.should_retry_harvest_fetch(FATAL_ERROR, 1) is False, FATAL_ERROR


def check_backoff_ladder(module):
    """The backoff is exponential and capped at 600 seconds."""
    ladder = [module.harvest_fetch_backoff_seconds(n) for n in range(1, 6)]
    assert ladder == [30, 60, 120, 240, 480], ladder
    assert module.harvest_fetch_backoff_seconds(0) == 0
    assert module.harvest_fetch_backoff_seconds(9) == 600


def check_outcome_threshold(module):
    """A transient failure stays transient up to the threshold; only the next one is lane-failed."""
    threshold = module.HARVEST_FETCH_MAX_TRANSIENT_FAILURES
    assert threshold >= 2, threshold
    assert module.harvest_fetch_outcome(threshold - 1, "transient") == "transient"
    assert module.harvest_fetch_outcome(threshold, "transient") == "transient"
    assert module.harvest_fetch_outcome(threshold + 1, "transient") == "lane-failed"
    assert module.harvest_fetch_outcome(1, "fatal") == "lane-failed"


def check_transient_sequence(module, scratch_root):
    """Replay the fake failure sequence: log + backoff for N failures, lane-failed on the next one."""
    threshold = module.HARVEST_FETCH_MAX_TRANSIENT_FAILURES
    with tempfile.TemporaryDirectory(dir=scratch_root) as raw:
        harvest = FakeHarvest(module, raw)
        state = new_state()
        lane_state = state[LANE]
        harvest.fetch_error = TRANSIENT_ERRORS[0]

        # the first N failures only log and back off, the issue stays untouched
        for cycle in range(1, threshold + 1):
            assert module.finish(LANE, CFG, lane_state, INFO, state) is False, cycle
            assert lane_state["fetch_failures"] == cycle, lane_state
            assert module.lane_in_fetch_backoff(lane_state) is True, lane_state
            assert not harvest.lane_failed_edits(), harvest.gh_calls
            assert not harvest.comments(), harvest.gh_calls
            assert any("transient" in msg for msg in harvest.logs), harvest.logs
            assert any("backing off" in msg for msg in harvest.logs), harvest.logs
            assert (pathlib.Path(raw) / "state.json").is_file(), "the lane state must stay in the scratch dir"

        # the failure past the threshold gives up on the issue
        assert module.finish(LANE, CFG, lane_state, INFO, state) is True
        assert lane_state.get("fetch_failures") is None, lane_state
        assert module.lane_in_fetch_backoff(lane_state) is False, lane_state
        assert harvest.lane_failed_edits(), harvest.gh_calls
        assert harvest.comments(), harvest.gh_calls
        assert "lane-failed" in harvest.comments()[0][-1], harvest.comments()
        assert module.D == str(pathlib.Path(raw)), "the live lane state dir must not be used"
        assert harvest.live_dir != module.D, "the live lane state dir must not be used"


def check_fatal_error(module, scratch_root):
    """A fatal fetch error is labelled lane-failed without waiting for the threshold."""
    with tempfile.TemporaryDirectory(dir=scratch_root) as raw:
        harvest = FakeHarvest(module, raw)
        state = new_state()
        lane_state = state[LANE]
        harvest.fetch_error = FATAL_ERROR

        assert module.finish(LANE, CFG, lane_state, INFO, state) is True
        assert harvest.lane_failed_edits(), harvest.gh_calls
        assert harvest.comments(), harvest.gh_calls
        assert lane_state.get("fetch_failures") is None, lane_state


def check_success_resets(module, scratch_root):
    """A successful harvest fetch clears the failure counter and never labels lane-failed."""
    with tempfile.TemporaryDirectory(dir=scratch_root) as raw:
        harvest = FakeHarvest(module, raw)
        state = new_state()
        lane_state = state[LANE]
        lane_state["fetch_failures"] = 2
        lane_state["fetch_backoff_until"] = time.time() + 600
        harvest.fetch_error = ""

        assert module.finish(LANE, CFG, lane_state, INFO, state) is True
        assert lane_state.get("fetch_failures") is None, lane_state
        assert module.lane_in_fetch_backoff(lane_state) is False, lane_state
        assert not harvest.lane_failed_edits(), harvest.gh_calls


def main():
    module = load_module()
    check_classification(module)
    check_backoff_ladder(module)
    check_outcome_threshold(module)

    scratch_root = ROOT / ".openswarm-tmp"
    scratch_root.mkdir(exist_ok=True)
    check_transient_sequence(module, scratch_root)
    check_fatal_error(module, scratch_root)
    check_success_resets(module, scratch_root)

    print("ad harvest fetch backoff: OK")


if __name__ == "__main__":
    main()
