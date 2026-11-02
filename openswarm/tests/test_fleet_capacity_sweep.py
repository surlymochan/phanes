"""Fleet capacity: every run end releases its slot, the sweeper reclaims the ghosts (Refs #1685).

The 2026-11-02 incident: a run that ended with ``exec_error`` kept its capacity
reservation, so the next run of the same host stayed queued for 23 minutes, and
two placeholder reservations of a cancelled worker were still holding host slots
20 hours later.  The tests below cover the run end matrix (exec_error,
budget_exceeded, a killed executor) and the ghost sweeper: it names the known
leftovers and never touches a live run.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
BIN = HERE.parent / 'bin' / 'fleet-capacity'
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()


def ns(**kw):
    base = dict(type="impl", difficulty=3, context_tokens=0, backend="pi", model=None, effort=None,
                prompt="add a function add(a, b)", task_file=None, title="capacity test", cwd=".",
                no_worktree=True, verify=None, verify_baseline=False, keep_worktree=False,
                no_fallback=True, budget_minutes=5, detach=False, run_id=None, dry_run=False, paths=None)
    base.update(kw)
    return argparse.Namespace(**base)


class FleetCapacityFixture(unittest.TestCase):
    """A host with a real capacity coordinator and a private run home."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.runs = self.root / 'runs'
        self.data = self.root / 'data'
        self.capacity = self.root / 'capacity'
        self.work = self.root / 'work'
        for path in (self.runs, self.data, self.work):
            path.mkdir()
        guard = self.root / 'guard.json'
        guard.write_text(json.dumps({'command': [str(BIN), 'gocc']}))
        self.env = {'OPENSWARM_FLEET_GUARD_CONFIG': str(guard),
                    'OPENSWARM_FLEET_CAPACITY_DIR': str(self.capacity)}
        env_patch = mock.patch.dict(os.environ, self.env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for patch in (mock.patch.object(xs, 'RUNS', str(self.runs)),
                      mock.patch.object(xs, 'DATA', str(self.data))):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(xs._FLEET_RESERVATIONS.clear)

    def call(self, host='gocc', **payload):
        process = subprocess.run([str(BIN), host], input=json.dumps(payload), text=True,
                                 capture_output=True, env=dict(os.environ, **self.env), timeout=15)
        self.assertTrue(process.stdout.strip(), process.stderr)
        return json.loads(process.stdout)

    def reservations(self):
        return self.call('gocc', op='status')['reservations']

    def reserve(self, run_id, token=None, backend='pi', provider='minimax-cn'):
        """Take a reservation and age it, so it looks like a leftover."""
        token = token or hashlib.sha256(run_id.encode()).hexdigest()[:32]
        answer = self.call('gocc', op='acquire', token=token, run_id=run_id,
                            backend=backend, provider=provider)
        self.assertTrue(answer.get('admitted'), answer)
        return token

    def age_reservation(self, run_id, seconds):
        """Make a reservation look as old as the incident (its created stamp)."""
        db = sqlite3.connect(str(self.capacity / 'capacity.sqlite'))
        try:
            db.execute('UPDATE reservations SET created=? WHERE run_id=?', (time.time() - seconds, run_id))
            db.commit()
        finally:
            db.close()

    def make_run(self, run_id, status):
        rdir = self.runs / run_id
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / 'status.json').write_text(json.dumps(status))
        return rdir


class FleetCapacityEndPathTests(FleetCapacityFixture):
    """Every abnormal run end has to give the capacity slot back."""

    def failing_executor(self, rc, error='executor failed'):
        def fake(_decision, _state, _prompt, _wd, _rdir, _timeout, _events):
            return rc, None, {'usage': {}}, error
        return fake

    def dispatch(self, executor):
        """Run a whole run with a fake executor and return its exit code."""
        decision = {'ok': True, 'backend': 'pi', 'model': 'fake-model', 'effort': None, 'tier': 'standard',
                    'fallbacks': [], 'claude_review': None, 'reasons': [], 'concurrency': {'total': 8}}
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(xs, 'route', return_value=decision), \
             mock.patch.dict(xs.EXECUTORS, {'pi': executor}), \
             mock.patch.object(xs, 'rate_limit_policy', return_value=([], 0)), \
             mock.patch.object(xs, 'rate_limit_delay', return_value=0):
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    xs.cmd_run(ns(cwd=str(self.work)), xs.load_policy())
                except SystemExit as exit_code:
                    return exit_code.code
        self.fail('cmd_run returned without exiting')

    def test_exec_error_end_releases_the_reservation(self):
        self.assertEqual(self.dispatch(self.failing_executor(1)), 1)
        self.assertEqual(self.reservations(), [])

    def test_budget_exceeded_end_releases_the_reservation(self):
        """A timeout must not keep the slot: the executor tree is already gone."""
        self.assertEqual(self.dispatch(self.failing_executor(124, 'budget exhausted')), 1)
        self.assertEqual(self.reservations(), [])

    def test_killed_executor_end_releases_the_reservation(self):
        """A killed executor (SIGKILL) ends the run; the slot goes back."""
        self.assertEqual(self.dispatch(self.failing_executor(-9, 'executor killed')), 1)
        self.assertEqual(self.reservations(), [])

    def test_rate_limited_end_releases_the_reservation(self):
        self.assertEqual(self.dispatch(self.failing_executor(75, '429 rate limit exceeded')), 1)
        self.assertEqual(self.reservations(), [])

    def test_finished_run_records_the_release_reason(self):
        self.dispatch(self.failing_executor(1))
        runs = sorted(self.runs.glob('*/status.json'))
        self.assertEqual(len(runs), 1)
        status = json.loads(runs[0].read_text())
        self.assertEqual(status['state'], 'finished')
        self.assertTrue(status['fleet_release_reason'].startswith('run-finished:'))

    def test_failed_release_is_alerted(self):
        """A release that keeps failing is written to the alert log."""
        real_call = xs.fleet_guard_call

        def fail_release(command, payload, timeout=15):
            if payload.get('op') == 'release':
                raise RuntimeError('coordinator unavailable')
            return real_call(command, payload, timeout=timeout)

        with mock.patch.object(xs, 'fleet_guard_call', side_effect=fail_release):
            self.assertEqual(self.dispatch(self.failing_executor(1)), 1)
        alerts = (self.data / 'fleet-capacity-alerts.jsonl').read_text().splitlines()
        self.assertTrue(alerts, 'the failed release was not alerted')
        row = json.loads(alerts[-1])
        self.assertEqual(row['kind'], 'fleet_release_failed')
        self.assertIn('coordinator unavailable', row['error'])

    def test_a_failed_release_is_retried_before_it_is_alerted(self):
        """The release retries, and only a release which keeps failing is alerted."""
        real_call = xs.fleet_guard_call
        attempts = []

        def fail_release(command, payload, timeout=15):
            if payload.get('op') == 'release':
                attempts.append(payload)
                raise RuntimeError('coordinator unavailable')
            return real_call(command, payload, timeout=timeout)

        with mock.patch.object(xs, 'fleet_guard_call', side_effect=fail_release):
            self.assertEqual(self.dispatch(self.failing_executor(1)), 1)
        # Two rounds of three attempts: the executor turn and the run end.
        self.assertEqual(len(attempts), 6)
        alerts = (self.data / 'fleet-capacity-alerts.jsonl').read_text().splitlines()
        self.assertEqual(len(alerts), 1)

    def test_a_live_executor_keeps_the_slot_at_the_run_end(self):
        """A run whose executor is still alive must not give its slot away."""
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])

        def fake(_decision, _state, _prompt, _wd, rdir, _timeout, _events):
            xs.set_status(rdir, executor_pid=child.pid)
            return 1, None, {'usage': {}}, 'executor failed'

        try:
            self.assertEqual(self.dispatch(fake), 1)
            self.assertEqual(len(self.reservations()), 1)
            status = json.loads(sorted(self.runs.glob('*/status.json'))[0].read_text())
            self.assertIn('retained after abnormal executor exit', status['fleet_reservation_error'])
        finally:
            child.kill()
            child.wait(timeout=5)

    def test_a_lane_run_with_a_live_child_keeps_the_shared_slot(self):
        """A lane shares its slot with the resident child, so the run end keeps it.

        The lane turn releases the slot itself when the child is gone; while a
        recorded descendant still lives, the slot belongs to that work.
        """
        rdir = self.runs / 'lane-run'
        rdir.mkdir(parents=True, exist_ok=True)
        policy = {'concurrency': {'total': 8}}
        self.assertTrue(xs.reserve_provider_slot(str(rdir), 'pi', 'minimax-cn', policy,
                                                 keep_waiting=lambda: True))
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            xs.set_status(str(rdir), id='lane-run', state='finished', status='failed', lane='P1',
                          runner_pid=os.getpid(), executor_pid=child.pid)
            self.assertFalse(xs.release_fleet_slot_at_run_end(str(rdir), 'lane-run-finished:failed'))
            self.assertEqual(len(self.reservations()), 1)
            status = json.loads((rdir / 'status.json').read_text())
            self.assertIn('retained after abnormal executor exit', status['fleet_reservation_error'])
            self.assertNotIn('fleet_release_reason', status)
        finally:
            child.kill()
            child.wait(timeout=5)
            self.assertTrue(xs.release_fleet_slot(str(rdir)))

    def test_a_stale_executor_scan_keeps_the_slot(self):
        """Without a subreaper an unrecorded detached child may still be alive."""
        rdir = self.runs / 'stale-scan'
        rdir.mkdir(parents=True, exist_ok=True)
        policy = {'concurrency': {'total': 8}}
        self.assertTrue(xs.reserve_provider_slot(str(rdir), 'pi', 'minimax-cn', policy,
                                                 keep_waiting=lambda: True))
        xs.set_status(str(rdir), id='stale-scan', state='finished', status='failed',
                      executor_scan_stale=True)
        self.assertFalse(xs.release_fleet_slot_at_run_end(str(rdir), 'run-finished:failed'))
        self.assertEqual(len(self.reservations()), 1)
        self.assertTrue(xs.release_fleet_slot(str(rdir)))

    def test_the_slot_release_needs_a_gone_executor_tree(self):
        """The gate of the run end release: no recorded executor may be alive."""
        rdir = self.runs / 'gate'
        rdir.mkdir(parents=True, exist_ok=True)
        self.assertTrue(xs.fleet_slot_release_safe(str(rdir)))
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            xs.set_status(str(rdir), executor_pid=child.pid)
            self.assertFalse(xs.fleet_slot_release_safe(str(rdir)))
        finally:
            child.kill()
            child.wait(timeout=5)
        self.assertTrue(xs.fleet_slot_release_safe(str(rdir)))
        xs.set_status(str(rdir), executor_scan_stale=True)
        self.assertFalse(xs.fleet_slot_release_safe(str(rdir)))


class FleetSweepTests(FleetCapacityFixture):
    """The ghost sweeper names the leftovers and never touches a live run."""

    def policy(self, **overrides):
        return {'fleet': {'sweep_grace_seconds': 600, 'sweep_interval_seconds': 300,
                          'sweep_enabled': True, 'sweep_apply': False, **overrides}}

    def test_dry_run_names_the_known_ghosts_and_keeps_the_live_run(self):
        """The leftovers of that night: a cancelled worker and a finished run."""
        now = time.time()
        self.reserve('cancel-worker')
        self.age_reservation('cancel-worker', 20 * 3600)
        self.reserve('finished-run')
        self.age_reservation('finished-run', 30 * 60)
        self.make_run('finished-run', {'id': 'finished-run', 'state': 'finished',
                                        'updated': now - 1800, 'finished': now - 1800})
        live = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            self.make_run('live-run', {'id': 'live-run', 'state': 'running', 'updated': now,
                                        'runner_pid': live.pid})
            self.reserve('live-run')
            self.age_reservation('live-run', 30 * 60)
            report = xs.sweep_fleet_reservations(command=[str(BIN), 'gocc'], policy=self.policy(),
                                                 dry_run=True, now=now, runs_root=str(self.runs))
            self.assertTrue(report['ok'], report)
            named = {row['run_id']: row for row in report['candidates']}
            self.assertEqual(sorted(named), ['cancel-worker', 'finished-run'])
            self.assertEqual(named['cancel-worker']['basis'], 'run-missing')
            self.assertEqual(named['finished-run']['basis'], 'run-finished')
            self.assertNotIn('live-run', named)
            self.assertEqual(report['released'], [])
            # A dry run releases nothing.
            self.assertEqual(len(self.reservations()), 3)
            log = (self.data / 'fleet-capacity-sweep.jsonl').read_text().splitlines()
            self.assertEqual({json.loads(line)['run_id'] for line in log},
                             {'cancel-worker', 'finished-run'})
        finally:
            live.terminate()
            live.wait(timeout=5)

    def test_apply_releases_the_ghosts_and_keeps_the_live_run(self):
        now = time.time()
        self.reserve('cancel-worker')
        self.age_reservation('cancel-worker', 20 * 3600)
        live = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            self.make_run('live-run', {'id': 'live-run', 'state': 'running', 'updated': now,
                                        'runner_pid': live.pid})
            self.reserve('live-run')
            self.age_reservation('live-run', 30 * 60)
            report = xs.sweep_fleet_reservations(command=[str(BIN), 'gocc'], policy=self.policy(),
                                                 dry_run=False, now=now, runs_root=str(self.runs))
            self.assertTrue(report['ok'], report)
            self.assertEqual([row['run_id'] for row in report['released']], ['cancel-worker'])
            self.assertEqual([row['run_id'] for row in self.reservations()], ['live-run'])
        finally:
            live.terminate()
            live.wait(timeout=5)

    def test_sweep_release_is_audited_by_the_coordinator(self):
        self.reserve('cancel-worker')
        self.age_reservation('cancel-worker', 20 * 3600)
        report = xs.sweep_fleet_reservations(command=[str(BIN), 'gocc'], policy=self.policy(),
                                             dry_run=False, runs_root=str(self.runs))
        self.assertEqual([row['run_id'] for row in report['released']], ['cancel-worker'])
        audit = [json.loads(line) for line in (self.capacity / 'audit.jsonl').read_text().splitlines()]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['host'], 'gocc')
        self.assertEqual(audit[0]['run_id'], 'cancel-worker')
        self.assertEqual(audit[0]['basis'], 'run-missing')
        self.assertGreater(audit[0]['age_seconds'], 600)

    def test_a_fresh_reservation_is_never_swept(self):
        """A reservation that was just taken is not a ghost yet."""
        self.reserve('fresh-run')
        self.make_run('fresh-run', {'id': 'fresh-run', 'state': 'queued', 'updated': time.time()})
        report = xs.sweep_fleet_reservations(command=[str(BIN), 'gocc'], policy=self.policy(),
                                             dry_run=True, runs_root=str(self.runs))
        self.assertEqual(report['candidates'], [])
        self.assertEqual(len(self.reservations()), 1)

    def test_a_run_without_a_readable_status_is_never_swept(self):
        """Incomplete evidence keeps the reservation."""
        self.reserve('no-status')
        self.age_reservation('no-status', 20 * 3600)
        (self.runs / 'no-status').mkdir()
        report = xs.sweep_fleet_reservations(command=[str(BIN), 'gocc'], policy=self.policy(),
                                             dry_run=True, runs_root=str(self.runs))
        self.assertEqual(report['candidates'], [])
        self.assertEqual(len(self.reservations()), 1)

    def test_a_dead_runner_with_a_stale_status_is_swept(self):
        """The dual condition: the runner is gone and the status file is stale."""
        now = time.time()
        self.reserve('killed-run')
        self.age_reservation('killed-run', 30 * 60)
        self.make_run('killed-run', {'id': 'killed-run', 'state': 'running', 'updated': now - 3600,
                                     'runner_pid': 999999999})
        report = xs.sweep_fleet_reservations(command=[str(BIN), 'gocc'], policy=self.policy(),
                                             dry_run=True, now=now, runs_root=str(self.runs))
        self.assertEqual([row['run_id'] for row in report['candidates']], ['killed-run'])
        self.assertEqual(report['candidates'][0]['basis'], 'runner-dead-stale')

    def test_a_dead_runner_with_a_fresh_status_is_kept(self):
        """The runner may only have died a moment ago."""
        now = time.time()
        self.reserve('just-died')
        self.age_reservation('just-died', 30 * 60)
        self.make_run('just-died', {'id': 'just-died', 'state': 'running', 'updated': now - 5,
                                    'runner_pid': 999999999})
        report = xs.sweep_fleet_reservations(command=[str(BIN), 'gocc'], policy=self.policy(),
                                             dry_run=True, now=now, runs_root=str(self.runs))
        self.assertEqual(report['candidates'], [])

    def test_a_run_without_a_runner_pid_is_never_swept(self):
        """Without a runner to probe, the run may still be alive: do not touch it."""
        now = time.time()
        self.reserve('unknown-runner')
        self.age_reservation('unknown-runner', 30 * 60)
        self.make_run('unknown-runner', {'id': 'unknown-runner', 'state': 'running', 'updated': now - 3600})
        report = xs.sweep_fleet_reservations(command=[str(BIN), 'gocc'], policy=self.policy(),
                                             dry_run=True, now=now, runs_root=str(self.runs))
        self.assertEqual(report['candidates'], [])
        self.assertEqual(len(self.reservations()), 1)

    def test_a_stale_executor_scan_is_never_swept(self):
        """An unrecorded detached child may still use the slot."""
        now = time.time()
        self.reserve('stale-scan')
        self.age_reservation('stale-scan', 30 * 60)
        self.make_run('stale-scan', {'id': 'stale-scan', 'state': 'running', 'updated': now - 3600,
                                     'runner_pid': 999999999, 'executor_scan_stale': True})
        report = xs.sweep_fleet_reservations(command=[str(BIN), 'gocc'], policy=self.policy(),
                                             dry_run=True, now=now, runs_root=str(self.runs))
        self.assertEqual(report['candidates'], [])
        self.assertEqual(len(self.reservations()), 1)

    def test_the_reservations_of_another_host_are_never_swept(self):
        """The sweeper only judges the reservations of its own host."""
        self.call('gores1', op='acquire', token='b' * 32, run_id='remote-run',
                  backend='pi', provider='minimax-cn')
        report = xs.sweep_fleet_reservations(command=[str(BIN), 'gocc'], policy=self.policy(),
                                             dry_run=False, runs_root=str(self.runs))
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['candidates'], [])
        self.assertEqual(len(self.reservations()), 1)

    def test_the_sweeper_needs_a_coordinator(self):
        with mock.patch.object(xs, 'fleet_guard_command', return_value=None):
            report = xs.sweep_fleet_reservations(command=None, policy=self.policy(),
                                                 dry_run=True, runs_root=str(self.runs))
        self.assertFalse(report['ok'])
        self.assertTrue(report['error'])


class FleetSweepModuleTests(unittest.TestCase):
    """The judgement of one reservation, offline and reproducible."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runs = pathlib.Path(self.temp.name) / 'runs'
        self.runs.mkdir()
        self.sweep = xs.fleet_sweep_module()
        self.now = 1800000000.0

    def make_run(self, run_id, status):
        rdir = self.runs / run_id
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / 'status.json').write_text(json.dumps(status))
        return rdir

    def classify(self, run_id, created=None, status=None):
        reservation = {'host': 'gocc', 'run_id': run_id, 'backend': 'pi', 'provider': 'minimax-cn',
                       'created': self.now - 3600 if created is None else created}
        run = self.sweep.load_run(str(self.runs), run_id)
        if status is not None:
            run = {'exists': True, 'status_exists': True, 'status': status, 'run_dir': str(self.runs / run_id)}
        return self.sweep.classify(reservation, run, self.now, 600)

    def test_a_missing_run_is_a_ghost(self):
        decision = self.classify('cancel-worker')
        self.assertEqual(decision['action'], 'release')
        self.assertEqual(decision['basis'], 'run-missing')

    def test_a_finished_run_is_a_ghost_after_the_grace(self):
        decision = self.classify('done', status={'state': 'finished', 'finished': self.now - 1200,
                                                  'updated': self.now - 1200})
        self.assertEqual(decision['action'], 'release')
        self.assertEqual(decision['basis'], 'run-finished')

    def test_a_finished_run_inside_the_grace_is_kept(self):
        decision = self.classify('done', status={'state': 'finished', 'finished': self.now - 60,
                                                  'updated': self.now - 60})
        self.assertEqual(decision['action'], 'keep')
        self.assertEqual(decision['basis'], 'run-finished-grace')

    def test_a_terminal_run_with_a_live_runner_is_kept(self):
        decision = self.classify('finalizing', status={'state': 'finished', 'finished': self.now - 1200,
                                                        'updated': self.now - 1200, 'runner_pid': os.getpid()})
        self.assertEqual(decision['action'], 'keep')
        self.assertEqual(decision['basis'], 'runner-alive')

    def test_a_live_run_is_kept(self):
        decision = self.classify('live', status={'state': 'running', 'updated': self.now - 5000,
                                                 'runner_pid': os.getpid()})
        self.assertEqual(decision['action'], 'keep')
        self.assertEqual(decision['basis'], 'runner-alive')

    def test_a_run_without_a_runner_pid_is_kept(self):
        decision = self.classify('unknown', status={'state': 'running', 'updated': self.now - 5000})
        self.assertEqual(decision['action'], 'keep')
        self.assertEqual(decision['basis'], 'runner-unknown')

    def test_a_stale_executor_scan_is_kept(self):
        decision = self.classify('stale', status={'state': 'running', 'updated': self.now - 5000,
                                                  'runner_pid': 999999999, 'executor_scan_stale': True})
        self.assertEqual(decision['action'], 'keep')
        self.assertEqual(decision['basis'], 'executor-scan-stale')

    def test_the_cli_reports_the_decisions(self):
        self.make_run('live', {'state': 'running', 'updated': self.now, 'runner_pid': os.getpid()})
        reservations = self.root_file()
        process = subprocess.run([sys.executable, str(HERE.parent / 'lib' / 'supervise' / 'fleet_sweep.py'),
                                  '--runs', str(self.runs), '--reservations', str(reservations),
                                  '--now', str(self.now), '--json'],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        rows = json.loads(process.stdout)
        self.assertEqual([row['action'] for row in rows], ['keep', 'release'])
        self.assertEqual(rows[1]['basis'], 'run-missing')

    def root_file(self):
        path = pathlib.Path(self.temp.name) / 'reservations.json'
        path.write_text(json.dumps({'reservations': [
            {'host': 'gocc', 'run_id': 'live', 'backend': 'pi', 'provider': None, 'created': self.now - 3600},
            {'host': 'gocc', 'run_id': 'gone', 'backend': 'pi', 'provider': None, 'created': self.now - 3600},
        ]}))
        return path


if __name__ == '__main__':
    unittest.main()
