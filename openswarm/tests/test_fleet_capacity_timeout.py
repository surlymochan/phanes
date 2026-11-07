"""Capacity coordinator calls are bounded: a slow link cannot hang a run (Refs #1694)."""
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

REAL_SLEEP = time.sleep


class LoopBreak(Exception):
    """Raised to leave the admission loop after its first cycle."""


class FleetCapacityTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runs = pathlib.Path(self.temp.name, 'runs')
        self.data = pathlib.Path(self.temp.name, 'data')
        self.runs.mkdir(parents=True)
        self.data.mkdir(parents=True)
        self.env = {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}
        for attribute, value in (('DATA', str(self.data)), ('RUNS', str(self.runs))):
            patch = mock.patch.object(xs, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, authenticated_host, **payload):
        process = subprocess.run([str(BIN), authenticated_host], input=json.dumps(payload), text=True,
                                 capture_output=True, env=dict(os.environ, **self.env), timeout=15)
        return json.loads(process.stdout)

    def test_silent_coordinator_is_cut_off_at_the_call_timeout(self):
        """A coordinator which never answers must not hold the caller."""
        command = ['/bin/sh', '-c', 'sleep 30', 'gocc']
        started = time.monotonic()
        with mock.patch.object(xs, 'FLEET_CALL_TIMEOUT_SECONDS', 1):
            with self.assertRaises(RuntimeError) as caught:
                xs.fleet_guard_call(command, {'op': 'status'})
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn('timeout after 1s', str(caught.exception))
        self.assertEqual(caught.exception.reason, 'unreachable')

    def test_status_call_shares_the_bounded_policy(self):
        """The status call point answers with the coordinator's reservations."""
        command = [str(BIN), 'gocc']
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(xs.fleet_capacity_status(command), [])
            self.assertTrue(self.call('gocc', op='acquire', token='a' * 32, run_id='r1',
                                      backend='pi', provider='minimax-cn')['admitted'])
            reservations = xs.fleet_capacity_status(command)
        self.assertEqual([row['run_id'] for row in reservations], ['r1'])

    def test_failed_call_retries_with_an_exponential_backoff(self):
        """A failed call is retried a bounded number of times with a growing delay."""
        sleeps = []
        with mock.patch.object(xs, 'fleet_guard_call', side_effect=RuntimeError('coordinator unavailable')) as call, \
             mock.patch.object(xs.time, 'sleep', sleeps.append):
            with self.assertRaises(RuntimeError) as caught:
                xs.fleet_guard_call_retry(['guard'], {'op': 'acquire'}, op='acquire')
        self.assertEqual(call.call_count, 3)
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(caught.exception.op, 'acquire')

    def test_healthy_coordinator_needs_one_call_and_no_backoff(self):
        """The normal path stays unchanged: one call, no sleep, no extra log."""
        rdir = self.runs / 'healthy'
        command = [str(BIN), 'gocc']
        policy = {'concurrency': {'total': 8}}
        ops = []
        real_call = xs.fleet_guard_call
        sleeps = []

        def counting(command, payload, timeout=None):
            ops.append(payload['op'])
            return real_call(command, payload, timeout=timeout)

        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=command), \
             mock.patch.object(xs, 'fleet_guard_call', counting), \
             mock.patch.object(xs.time, 'sleep', sleeps.append):
            self.assertTrue(xs.reserve_provider_slot(str(rdir), 'pi', 'minimax-cn', policy,
                                                      keep_waiting=lambda: True))
        self.assertEqual(ops, ['acquire'])
        # Only the sub-millisecond polling of the subprocess wait loop ran.
        self.assertEqual([seconds for seconds in sleeps if seconds >= 0.1], [])
        self.assertFalse((rdir / 'capacity.jsonl').exists())
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['state'], 'running')
        self.assertIsNone(status.get('queue_reason'))
        self.assertFalse(status.get('capacity_unreachable'))

    def test_silent_coordinator_leaves_the_run_queued_and_logs_the_give_up(self):
        """The acquire gives up after its retries: the run stays queued and is logged."""
        rdir = self.runs / 'silent'
        command = ['/bin/sh', '-c', 'sleep 30', 'gocc']
        sleeps = []

        def fake_sleep(seconds):
            # The subprocess wait loop polls in sub-millisecond steps; only the
            # backoff of the coordinator call is a real wait.
            if seconds < 0.1:
                REAL_SLEEP(seconds)
                return
            sleeps.append(seconds)
            if len(sleeps) > 2:
                raise LoopBreak()

        with mock.patch.object(xs, 'FLEET_CALL_TIMEOUT_SECONDS', 1), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=command), \
             mock.patch.object(xs.time, 'sleep', fake_sleep):
            with self.assertRaises(LoopBreak):
                xs.reserve_provider_slot(str(rdir), 'codex', None, {'concurrency': {'total': 8}},
                                         keep_waiting=lambda: True)
        # The two retries wait 0.5s and 1.0s; the third attempt is the last one.
        self.assertEqual(sleeps[:2], [0.5, 1.0])
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['state'], 'queued')
        # An unreachable coordinator is not a capacity shortage.
        self.assertEqual(status['queue_reason'], 'capacity coordinator unreachable')
        self.assertIn('timeout', status['fleet_reservation_error'])
        # A reservation which may exist is never dropped.
        self.assertTrue(status['fleet_reservation_token'])
        self.assertTrue(status['fleet_reservation_pending'])
        events = [json.loads(line) for line in (rdir / 'capacity.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['type'], 'capacity-unreachable')
        self.assertEqual(events[-1]['op'], 'acquire')
        self.assertEqual(events[-1]['attempts'], 3)
        self.assertGreaterEqual(events[-1]['seconds'], 3)
        self.assertEqual(status['fleet_capacity_last']['op'], 'acquire')
        self.assertEqual(status['fleet_capacity_last']['attempts'], 3)

    def test_a_rejected_request_is_not_retried_and_is_not_an_outage(self):
        """A coordinator which refuses the request answered: it is not an outage."""
        rdir = self.runs / 'rejected'
        command = [str(BIN), 'gocc']
        calls = []
        sleeps = []

        def reject(command, payload, timeout=None):
            calls.append(payload['op'])
            raise xs.FleetCapacityError('fleet capacity coordinator rejected request: invalid backend',
                                        reason='rejected')

        with mock.patch.object(xs, 'fleet_guard_command', return_value=command), \
             mock.patch.object(xs, 'fleet_guard_call', reject), \
             mock.patch.object(xs.time, 'sleep', sleeps.append):
            self.assertFalse(xs.reserve_provider_slot(str(rdir), 'codex', None,
                                                      {'concurrency': {'total': 8}},
                                                      keep_waiting=lambda: False))
        # A definitive answer is not retried.
        self.assertEqual(calls, ['acquire'])
        self.assertEqual([seconds for seconds in sleeps if seconds >= 0.1], [])
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['queue_reason'], 'fleet capacity request rejected')
        self.assertFalse(status.get('capacity_unreachable'))
        self.assertIsNone(status.get('capacity_unreachable_since'))

    def test_capacity_unreachable_is_marked_only_after_the_threshold(self):
        """A short blip is not an outage; a lasting one is marked for the diagnosis."""
        rdir = self.runs / 'outage'
        rdir.mkdir()
        with mock.patch.object(xs, 'NOW', return_value=1000.0):
            self.assertFalse(xs.note_capacity_unreachable(str(rdir), 'acquire', RuntimeError('boom')))
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['capacity_unreachable_since'], 1000.0)
        self.assertFalse(status['capacity_unreachable'])
        with mock.patch.object(xs, 'NOW', return_value=1030.0):
            self.assertFalse(xs.note_capacity_unreachable(str(rdir), 'acquire', RuntimeError('boom')))
        with mock.patch.object(xs, 'NOW', return_value=1061.0):
            self.assertTrue(xs.note_capacity_unreachable(str(rdir), 'acquire', RuntimeError('boom')))
        status = json.loads((rdir / 'status.json').read_text())
        self.assertTrue(status['capacity_unreachable'])
        self.assertEqual(status['capacity_unreachable_since'], 1000.0)

    def test_failed_release_is_queued_and_the_sweep_resends_it(self):
        """A failed release must not block the run: it is queued for the sweep."""
        rdir = self.runs / 'release-failed'
        command = [str(BIN), 'gocc']
        policy = {'concurrency': {'total': 8}, 'fleet': {'recovery_ttl_seconds': 0.1}}
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=command):
            self.assertTrue(xs.reserve_provider_slot(str(rdir), 'pi', 'minimax-cn', policy,
                                                      keep_waiting=lambda: True))
        token = json.loads((rdir / 'status.json').read_text())['fleet_reservation_token']
        with mock.patch.object(xs, 'fleet_guard_call', side_effect=RuntimeError('coordinator unavailable')):
            self.assertFalse(xs.release_fleet_slot(str(rdir), confirmed=True, attempts=1))
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['fleet_release_pending']['token'], token)
        queue = json.loads((self.data / 'fleet-release-queue.json').read_text())
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]['token'], token)
        self.assertEqual(queue[0]['host'], 'gocc')
        self.assertEqual(queue[0]['run_id'], rdir.name)
        # The reservation is still held until the sweep resends the release.
        self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(xs.flush_fleet_release_queue(command, limit=1), [rdir.name])
        self.assertEqual(json.loads((self.data / 'fleet-release-queue.json').read_text()), [])
        self.assertEqual(self.call('gocc', op='status')['reservations'], [])
        released = json.loads((rdir / 'status.json').read_text())
        self.assertIsNone(released['fleet_release_pending'])

    def test_release_queue_keeps_the_entries_of_another_host(self):
        """Only the owner of a reservation may release it."""
        queue_path = self.data / 'fleet-release-queue.json'
        queue_path.write_text(json.dumps([
            {'token': 'a' * 32, 'host': 'gores1', 'run_id': 'other', 'attempts': 0},
            {'token': 'b' * 32, 'host': 'gocc', 'run_id': 'mine', 'attempts': 0},
        ]))
        self.assertEqual(xs.flush_fleet_release_queue([str(BIN), 'gocc'], limit=2), ['mine'])
        entries = xs.fleet_release_queue_entries()
        self.assertEqual([entry['host'] for entry in entries], ['gores1'])

    def test_run_ends_although_the_release_cannot_be_delivered(self):
        """The end of a run never waits for the coordinator."""
        rdir = self.runs / 'run-end'
        command = [str(BIN), 'gocc']
        policy = {'concurrency': {'total': 8}, 'fleet': {'recovery_ttl_seconds': 0.1}}
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=command):
            self.assertTrue(xs.reserve_provider_slot(str(rdir), 'pi', 'minimax-cn', policy,
                                                      keep_waiting=lambda: True))
        calls = []
        real_call = xs.fleet_guard_call

        def failing_release(command, payload, timeout=None):
            calls.append(payload['op'])
            if payload['op'] == 'release':
                raise RuntimeError('coordinator unavailable')
            return real_call(command, payload, timeout=timeout)

        with mock.patch.object(xs, 'fleet_guard_call', failing_release):
            self.assertFalse(xs.release_fleet_slot(str(rdir), confirmed=True, attempts=1))
        # One bounded attempt: the run end is not delayed by a retry storm.
        self.assertEqual(calls, ['release'])
        self.assertEqual(len(xs.fleet_release_queue_entries()), 1)
        self.assertEqual(json.loads((rdir / 'status.json').read_text())['state'], 'running')


if __name__ == '__main__':
    unittest.main()
