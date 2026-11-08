"""Contract of the autodispatch worker (the phanes AD, tools/lane-autodispatch).

The `in-progress` leak sweep of the AD is lease based (Refs #1364): the dispatch takes a lease
*before* it labels the issue `in-progress`, the lane heartbeat renews the lease, and an expired
lease is only reclaimed when the run it points at is gone. A label that was written before the
upgrade holds no lease and is reclaimed with the original grace rule (the label must be older
than `AD_LEAK_GRACE_S` and no lane may hold the issue).

The upstream side of the change lives in `openswarm/lib/dispatch/` (the lease store and the
sweep decisions, the wiring the retired `lib/dispatch/engine.py` used to hold). The AD of the
phanes control plane is deployed as a single file, so it mirrors those rules; the parity test
below pins the mirror to the upstream decisions.

The file lives in ``openswarm/tests`` because the documented acceptance of the leak sweep is
``cd openswarm && python3 -m unittest discover -s tests -p 'test_autodispatch.py'``; the AD
itself stayed in the phanes tree when the vendored openswarm implementation was replaced by swrm.

The delivery mapping of the last review round (B1/B2/B3) lives in the diff, not only in the
``.lane-review/`` folder (the harvest drops that folder before the PR is pushed):

* **B1** (the upstream lease integration of the previous round vanished with the retired
  ``lib/dispatch/engine.py``): the upstream side is back under ``openswarm/`` --
  ``openswarm/lib/dispatch/lease.py`` (``LeaseStore``) and ``openswarm/lib/dispatch/lease_sweep.py``
  (``decide()`` / ``reclaimable()``), with their own tests
  (``openswarm/tests/test_dispatch_lease.py`` and ``openswarm/tests/test_dispatch_lease_sweep.py``).
  The running path is the phanes mirror ``tools/lane-autodispatch/autodispatch.py``
  (``grant_lease()`` / ``renew_lease()`` / ``release_lease()`` / ``sweep_leaked_inprogress()``);
  ``SweepParityTests`` below pins the mirror to the upstream decisions.
* **B2** (the delivery note missed the per-item mapping): ``tools/lane-autodispatch/README.md``
  carries the table B-number -> file:line -> test name -> result.
* **B3** (the renewal, the reclaim and the "still running" case called the sweep directly):
  ``TickLeaseTests`` below drives ``autodispatch.tick()`` for all of them, so the lease is taken
  before the label, the heartbeat renews it, the expired lease of a gone run is reclaimed and a
  live run keeps its label.
"""

import importlib.util
import json
import pathlib
import shutil
import tempfile
import time
import unittest
import warnings
from unittest import mock


AUTODISPATCH = pathlib.Path(__file__).parents[2] / "tools" / "lane-autodispatch" / "autodispatch.py"
spec = importlib.util.spec_from_file_location("autodispatch", AUTODISPATCH)
autodispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autodispatch)

# the upstream library of the same change (the sweep decisions)
SWEEP = pathlib.Path(__file__).parents[1] / "lib" / "dispatch" / "lease_sweep.py"
sweep_spec = importlib.util.spec_from_file_location("dispatch_lease_sweep", SWEEP)
lease_sweep = importlib.util.module_from_spec(sweep_spec)
sweep_spec.loader.exec_module(lease_sweep)


def gh_time(epoch):
    """A GitHub `updatedAt` timestamp of the given epoch seconds."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class LeaseHarness(unittest.TestCase):
    """The side effects of the AD (GitHub, the log, the state file) are faked."""

    LANES = {"CX2": {"host": "h2", "backend": "codex"}}

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.edits = []      # the `gh issue edit` calls
        self.queue_rows = [] # the `lane-ready` issues of the queue
        self.labelled = []   # the open issues that carry the `in-progress` label
        self.sweeps = 0      # the leak sweeps that listed the `in-progress` issues
        self.logs = []
        self.lease_at_label = {}  # issue -> the lease that existed when the label was added
        self.edit_failures = set()  # the issues whose `gh issue edit` fails
        patch = mock.patch.multiple(autodispatch, D=self.directory, log=self.fake_log, gh=self.fake_gh)
        patch.start()
        self.addCleanup(patch.stop)

    # ------------------------------------------------------------------ fakes

    def fake_log(self, msg, brain=True):
        self.logs.append(msg)

    def fake_gh(self, args, timeout=60):
        if args[:2] == ["issue", "list"]:
            label = args[args.index("--label") + 1] if "--label" in args else ""
            if label == "lane-ready":
                return 0, json.dumps(self.queue_rows), ""
            self.sweeps += 1
            return 0, json.dumps(self.labelled), ""
        if args[:2] == ["issue", "view"]:
            n = int(args[2])
            for row in self.queue_rows:
                if row["number"] == n:
                    return 0, json.dumps({"number": n, "state": "OPEN", "labels": row["labels"]}), ""
            return 0, json.dumps({"number": n, "state": "CLOSED", "labels": []}), ""
        if args[:2] == ["issue", "edit"]:
            if "--add-label" in args and "in-progress" in args:
                # the lease must exist before the label is written
                self.lease_at_label[int(args[2])] = autodispatch.load_leases().get(str(int(args[2])))
            self.edits.append(args)
            if int(args[2]) in self.edit_failures:
                return 1, "", "gh: edit failed"
            return 0, "", ""
        return 0, "", ""

    # ------------------------------------------------------------------ helpers

    def removed(self):
        """The issue numbers whose `in-progress` label was removed."""
        return [int(a[2]) for a in self.edits if "--remove-label" in a and "in-progress" in a]

    def added(self):
        """The issue numbers that got the `in-progress` label."""
        return [int(a[2]) for a in self.edits if "--add-label" in a and "in-progress" in a]

    def run_tick(self, dispatch_result="run-1", dispatch_error=None, state=None,
                 remote_result=None, now=None):
        """One dispatch tick with the GitHub/ssh/dispatch side effects faked.

        `now` freezes the clock of the tick (the lease deadlines are compared with it),
        `remote_result` is what the lane reports (idle by default)."""
        patched = ("sh", "host_ok", "host_fail", "host_good", "remote", "deps_met", "queue",
                   "dispatch", "replenish", "save_state", "status_snapshot")
        saved = {name: getattr(autodispatch, name) for name in patched}

        def fake_dispatch(lane, cfg, item, base):
            if dispatch_error is not None:
                raise dispatch_error
            return dispatch_result, None

        clock = mock.patch.object(autodispatch.time, "time", lambda: now) if now is not None else None
        try:
            autodispatch.sh = lambda *a, **k: (0, "0" * 40 + "\n", "")
            autodispatch.host_ok = lambda h: True
            autodispatch.host_fail = lambda h, why: None
            autodispatch.host_good = lambda h: None
            autodispatch.remote = lambda lane, cfg, run="", live=None: dict(remote_result or {"lane": "idle"})
            autodispatch.deps_met = lambda item: True
            autodispatch.queue = lambda: [
                {"n": row["number"], "title": row["title"], "body": row.get("body", ""),
                 "prio": 1, "backend": None, "strict": False, "verify": False}
                for row in self.queue_rows]
            autodispatch.dispatch = fake_dispatch
            autodispatch.replenish = lambda k, state: []
            autodispatch.save_state = lambda state: None
            autodispatch.status_snapshot = lambda *a, **k: {}
            if clock is not None: clock.start()
            # tick() writes lanes-status.json without closing it: the warning is pre-existing
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                autodispatch.tick(dict(self.LANES), state if state is not None else {}, 0)
        finally:
            if clock is not None: clock.stop()
            for name, value in saved.items():
                setattr(autodispatch, name, value)


class InProgressLeaseTests(LeaseHarness):
    """The in-progress leak sweep of the AD is lease based (Refs #1364)."""

    def test_the_lease_is_taken_before_the_label_and_the_dispatch(self):
        """A crash between the label and the dispatch must not leave a label without a lease."""
        self.queue_rows = [{"number": 1, "title": "card", "body": "", "labels": ["lane-ready"]}]

        self.run_tick()

        self.assertEqual(self.added(), [1])
        # the lease existed already when the label was written
        self.assertIsNotNone(self.lease_at_label.get(1), self.lease_at_label)
        # and it points at the run the lane executes now
        lease = autodispatch.load_leases().get("1")
        self.assertEqual(lease["run"], "run-1")
        self.assertEqual(lease["lane"], "CX2")
        self.assertGreater(lease["expires_at"], lease["granted_at"])

    def test_a_failed_dispatch_leaves_neither_a_label_nor_a_lease(self):
        self.queue_rows = [{"number": 1, "title": "card", "body": "", "labels": ["lane-ready"]}]

        self.run_tick(dispatch_error=RuntimeError("ssh down"))

        self.assertEqual(self.removed(), [1])
        self.assertEqual(autodispatch.load_leases(), {})

    def test_the_lease_is_renewed_while_the_lane_still_runs(self):
        autodispatch.grant_lease(5, "run-5", "M5", ttl=600, now=1_000)
        self.labelled = [{"number": 5, "updatedAt": gh_time(1_000)}]

        autodispatch.sweep_leaked_inprogress(
            {"M5": {"issue": 5, "run": "run-5"}},
            {"M5": {"lane": "busy", "current_run": "run-5"}},
            now=2_000)

        self.assertEqual(autodispatch.load_leases()["5"]["expires_at"], 2_600)
        self.assertEqual(self.removed(), [])

    def test_an_expired_lease_is_reclaimed_when_the_run_is_gone(self):
        autodispatch.grant_lease(5, "run-5", "M5", ttl=600, now=1_000)
        self.labelled = [{"number": 5, "updatedAt": gh_time(1_000)}]

        autodispatch.sweep_leaked_inprogress(
            {"M5": {"issue": 5, "run": "run-5"}},
            {"M5": {"lane": "idle", "current_run": None}},
            now=2_000)

        self.assertEqual(self.removed(), [5])
        self.assertIsNone(autodispatch.load_leases().get("5"))

    def test_an_expired_lease_is_kept_while_the_run_still_runs(self):
        """The run is alive: the expired lease is renewed, the label stays."""
        autodispatch.grant_lease(5, "run-5", "M5", ttl=600, now=1_000)
        self.labelled = [{"number": 5, "updatedAt": gh_time(1_000)}]

        autodispatch.sweep_leaked_inprogress(
            {"M5": {"issue": 5, "run": "run-5"}},
            {"M5": {"lane": "busy", "current_run": "run-5"}},
            now=2_000)

        self.assertEqual(self.removed(), [])
        self.assertEqual(autodispatch.load_leases()["5"]["expires_at"], 2_600)

    def test_a_dispatch_interrupted_before_the_run_is_reclaimed(self):
        """The lease of a dispatch that never produced a run expires and is reclaimed."""
        autodispatch.grant_lease(5, "", "M5", ttl=600, now=1_000)
        self.labelled = [{"number": 5, "updatedAt": gh_time(1_000)}]

        autodispatch.sweep_leaked_inprogress({}, {"M5": {"lane": "idle", "current_run": None}}, now=2_000)

        self.assertEqual(self.removed(), [5])
        self.assertIsNone(autodispatch.load_leases().get("5"))

    def test_a_label_without_a_lease_is_reclaimed_after_the_grace(self):
        """The labels written before the upgrade hold no lease: the grace rule applies."""
        now = time.time()
        self.labelled = [{"number": 7, "updatedAt": gh_time(now - 3_600)}]

        autodispatch.sweep_leaked_inprogress({}, {}, now=now)

        self.assertEqual(self.removed(), [7])

    def test_a_fresh_label_without_a_lease_is_kept(self):
        now = time.time()
        self.labelled = [{"number": 7, "updatedAt": gh_time(now - 60)}]

        autodispatch.sweep_leaked_inprogress({}, {}, now=now)

        self.assertEqual(self.removed(), [])

    def test_a_label_without_a_lease_is_kept_while_a_lane_holds_the_issue(self):
        now = time.time()
        self.labelled = [{"number": 7, "updatedAt": gh_time(now - 3_600)}]

        autodispatch.sweep_leaked_inprogress(
            {"M5": {"issue": 7, "run": "run-7"}},
            {"M5": {"lane": "busy", "current_run": "run-7"}},
            now=now)

        self.assertEqual(self.removed(), [])

    def test_the_sweep_runs_at_most_every_five_minutes(self):
        state = {}

        self.run_tick(state=state)
        self.run_tick(state=state)

        self.assertEqual(self.sweeps, 1)


class TickLeaseTests(LeaseHarness):
    """The leak sweep on the real tick path of the AD (Refs #1364).

    The tests drive `tick()` (not the sweep directly): the dispatch, the lane heartbeat and the
    reclaim therefore run the way they run on the box.
    """

    T0 = 1_700_000_000.0

    def dispatch_tick(self, state):
        """One tick that dispatches issue 1 on the lane."""
        self.queue_rows = [{"number": 1, "title": "card", "body": "", "labels": ["lane-ready"]}]
        self.run_tick(state=state, now=self.T0)
        # the issue carries the `in-progress` label now
        self.labelled = [{"number": 1, "updatedAt": gh_time(self.T0)}]
        self.queue_rows = []

    def test_the_tick_takes_the_lease_before_the_label_and_the_dispatch(self):
        state = {}

        self.dispatch_tick(state)

        self.assertEqual(self.added(), [1])
        # the lease existed already when the label was written
        self.assertIsNotNone(self.lease_at_label.get(1), self.lease_at_label)
        lease = autodispatch.load_leases()["1"]
        self.assertEqual(lease["run"], "run-1")
        self.assertEqual(lease["lane"], "CX2")
        self.assertEqual(lease["expires_at"], self.T0 + autodispatch.LEASE_TTL_S)
        self.assertEqual(state["CX2"]["issue"], 1)

    def test_the_tick_renews_the_lease_of_the_lane_that_still_runs(self):
        state = {}
        self.dispatch_tick(state)
        later = self.T0 + autodispatch.LEASE_TTL_S + 100

        # the lane is still busy with the run: the heartbeat renews the expired lease
        self.run_tick(state=state, now=later,
                      remote_result={"lane": "busy", "current_run": "run-1", "run": "running"})

        self.assertEqual(self.sweeps, 2)   # the sweep of the second tick ran
        self.assertEqual(self.removed(), [])
        self.assertEqual(autodispatch.load_leases()["1"]["expires_at"], later + autodispatch.LEASE_TTL_S)

    def test_the_tick_keeps_the_label_while_the_run_still_runs(self):
        state = {}
        self.dispatch_tick(state)

        # the run is alive: the issue keeps its label and is not dispatched a second time
        self.run_tick(state=state, now=self.T0 + autodispatch.LEASE_TTL_S + 100,
                      remote_result={"lane": "busy", "current_run": "run-1", "run": "running"})

        self.assertEqual(self.removed(), [])
        self.assertEqual(self.added(), [1])
        self.assertIsNotNone(autodispatch.load_leases().get("1"))

    def test_the_tick_reclaims_the_label_when_the_lease_expired_and_the_run_is_gone(self):
        state = {}
        self.dispatch_tick(state)

        # the lane is idle again: the run is gone, so the expired lease may be reclaimed
        self.run_tick(state=state, now=self.T0 + autodispatch.LEASE_TTL_S + 100,
                      remote_result={"lane": "idle"})

        self.assertEqual(self.sweeps, 2)   # the sweep of the second tick ran
        self.assertEqual(self.removed(), [1])
        self.assertIsNone(autodispatch.load_leases().get("1"))

    def test_a_failing_label_removal_keeps_the_lease(self):
        """A GitHub write that fails must not release the lease (the next sweep retries)."""
        state = {}
        self.dispatch_tick(state)
        self.edit_failures = {1}

        self.run_tick(state=state, now=self.T0 + autodispatch.LEASE_TTL_S + 100,
                      remote_result={"lane": "idle"})

        # the removal was attempted ...
        self.assertEqual(self.removed(), [1])
        # ... but the lease stays and the success was not logged
        self.assertIsNotNone(autodispatch.load_leases().get("1"))
        self.assertFalse([msg for msg in self.logs if "→ removed" in msg], self.logs)


class SweepParityTests(LeaseHarness):
    """The phanes mirror and the upstream library decide the same way (Refs #1364).

    The AD of the phanes control plane is deployed as a single file, so it cannot import
    `openswarm/lib/dispatch/lease_sweep.py`: the rules are mirrored. This test pins the mirror to
    the upstream decisions, so the two cannot drift apart.
    """

    SCENARIOS = [
        {
            "name": "the lease is alive",
            "labels": [{"number": 5, "updatedAt": gh_time(1_000)}],
            "leases": {5: {"run": "run-5", "expires_at": 2_000.0}},
            "lanes": {"M5": {"issue": 5, "run": "run-5", "busy": True}},
            "now": 1_500.0,
        },
        {
            "name": "the lease expired and the run is gone",
            "labels": [{"number": 5, "updatedAt": gh_time(1_000)}],
            "leases": {5: {"run": "run-5", "expires_at": 2_000.0}},
            "lanes": {"M5": {"issue": 5, "run": "run-5", "busy": False}},
            "now": 2_500.0,
        },
        {
            "name": "the lease expired but the run still lives",
            "labels": [{"number": 5, "updatedAt": gh_time(1_000)}],
            "leases": {5: {"run": "run-5", "expires_at": 2_000.0}},
            "lanes": {"M5": {"issue": 5, "run": "run-5", "busy": True}},
            "now": 2_500.0,
        },
        {
            "name": "the dispatch is still in flight (a pending lease)",
            "labels": [{"number": 5, "updatedAt": gh_time(1_000)}],
            "leases": {5: {"run": "", "expires_at": 2_000.0}},
            "lanes": {"M5": {"issue": 5, "run": "", "busy": False}},
            "now": 2_500.0,
        },
        {
            "name": "a label without a lease is older than the grace",
            "labels": [{"number": 7, "updatedAt": gh_time(1_000)}],
            "leases": {},
            "lanes": {},
            "now": 1_901.0,
        },
        {
            "name": "a fresh label without a lease",
            "labels": [{"number": 7, "updatedAt": gh_time(1_000)}],
            "leases": {},
            "lanes": {},
            "now": 1_060.0,
        },
        {
            "name": "a label without a lease is held by a lane",
            "labels": [{"number": 7, "updatedAt": gh_time(1_000)}],
            "leases": {},
            "lanes": {"M5": {"issue": 7, "run": "run-7", "busy": True}},
            "now": 1_901.0,
        },
    ]

    def upstream_decision(self, scenario):
        """The issues the upstream library reclaims."""
        leases = {str(issue): {"issue": issue, "run_id": record["run"], "expires_at": record["expires_at"]}
                  for issue, record in scenario["leases"].items()}
        held = [record["issue"] for record in scenario["lanes"].values()]
        running = [record["run"] for record in scenario["lanes"].values() if record["busy"]]
        return sorted(lease_sweep.reclaimable(scenario["labels"], leases, held, running,
                                               scenario["now"], autodispatch.LEAK_GRACE_S))

    def mirror_decision(self, scenario):
        """The issues the AD of the phanes control plane reclaims."""
        autodispatch.save_leases({
            str(issue): {"issue": issue, "run": record["run"], "lane": "M5", "granted_at": 0.0,
                         "expires_at": record["expires_at"], "ttl_s": autodispatch.LEASE_TTL_S}
            for issue, record in scenario["leases"].items()})
        state = {lane: {"issue": record["issue"], "run": record["run"]}
                 for lane, record in scenario["lanes"].items()}
        res = {lane: {"lane": "busy" if record["busy"] else "idle",
                      "current_run": record["run"] if record["busy"] else None}
               for lane, record in scenario["lanes"].items()}
        self.labelled = scenario["labels"]
        return sorted(autodispatch.sweep_leaked_inprogress(state, res, now=scenario["now"]))

    def test_the_mirror_and_the_upstream_sweep_decide_the_same(self):
        for scenario in self.SCENARIOS:
            with self.subTest(scenario=scenario["name"]):
                self.assertEqual(self.mirror_decision(scenario), self.upstream_decision(scenario))


if __name__ == "__main__":
    unittest.main()
