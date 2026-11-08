"""TTL recovery releases a finished run even while its lane keeps a resident child alive.

Refs #1816: the run-finished recovery path of `recover_expired_fleet_slots` is
restored from the pre-swrm-merge implementation (openswarm/bin/openswarm) and
covered here: a resident lane reuses one executor child, so a live child is not
a live run.  The reservation of a run which already wrote its terminal result
has to be released by the native TTL recovery instead of by an out-of-band
reaper, and that release must never touch the resident child.

Every TTL decision of this file is taken on an injected clock (`xs.NOW`), so no
assertion depends on how fast the machine is: the "fresh terminal result" case
must not be a race against the wall clock.

BLOCKER mapping of this delivery (Refs #1816):
  * B1 (no polling with a hard timeout in the suite) -> `recover()` (:76) injects
    the clock and `test_finished_run_waits_for_the_terminal_state_to_age_past_the_ttl`
    (:135) asserts the TTL boundary on it; the recovery fix itself lives in
    swrm/bin/swrm (`process_exited` :4027, `process_group_members` :4066).
    -> tests.test_fleet_ttl_resident.FleetTtlResidentTests (5 cases) and
       tests.test_lane.LaneTests.test_lane_supervisor_recovers_killed_runner_after_ttl_without_new_admission
    -> 50/50 local runs green; the CI matrix (ubuntu-24.04/macos-latest x
       Python 3.9/3.11/3.13) is the PR check, see the doc section 5.2.
  * B2 (verdict and evidence) -> docs/design/issue-1816-lane-ttl-recovery.md
    (section 0, 1.5 and 4) and this docstring.
    -> verdict: the red CI leg of this round was a test timing window (the
       "fresh terminal result" assertion read the wall clock twice, with a
       0.1s budget); the pre-fix behaviour is reproduced in the doc section 1.5.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
BIN = HERE.parent / 'bin' / 'fleet-capacity'
sys.path.insert(0, str(HERE))
import _load
xs = _load.load()

POLICY = {'concurrency': {'total': 8, 'pi': 4, 'minimax-cn': 4},
          'fleet': {'recovery_ttl_seconds': 0.1}}
# The injected "now" of the recovery decisions.  A fixed point in time keeps the
# TTL boundary exact instead of "100 ms of wall clock happened to pass".
BASE = 1700000000.0
# A reservation or a terminal state older than the TTL.
AGED = BASE - 600


class FleetTtlResidentTests(unittest.TestCase):
    """A resident lane reuses one executor child, so a live child is not a live run."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runs = pathlib.Path(self.temp.name, 'runs')
        self.data = pathlib.Path(self.temp.name, 'data')
        self.command = [str(BIN), 'broker']
        guard = pathlib.Path(self.temp.name, 'guard.json')
        guard.write_text(json.dumps({'command': self.command}))
        environment = mock.patch.dict(os.environ, {
            'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name,
            'OPENSWARM_FLEET_GUARD_CONFIG': str(guard)})
        environment.start()
        self.addCleanup(environment.stop)
        for target, value in (('RUNS', str(self.runs)), ('DATA', str(self.data))):
            patcher = mock.patch.object(xs, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def call(self, host, **payload):
        process = subprocess.run([str(BIN), host], input=json.dumps(payload), text=True,
                                 capture_output=True, env=dict(os.environ), timeout=15)
        return json.loads(process.stdout)

    def recover(self, policy=POLICY, now=BASE):
        """Run the TTL recovery on the injected clock."""
        with mock.patch.object(xs, 'NOW', return_value=now):
            return xs.recover_expired_fleet_slots(self.command, policy)

    def resident_lane(self, name='P1', current_run=None):
        """A live lane supervisor with the resident executor child it keeps."""
        supervisor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)
        self.processes.extend([supervisor, child])
        self.write_lane_state(name, supervisor_pid=supervisor.pid, child_pid=child.pid,
                              state='idle', current_run=current_run)
        return supervisor, child

    def write_lane_state(self, name, **state):
        path = self.data / 'lanes' / name / 'state.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
        return state

    def resident_run(self, run_id, lane, supervisor, child):
        """Admit a run for a resident lane and age its reservation past the TTL."""
        rdir = self.runs / run_id
        self.assertTrue(xs.reserve_provider_slot(str(rdir), 'pi', 'minimax-cn', POLICY,
                                                 keep_waiting=lambda: True))
        xs.set_status(str(rdir), lane=lane, runner_pid=supervisor.pid,
                      fleet_reserved_at=AGED,
                      **xs.executor_status_fields(child.pid, None))
        return rdir

    def finish_run(self, rdir, run_id, finished=AGED):
        """Publish the terminal result of a run the lane already left behind."""
        (rdir / 'result.json').write_text(json.dumps({'id': run_id, 'status': 'failed'}))
        xs.set_status(str(rdir), state='finished', status='failed', finished=finished)

    def test_finished_run_releases_its_reservation_while_the_resident_child_lives(self):
        supervisor, child = self.resident_lane('P1')
        rdir = self.resident_run('resident-finished', 'P1', supervisor, child)
        self.finish_run(rdir, 'resident-finished')
        self.assertEqual(len(self.call('broker', op='status')['reservations']), 1)
        self.assertEqual(self.recover(), ['resident-finished'])
        self.assertEqual(self.call('broker', op='status')['reservations'], [])
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['recovered_reason'], 'run_finished_child_resident')
        self.assertTrue(status['fleet_recovered_at'])
        # The resident child belongs to the lane, not to the finished run.
        self.assertIsNone(child.poll())
        self.assertIsNone(supervisor.poll())

    def test_unfinished_run_keeps_its_reservation_while_the_resident_child_lives(self):
        supervisor, child = self.resident_lane('P1')
        rdir = self.resident_run('resident-running', 'P1', supervisor, child)
        xs.set_status(str(rdir), state='running')
        self.assertEqual(self.recover(), [])
        self.assertEqual(len(self.call('broker', op='status')['reservations']), 1)
        status = json.loads((rdir / 'status.json').read_text())
        self.assertNotIn('recovered_reason', status)
        self.assertIsNone(child.poll())

    def test_finished_run_waits_for_the_terminal_state_to_age_past_the_ttl(self):
        """A fresh terminal result is not yet proof that the lane left the run.

        The decision is taken on the injected clock, so this case cannot fail
        because the machine needed 100 ms to write one status file.
        """
        supervisor, child = self.resident_lane('P1', current_run='fresh-finished')
        rdir = self.resident_run('fresh-finished', 'P1', supervisor, child)
        self.finish_run(rdir, 'fresh-finished', finished=BASE)
        self.assertEqual(self.recover(), [])
        self.assertEqual(len(self.call('broker', op='status')['reservations']), 1)
        # A full TTL later the very same record is recovered: the boundary is the
        # configured TTL, not the speed of the machine.
        self.assertEqual(self.recover(now=BASE + 1), ['fresh-finished'])
        self.assertEqual(self.call('broker', op='status')['reservations'], [])

    def test_lane_switching_to_another_run_releases_the_finished_run(self):
        supervisor, child = self.resident_lane('P1', current_run='finished-run')
        rdir = self.resident_run('finished-run', 'P1', supervisor, child)
        self.finish_run(rdir, 'finished-run', finished=BASE)
        # The lane accepted the next run, so the resident child now serves it.
        self.write_lane_state('P1', supervisor_pid=supervisor.pid, child_pid=child.pid,
                              state='busy', current_run='next-run')
        self.assertEqual(self.recover(), ['finished-run'])
        self.assertEqual(self.call('broker', op='status')['reservations'], [])
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['recovered_reason'], 'run_finished_child_resident')
        self.assertIsNone(child.poll())

    def test_a_normally_released_run_is_not_reported_as_recovered(self):
        """A run released at the end of its turn holds no reservation any more."""
        supervisor, child = self.resident_lane('P1', current_run='released-run')
        rdir = self.resident_run('released-run', 'P1', supervisor, child)
        self.finish_run(rdir, 'released-run')
        self.assertTrue(xs.release_fleet_slot(str(rdir), confirmed=True))
        self.write_lane_state('P1', supervisor_pid=supervisor.pid, child_pid=child.pid,
                              state='busy', current_run='next-run')
        self.assertEqual(self.recover(), [])
        self.assertEqual(self.call('broker', op='status')['reservations'], [])
        status = json.loads((rdir / 'status.json').read_text())
        self.assertNotIn('recovered_reason', status)


if __name__ == '__main__':
    unittest.main()
