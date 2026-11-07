"""Regression coverage for the host recovery of the autodispatcher (Refs #1673).

The 2026-11-02 incident is the sample: the box was unreachable twice, the control
plane wrote ``PAUSE-HOST-box`` while it was gone, and the hold was only lifted by
hand.  The run that was in flight (M6, issue #1361) was lost for 44 minutes
before anybody noticed.

The autodispatcher probes every lane over ssh anyway, so it observes the
reachability of every host and acts on the down -> up edge: the hold of the host
is reviewed (and only released when it was caused by the unreachable host) and the
runs that were in flight are verified -- a run that survived keeps its lane, a run
that was lost goes back to the queue with the labels of the orphan flow.
"""
import importlib.util
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock


AUTODISPATCH = pathlib.Path(__file__).parents[2] / "tools" / "lane-autodispatch" / "autodispatch.py"
spec = importlib.util.spec_from_file_location("autodispatch_recovery", AUTODISPATCH)
autodispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autodispatch)

# 2026-11-02 00:00 UTC, the day of the incident.
MIDNIGHT = 1793577600.0


def at(hour, minute=0, second=0):
    """Return the timestamp of a wall clock time of the sample day."""
    return MIDNIGHT + hour * 3600 + minute * 60 + second


def stamp(moment):
    """Return the control plane dispatch stamp of a moment."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment))


def dead_run(run="run-1", lane="idle"):
    """The probe of a run whose runner is gone: the host dropped while it ran."""
    return {"lane": lane, "current_run": None, "run": "running", "runner_alive": False}


def alive_run(run="run-1"):
    """The probe of a run that survived the outage: the runner is still there."""
    return {"lane": "busy", "current_run": run, "run": "running", "runner_alive": True}


class RecoveryFixture(unittest.TestCase):
    """A fake autodispatch state dir: no ssh, no GitHub, no real clock."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ad-host-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name)
        self.logs = []
        self.gh_calls = []
        self.edit_error = False
        self.clock = at(8, 0)
        patcher = mock.patch.multiple(
            autodispatch,
            D=str(self.dir),
            log=lambda msg, brain=True: self.logs.append(msg),
            gh=self.fake_gh,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        clock = mock.patch.object(autodispatch.time, "time", lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        autodispatch.BACKOFF.clear()
        self.addCleanup(autodispatch.BACKOFF.clear)
        self.lanes = {"M6": {"host": "box", "backend": "pi"}}
        self.state = {"M6": {"issue": 1361, "run": "run-1", "host": "box", "base": "abc123",
                             "at": stamp(at(8, 0))}}

    def fake_gh(self, args, timeout=60):
        """Record the GitHub calls and answer the issue reads.

        `edit_error` makes the label edit fail, the way a broken GitHub call does."""
        self.gh_calls.append(list(args))
        if args[:2] == ["issue", "view"]:
            return 0, json.dumps({"state": "OPEN", "labels": [{"name": "in-progress"}]}), ""
        if args[:2] == ["issue", "edit"] and self.edit_error:
            return 1, "", "gh: could not add label: HTTP 502"
        return 0, "", ""

    def write_hold(self, body, host="box"):
        """Write a PAUSE-HOST marker in the fake state dir."""
        path = self.dir / ("PAUSE-HOST-" + host)
        path.write_text(body, encoding="utf-8")
        return path

    def hold_exists(self, host="box"):
        """Whether the fake state dir still holds the host."""
        return (self.dir / ("PAUSE-HOST-" + host)).exists()

    def down(self, state=None, now=None):
        """One tick in which the host does not answer."""
        state = self.state if state is None else state
        self.clock = now if now is not None else self.clock
        res = {"M6": {"error": "ssh: connect to host box port 22: No route to host"}}
        return autodispatch.recover_hosts(self.lanes, state, res, now=self.clock)

    def up(self, state=None, now=None, probe=None):
        """One tick in which the host answers again."""
        state = self.state if state is None else state
        self.clock = now if now is not None else self.clock
        res = {"M6": probe if probe is not None else dead_run()}
        return autodispatch.recover_hosts(self.lanes, state, res, now=self.clock)


class HostHoldTests(RecoveryFixture):
    """Only a hold that the unreachable host caused is released."""

    def test_the_hold_of_the_outage_is_released(self):
        """The hold was written because the host was gone: the host is back."""
        self.write_hold("auto: ssh streak 3\n")
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["hold"]["released"])
        self.assertEqual(records[0]["hold"]["kind"], "outage")
        self.assertFalse(self.hold_exists())

    def test_a_manual_hold_survives_the_recovery(self):
        """An operator hold outlives the outage."""
        self.write_hold("operator hold\n")
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertFalse(records[0]["hold"]["released"])
        self.assertEqual(records[0]["hold"]["reason"], "manual-hold")
        self.assertTrue(self.hold_exists())

    def test_a_disk_hold_survives_the_recovery(self):
        """ssh says nothing about the disk: a disk hold has its own owner."""
        self.write_hold("auto: disk_hard\n")
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertFalse(records[0]["hold"]["released"])
        self.assertEqual(records[0]["hold"]["reason"], "resource-hold")
        self.assertTrue(self.hold_exists())

    def test_a_memory_hold_survives_the_recovery(self):
        """The memory holds are healed by the health self-heal path."""
        self.write_hold("auto:host_hard mem_avail_mb=100\n")
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertFalse(records[0]["hold"]["released"])
        self.assertEqual(records[0]["hold"]["reason"], "resource-hold")
        self.assertTrue(self.hold_exists())

    def test_a_cooldown_hold_survives_the_recovery(self):
        """A machine cooldown has its own deadline."""
        self.write_hold("auto:cooldown:until=%r\n" % at(10))
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertFalse(records[0]["hold"]["released"])
        self.assertEqual(records[0]["hold"]["reason"], "cooldown-hold")
        self.assertTrue(self.hold_exists())

    def test_an_unknown_hold_written_during_the_outage_is_kept(self):
        """An independent protection written while the host was down is not ours to release.

        The write time of a hold says nothing about its cause: the host answering ssh again only cures the
        holds it caused."""
        path = self.write_hold("auto: unknown reason\n")
        os.utime(path, (at(8, 20), at(8, 20)))
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertFalse(records[0]["hold"]["released"])
        self.assertEqual(records[0]["hold"]["kind"], "unknown")
        self.assertEqual(records[0]["hold"]["reason"], "unknown-hold")
        self.assertTrue(self.hold_exists())

    def test_an_outage_hold_written_before_the_outage_is_released(self):
        """The cause is read from the body, not from the write time."""
        path = self.write_hold("auto: ssh streak 3\n")
        os.utime(path, (at(7, 0), at(7, 0)))
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertTrue(records[0]["hold"]["released"])
        self.assertEqual(records[0]["hold"]["kind"], "outage")
        self.assertFalse(self.hold_exists())

    def test_an_unknown_hold_written_before_the_outage_is_kept(self):
        """A hold that predates the outage was not caused by it."""
        path = self.write_hold("auto: unknown reason\n")
        os.utime(path, (at(7, 0), at(7, 0)))
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertFalse(records[0]["hold"]["released"])
        self.assertEqual(records[0]["hold"]["kind"], "unknown")
        self.assertEqual(records[0]["hold"]["reason"], "unknown-hold")
        self.assertTrue(self.hold_exists())

    def test_a_host_without_a_hold_is_reported_as_such(self):
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertFalse(records[0]["hold"]["present"])
        self.assertEqual(records[0]["hold"]["reason"], "no-hold")


class InFlightRunTests(RecoveryFixture):
    """The runs that were in flight are verified when the host comes back."""

    def test_a_lost_run_goes_back_to_the_queue(self):
        """The issue is requeued: `in-progress` removed, `lane-ready` added."""
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertEqual(records[0]["requeued"], ["M6"])
        edits = [call for call in self.gh_calls if call[:2] == ["issue", "edit"]]
        self.assertEqual(len(edits), 1)
        self.assertIn("--remove-label", edits[0])
        self.assertIn("in-progress", edits[0])
        self.assertIn("--add-label", edits[0])
        self.assertIn("lane-ready", edits[0])
        self.assertNotIn("M6", self.state)
        self.assertNotIn("M6", json.loads((self.dir / "state.json").read_text(encoding="utf-8")))

    def test_a_lost_run_on_a_busy_lane_is_requeued(self):
        """The lane still says busy but its runner is gone: the run is lost."""
        self.down(now=at(8, 30))
        records = self.up(now=at(9), probe=dead_run(lane="busy"))
        self.assertEqual(records[0]["requeued"], ["M6"])
        self.assertNotIn("M6", self.state)

    def test_a_surviving_run_keeps_its_lane(self):
        self.down(now=at(8, 30))
        records = self.up(now=at(9), probe=alive_run())
        self.assertEqual(records[0]["requeued"], [])
        self.assertEqual(records[0]["kept"], ["M6"])
        self.assertIn("M6", self.state)

    def test_an_unreadable_lane_state_never_drops_a_lane(self):
        """The host answers but its lane state cannot be read: nothing is known."""
        self.down(now=at(8, 30))
        records = self.up(now=at(9), probe={"lane": "unknown", "current_run": None, "run": None})
        self.assertEqual(records[0]["requeued"], [])
        self.assertIn("M6", self.state)
        self.assertEqual([call for call in self.gh_calls if call[:2] == ["issue", "edit"]], [])

    def test_a_host_that_does_not_answer_again_is_not_recovered(self):
        """Without the up edge there is nothing to recover."""
        self.down(now=at(8, 30))
        self.assertEqual(self.up(now=at(9), probe={"error": "ssh status timeout"}), [])
        self.assertIn("M6", self.state)

    def test_a_fresh_assignment_is_kept_even_when_the_runner_is_gone(self):
        """A runner that just died may still be writing its result.json."""
        self.state["M6"]["at"] = stamp(at(8, 59))
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertEqual(records[0]["requeued"], [])
        self.assertIn("M6", self.state)


class RequeueFailureTests(RecoveryFixture):
    """A failed label edit keeps the lane: the issue and the lane stay consistent.

    An issue that still carries `in-progress` is skipped by the scheduler. If the lane record were dropped
    anyway, the issue would never be dispatched again."""

    def test_a_failed_label_edit_keeps_the_lane(self):
        """The labels were not updated: the lane record survives for a retry."""
        self.edit_error = True
        self.down(now=at(8, 30))
        records = self.up(now=at(9))
        self.assertEqual(records[0]["requeued"], [])
        self.assertEqual(records[0]["kept"], ["M6"])
        self.assertIn("M6", self.state)
        self.assertIn("M6", json.loads((self.dir / "state.json").read_text(encoding="utf-8")))

    def test_the_next_recovery_retries_the_requeue(self):
        """The retry releases the lane and the issue."""
        self.edit_error = True
        self.down(now=at(8, 30))
        self.up(now=at(9))
        self.assertIn("M6", self.state)
        self.edit_error = False
        self.down(now=at(9, 30))
        records = self.up(now=at(10))
        self.assertEqual(records[0]["requeued"], ["M6"])
        self.assertNotIn("M6", self.state)
        self.assertNotIn("M6", json.loads((self.dir / "state.json").read_text(encoding="utf-8")))
        edits = [call for call in self.gh_calls if call[:2] == ["issue", "edit"]]
        self.assertEqual(len(edits), 2)

    def test_a_failed_issue_read_keeps_the_lane(self):
        """The labels of the issue cannot be read: nothing is known about it."""
        original = autodispatch.gh

        def broken_gh(args, timeout=60):
            if args[:2] == ["issue", "view"]:
                self.gh_calls.append(list(args))
                return 1, "", "gh: HTTP 502"
            return original(args, timeout=timeout)

        with mock.patch.object(autodispatch, "gh", side_effect=broken_gh):
            self.down(now=at(8, 30))
            records = self.up(now=at(9))
        self.assertEqual(records[0]["requeued"], [])
        self.assertIn("M6", self.state)
        self.assertEqual([call for call in self.gh_calls if call[:2] == ["issue", "edit"]], [])


class ReachabilityTests(RecoveryFixture):
    """The reachability observation is the edge the recovery acts on."""

    def test_a_host_that_never_went_down_is_not_reported(self):
        self.assertEqual(self.up(now=at(9)), [])

    def test_the_outage_is_kept_in_the_state(self):
        self.down(now=at(8, 30))
        self.assertEqual(self.state["_hosts"]["box"], {"reachable": False, "down_since": at(8, 30)})
        self.up(now=at(9))
        self.assertEqual(self.state["_hosts"]["box"], {"reachable": True, "down_since": None})

    def test_a_host_in_the_ssh_backoff_keeps_the_last_observation(self):
        """A host that is not probed says nothing: the edge is not lost."""
        self.down(now=at(8, 30))
        res = {"M6": {"error": "host in backoff", "skip": True}}
        self.assertEqual(autodispatch.recover_hosts(self.lanes, self.state, res, now=at(8, 31)), [])
        self.assertEqual(self.state["_hosts"]["box"], {"reachable": False, "down_since": at(8, 30)})

    def test_a_second_outage_is_detected_again(self):
        """The incident had two dropouts; every edge is handled on its own."""
        self.down(now=at(8, 30))
        first = self.up(now=at(9))
        self.down(now=at(9, 30))
        second = self.up(now=at(10))
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["down_s"], 1800.0)

    def test_the_recovery_is_logged_in_the_state_dir(self):
        self.down(now=at(8, 30))
        self.up(now=at(9))
        records = [json.loads(line) for line in
                   (self.dir / autodispatch.RECOVERY_LOG).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["host"], "box")
        self.assertEqual(records[0]["recovered_at"], at(9))


class TickTests(RecoveryFixture):
    """The recovery runs in the tick of the control loop, not beside it."""

    def test_the_tick_releases_the_hold_and_requeues_the_lost_run(self):
        """The real path: the host drops out, comes back, and the tick recovers."""
        self.write_hold("auto: ssh streak 3\n")
        probes = [{"error": "ssh: connect to host box port 22: No route to host"}, dead_run()]
        with mock.patch.object(autodispatch, "remote", side_effect=lambda *a, **k: probes.pop(0)), \
             mock.patch.object(autodispatch, "queue", return_value=[]), \
             mock.patch.object(autodispatch, "replenish", return_value=[]):
            self.clock = at(8, 30)
            autodispatch.tick(self.lanes, self.state, 0)
            self.clock = at(8, 32)   # the ssh backoff of the host expired
            autodispatch.tick(self.lanes, self.state, 0)
        self.assertFalse(self.hold_exists())
        edits = [call for call in self.gh_calls if call[:2] == ["issue", "edit"]]
        self.assertEqual(len(edits), 1)
        self.assertIn("lane-ready", edits[0])
        self.assertNotIn("M6", self.state)
        self.assertNotIn("M6", json.loads((self.dir / "state.json").read_text(encoding="utf-8")))

    def test_the_tick_keeps_a_surviving_run(self):
        probes = [{"error": "ssh: connect to host box port 22: No route to host"}, alive_run()]
        with mock.patch.object(autodispatch, "remote", side_effect=lambda *a, **k: probes.pop(0)), \
             mock.patch.object(autodispatch, "queue", return_value=[]), \
             mock.patch.object(autodispatch, "replenish", return_value=[]):
            self.clock = at(8, 30)
            autodispatch.tick(self.lanes, self.state, 0)
            self.clock = at(8, 32)
            autodispatch.tick(self.lanes, self.state, 0)
        self.assertIn("M6", self.state)
        self.assertEqual([call for call in self.gh_calls if call[:2] == ["issue", "edit"]], [])

    def test_an_orphaned_run_is_still_requeued_without_an_outage(self):
        """The recovery does not replace the orphan flow of the tick."""
        probes = [dead_run()]
        with mock.patch.object(autodispatch, "remote", side_effect=lambda *a, **k: probes.pop(0)), \
             mock.patch.object(autodispatch, "queue", return_value=[]), \
             mock.patch.object(autodispatch, "replenish", return_value=[]):
            self.clock = at(9)
            autodispatch.tick(self.lanes, self.state, 0)
        edits = [call for call in self.gh_calls if call[:2] == ["issue", "edit"]]
        self.assertEqual(len(edits), 1)
        self.assertIn("lane-ready", edits[0])
        self.assertNotIn("M6", self.state)

    def test_the_tick_keeps_the_lane_until_the_label_edit_works(self):
        """A failed `gh issue edit` keeps the lane; a later tick retries and releases it."""
        self.edit_error = True
        probes = [{"error": "ssh: connect to host box port 22: No route to host"}, dead_run()]
        with mock.patch.object(autodispatch, "remote", side_effect=lambda *a, **k: probes.pop(0)), \
             mock.patch.object(autodispatch, "queue", return_value=[]), \
             mock.patch.object(autodispatch, "replenish", return_value=[]):
            self.clock = at(8, 30)
            autodispatch.tick(self.lanes, self.state, 0)
            self.clock = at(8, 32)   # the ssh backoff of the host expired
            autodispatch.tick(self.lanes, self.state, 0)
        # the labels were not updated: the lane record survives, the issue is not lost
        self.assertIn("M6", self.state)
        self.assertIn("M6", json.loads((self.dir / "state.json").read_text(encoding="utf-8")))
        self.edit_error = False
        probes = [dead_run()]
        with mock.patch.object(autodispatch, "remote", side_effect=lambda *a, **k: probes.pop(0)), \
             mock.patch.object(autodispatch, "queue", return_value=[]), \
             mock.patch.object(autodispatch, "replenish", return_value=[]):
            self.clock = at(8, 34)
            autodispatch.tick(self.lanes, self.state, 0)
        self.assertNotIn("M6", self.state)
        self.assertNotIn("M6", json.loads((self.dir / "state.json").read_text(encoding="utf-8")))
        edits = [call for call in self.gh_calls if call[:2] == ["issue", "edit"]]
        self.assertTrue(edits)
        self.assertIn("lane-ready", edits[-1])


if __name__ == "__main__":
    unittest.main()
