"""TTL recovery releases a finished run even while its lane keeps a resident child alive."""
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


class FleetTtlResidentTests(unittest.TestCase):
    """A resident lane reuses one executor child, so a live child is not a live run.

    The lane keeps that child alive between turns, so the reservation of a run
    which already wrote its terminal result has to be released by the native TTL
    recovery instead of by an out-of-band reaper.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runs = pathlib.Path(self.temp.name, 'runs')
        self.data = pathlib.Path(self.temp.name, 'data')
        self.command = [str(BIN), 'gocc']
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
                      fleet_reserved_at=time.time() - 600,
                      **xs.executor_status_fields(child.pid, None))
        return rdir

    def finish_run(self, rdir, run_id, finished):
        """Publish the terminal result of a run the lane already left behind."""
        (rdir / 'result.json').write_text(json.dumps({'id': run_id, 'status': 'failed'}))
        xs.set_status(str(rdir), state='finished', status='failed', finished=finished)

    def test_finished_run_releases_its_reservation_while_the_resident_child_lives(self):
        supervisor, child = self.resident_lane('P1')
        rdir = self.resident_run('resident-finished', 'P1', supervisor, child)
        self.finish_run(rdir, 'resident-finished', time.time() - 600)
        self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
        self.assertEqual(xs.recover_expired_fleet_slots(self.command, POLICY), ['resident-finished'])
        self.assertEqual(self.call('gocc', op='status')['reservations'], [])
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
        self.assertEqual(xs.recover_expired_fleet_slots(self.command, POLICY), [])
        self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
        status = json.loads((rdir / 'status.json').read_text())
        self.assertNotIn('recovered_reason', status)
        self.assertIsNone(child.poll())

    def test_finished_run_waits_for_the_terminal_state_to_age_past_the_ttl(self):
        """A fresh terminal result is not yet proof that the lane left the run."""
        supervisor, child = self.resident_lane('P1', current_run='fresh-finished')
        rdir = self.resident_run('fresh-finished', 'P1', supervisor, child)
        self.finish_run(rdir, 'fresh-finished', time.time())
        self.assertEqual(xs.recover_expired_fleet_slots(self.command, POLICY), [])
        self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)

    def test_lane_switching_to_another_run_releases_the_finished_run(self):
        supervisor, child = self.resident_lane('P1', current_run='finished-run')
        rdir = self.resident_run('finished-run', 'P1', supervisor, child)
        self.finish_run(rdir, 'finished-run', time.time())
        # The lane accepted the next run, so the resident child now serves it.
        self.write_lane_state('P1', supervisor_pid=supervisor.pid, child_pid=child.pid,
                              state='busy', current_run='next-run')
        self.assertEqual(xs.recover_expired_fleet_slots(self.command, POLICY), ['finished-run'])
        self.assertEqual(self.call('gocc', op='status')['reservations'], [])
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['recovered_reason'], 'run_finished_child_resident')
        self.assertIsNone(child.poll())

    def test_a_normally_released_run_is_not_reported_as_recovered(self):
        """A run released at the end of its turn holds no reservation any more."""
        supervisor, child = self.resident_lane('P1', current_run='released-run')
        rdir = self.resident_run('released-run', 'P1', supervisor, child)
        self.finish_run(rdir, 'released-run', time.time() - 600)
        self.assertTrue(xs.release_fleet_slot(str(rdir), confirmed=True))
        self.write_lane_state('P1', supervisor_pid=supervisor.pid, child_pid=child.pid,
                              state='busy', current_run='next-run')
        self.assertEqual(xs.recover_expired_fleet_slots(self.command, POLICY), [])
        self.assertEqual(self.call('gocc', op='status')['reservations'], [])
        status = json.loads((rdir / 'status.json').read_text())
        self.assertNotIn('recovered_reason', status)


if __name__ == '__main__':
    unittest.main()
