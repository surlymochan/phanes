"""Cross-host admission is atomic and uncertain reservations are never expired."""
import concurrent.futures
import json
import os
import pathlib
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
BIN = HERE.parent / 'bin' / 'fleet-capacity'
CLI = HERE.parent / 'bin' / 'openswarm'
sys.path.insert(0, str(HERE))
import _load
xs = _load.load()


def stop_executor_group(pgid, timeout=5):
    """Stop a recorded executor group and wait for it to disappear.

    The lane may already have reaped the group when the executor exits early,
    so a missing group is the expected outcome rather than an error.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, TypeError, ValueError):
        return
    deadline = time.monotonic() + timeout
    while xs.process_group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)

class FleetCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=self.temp.name)

    def call(self, authenticated_host, **payload):
        p = subprocess.run([str(BIN), authenticated_host], input=json.dumps(payload), text=True,
                           capture_output=True, env=self.env, timeout=15)
        return json.loads(p.stdout)

    def test_atomic_minimax_limit_and_idempotent_recovery(self):
        def attempt(i):
            return self.call('gocc' if i % 2 else 'gores1', op='acquire',
                             token=f'{i:032x}', run_id=f'run-{i}', backend='pi', provider='minimax-cn')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            answers = list(pool.map(attempt, range(8)))
        # Four MiniMax turns may be admitted globally.
        self.assertEqual(sum(bool(a.get('admitted')) for a in answers), 4)
        winner = next(i for i, a in enumerate(answers) if a.get('admitted'))
        host = 'gocc' if winner % 2 else 'gores1'
        token = f'{winner:032x}'
        self.assertTrue(self.call(host, op='acquire', token=token, run_id=f'run-{winner}',
                                  backend='pi', provider='minimax-cn')['existing'])
        self.assertFalse(self.call('gores1' if host == 'gocc' else 'gocc', op='release',
                                   token=token, run_id=f'run-{winner}')['ok'])
        self.assertEqual(len(self.call(host, op='status')['reservations']), 4)
        for i, answer in enumerate(answers):
            if not answer.get('admitted'):
                continue
            owner = 'gocc' if i % 2 else 'gores1'
            self.assertTrue(self.call(owner, op='release', token=f'{i:032x}', run_id=f'run-{i}')['released'])
        self.assertEqual(self.call(host, op='status')['reservations'], [])

    def test_agy_and_copilot_caps_match_requested_422_allocation(self):
        def acquire(backend, number):
            host = 'gocc' if number % 2 else 'gores1'
            return self.call(host, op='acquire', token=f'{number:032x}',
                             run_id=f'{backend}-{number}', backend=backend, provider=None)

        # Test each backend cap separately in the two-host deployment plan.
        agy = [acquire('agy', number) for number in range(1, 5)]
        self.assertEqual(sum(bool(answer.get('admitted')) for answer in agy), 2)
        for number, answer in enumerate(agy, 1):
            if answer.get('admitted'):
                host = 'gocc' if number % 2 else 'gores1'
                self.assertTrue(self.call(host, op='release', token=f'{number:032x}',
                                          run_id=f'agy-{number}')['released'])

        copilot = [acquire('copilot', number) for number in range(11, 14)]
        self.assertEqual(sum(bool(answer.get('admitted')) for answer in copilot), 2)

    def test_two_host_mixed_422_allocation_releases_a_slot(self):
        """The two-host layout admits 4 Pi, 2 AGY and 2 Copilot turns.

        gocc owns two Antigravity turns and two Copilot turns; gores1 supplies
        the four MiniMax turns. A ninth turn is rejected by the global cap.
        """
        requests = [
            ('gores1', 'pi', 'minimax-cn', 'minimax-1'),
            ('gores1', 'pi', 'minimax-cn', 'minimax-2'),
            ('gores1', 'pi', 'minimax-cn', 'minimax-3'),
            ('gores1', 'pi', 'minimax-cn', 'minimax-4'),
            ('gocc', 'agy', None, 'agy-1'),
            ('gocc', 'agy', None, 'agy-2'),
            ('gocc', 'copilot', None, 'copilot-1'),
            ('gocc', 'copilot', None, 'copilot-2'),
        ]
        admitted = []
        for number, (host, backend, provider, run_id) in enumerate(requests):
            token = f'{number + 100:032x}'
            result = self.call(host, op='acquire', token=token, run_id=run_id,
                               backend=backend, provider=provider)
            self.assertTrue(result['admitted'], (run_id, result))
            admitted.append((host, token, run_id))

        self.assertEqual(len(self.call('gocc', op='status')['reservations']), 8)
        ninth = self.call('gocc', op='acquire', token='f' * 32, run_id='extra-codex',
                          backend='codex', provider=None)
        self.assertFalse(ninth['admitted'])
        self.assertIn('total', ninth['blocked'])

        host, token, run_id = admitted[-1]
        self.assertTrue(self.call(host, op='release', token=token, run_id=run_id)['released'])
        self.assertTrue(self.call('gocc', op='acquire', token='f' * 32,
                                  run_id='extra-codex', backend='codex', provider=None)['admitted'])

    def test_disconnected_coordinator_fails_closed(self):
        config = pathlib.Path(self.temp.name, 'guard.json')
        config.write_text(json.dumps({'command': ['/nonexistent/coordinator']}))
        old = os.environ.get('OPENSWARM_FLEET_GUARD_CONFIG')
        os.environ['OPENSWARM_FLEET_GUARD_CONFIG'] = str(config)
        child = None
        try:
            rdir = pathlib.Path(self.temp.name, 'runs', 'blocked')
            ok = xs.reserve_provider_slot(str(rdir), 'codex', None, {'concurrency': {'total': 1}},
                                          keep_waiting=lambda: False)
            self.assertFalse(ok)
            status = json.loads((rdir / 'status.json').read_text())
            self.assertEqual(status['state'], 'queued')
            self.assertIn('coordinator unavailable', status['fleet_reservation_error'])
        finally:
            if old is None: os.environ.pop('OPENSWARM_FLEET_GUARD_CONFIG', None)
            else: os.environ['OPENSWARM_FLEET_GUARD_CONFIG'] = old

    def test_required_missing_configuration_fails_closed(self):
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_GUARD_REQUIRED': '1',
                                          'OPENSWARM_FLEET_GUARD_CONFIG': os.path.join(self.temp.name, 'missing')}):
            with self.assertRaisesRegex(RuntimeError, 'required but missing'):
                xs.fleet_guard_command()

    def test_capacity_configuration_missing_field_reports_readable_error(self):
        """A capacity configuration without its required field must fail readably."""
        config = pathlib.Path(self.temp.name, 'fleet-capacity.json')
        config.write_text(json.dumps({'hosts': ['gocc'], 'limits': {'total': 4}}))
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_GUARD_CONFIG': str(config)}):
            with self.assertRaises(RuntimeError) as caught:
                xs.fleet_guard_command()
        message = str(caught.exception)
        self.assertIn('fleet capacity configuration invalid', message)
        self.assertIn('command', message)
        for leak in ('Traceback', 'KeyError', 'json.decoder', 'File "'):
            self.assertNotIn(leak, message)

    def test_ssh_guard_trailing_owner_is_recorded_and_recovers(self):
        """An SSH alias is transport only; the explicit final owner drives lifecycle."""
        command = ['/usr/bin/ssh', '-T', '-F', '/fixed/config', 'openswarm-pi-broker', 'gores1']
        rdir = pathlib.Path(self.temp.name, 'runs', 'gores-turn')
        with mock.patch.object(xs, 'fleet_guard_command', return_value=command), \
             mock.patch.object(xs, 'fleet_guard_call', return_value={'admitted': True}):
            self.assertTrue(xs.reserve_provider_slot(str(rdir), 'pi', 'minimax-cn',
                                                      {'concurrency': {'total': 8}}, keep_waiting=lambda: True))
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['fleet_host'], 'gores1')
        self.assertIn('fleet_reserved_at', status)
        status.update({'state': 'running', 'runner_pid': 999999999,
                       'executor_pid': None, 'executor_pgid': None, 'fleet_reserved_at': 0.0})
        (rdir / 'status.json').write_text(json.dumps(status))
        with mock.patch.object(xs, 'RUNS', str(rdir.parent)), \
             mock.patch.object(xs, 'NOW', return_value=601.0), \
             mock.patch.object(xs, 'pid_alive', return_value=False), \
             mock.patch.object(xs, 'executor_stopped', return_value=True), \
             mock.patch.object(xs, 'fleet_guard_call', return_value={'released': 1}) as call:
            self.assertEqual(xs.recover_expired_fleet_slots(command, {'fleet': {'recovery_ttl_seconds': 600}}),
                             ['gores-turn'])
        # The owner frees its own dead reservation with the exact token.
        self.assertEqual(call.call_args.args[1]['token'], status['fleet_reservation_token'])

    def test_release_retries_transient_connection_failure(self):
        rdir = os.path.realpath(os.path.join(self.temp.name, 'run'))
        xs._FLEET_RESERVATIONS[rdir] = (['guard'], {'run_id': 'run', 'token': 'a' * 32})
        self.addCleanup(xs._FLEET_RESERVATIONS.pop, rdir, None)
        with mock.patch.object(xs, 'fleet_guard_call', side_effect=[RuntimeError('unavailable'), {'ok': True}]) as call, \
                mock.patch.object(xs.time, 'sleep'):
            self.assertTrue(xs.release_fleet_slot(rdir))
        self.assertEqual(call.call_count, 2)
        self.assertNotIn(rdir, xs._FLEET_RESERVATIONS)

    def test_release_retains_reservation_after_repeated_failure(self):
        rdir = os.path.realpath(os.path.join(self.temp.name, 'run'))
        xs._FLEET_RESERVATIONS[rdir] = (['guard'], {'run_id': 'run', 'token': 'a' * 32})
        self.addCleanup(xs._FLEET_RESERVATIONS.pop, rdir, None)
        with mock.patch.object(xs, 'fleet_guard_call', side_effect=RuntimeError('unavailable')) as call, \
                mock.patch.object(xs.time, 'sleep'), mock.patch.object(xs, 'set_status'):
            self.assertFalse(xs.release_fleet_slot(rdir))
        self.assertEqual(call.call_count, 3)
        self.assertIn(rdir, xs._FLEET_RESERVATIONS)

    def test_timeout_after_committed_acquire_persists_ttl_recovery_metadata(self):
        """A lost acquire reply must leave a recoverable record without starting work."""
        runs = pathlib.Path(self.temp.name, 'runs')
        rdir = runs / 'lost-acquire-reply'
        command = [str(BIN), 'gocc']
        policy = {'concurrency': {'total': 8}, 'fleet': {'recovery_ttl_seconds': 0.1}}
        real_call = xs.fleet_guard_call

        def commit_then_timeout(_command, payload, timeout=15):
            answer = real_call(command, payload, timeout=timeout)
            if payload['op'] == 'acquire':
                raise RuntimeError('coordinator unavailable: reply lost')
            return answer

        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'RUNS', str(runs)), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=command), \
             mock.patch.object(xs, 'fleet_guard_call', side_effect=commit_then_timeout):
            self.assertFalse(xs.reserve_provider_slot(str(rdir), 'pi', 'minimax-cn', policy,
                                                       keep_waiting=lambda: False))
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['fleet_host'], 'gocc')
        self.assertTrue(status['fleet_reservation_token'])
        self.assertTrue(status['fleet_reservation_pending'])
        self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'RUNS', str(runs)), \
             mock.patch.object(xs, 'NOW', return_value=status['fleet_reserved_at'] + 1):
            self.assertEqual(xs.recover_expired_fleet_slots(command, policy), ['lost-acquire-reply'])
        recovered = json.loads((rdir / 'status.json').read_text())
        self.assertIsNone(recovered['fleet_reservation_token'])
        self.assertEqual(self.call('gocc', op='status')['reservations'], [])

    def test_finished_release_failure_recovers_after_process_restart(self):
        """A confirmed release retry must not kill a resident lane child."""
        runs = pathlib.Path(self.temp.name, 'runs')
        data = pathlib.Path(self.temp.name, 'data')
        rdir = runs / 'release-failed'
        command = [str(BIN), 'gocc']
        policy = {'concurrency': {'total': 8}, 'fleet': {'recovery_ttl_seconds': 0.1}}
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'RUNS', str(runs)), \
             mock.patch.object(xs, 'DATA', str(data)), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=command):
            self.assertTrue(xs.reserve_provider_slot(str(rdir), 'pi', 'minimax-cn', policy,
                                                      keep_waiting=lambda: True))
        status = json.loads((rdir / 'status.json').read_text())
        supervisor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        resident = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        try:
            self.assertIsNone(supervisor.poll())
            self.assertIsNone(resident.poll())
            lane_state = data / 'lanes' / 'P1' / 'state.json'
            lane_state.parent.mkdir(parents=True)
            lane_state.write_text(json.dumps({'supervisor_pid': supervisor.pid, 'child_pid': resident.pid,
                                               'state': 'idle'}))
            xs.set_status(str(rdir), state='finished', status='failed', lane='P1', runner_pid=supervisor.pid,
                          **xs.executor_status_fields(resident.pid, None))
            with mock.patch.object(xs, 'fleet_guard_call', side_effect=RuntimeError('coordinator unavailable')):
                self.assertFalse(xs.release_fleet_slot(str(rdir), confirmed=True))
            self.assertTrue(json.loads((rdir / 'status.json').read_text())['fleet_release_pending'])
            # Simulate a supervisor restart: recovery may use only the exact
            # persisted release token, never the resident executor process.
            xs._FLEET_RESERVATIONS.pop(os.path.realpath(rdir), None)
            with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
                 mock.patch.object(xs, 'RUNS', str(runs)), \
                 mock.patch.object(xs, 'DATA', str(data)), \
                 mock.patch.object(xs, 'NOW', return_value=status['fleet_reserved_at'] + 1):
                self.assertEqual(xs.recover_expired_fleet_slots(command, policy), ['release-failed'])
            self.assertIsNone(supervisor.poll())
            self.assertIsNone(resident.poll())
        finally:
            supervisor.terminate()
            supervisor.wait(timeout=5)
            resident.terminate()
            resident.wait(timeout=5)
        self.assertEqual(self.call('gocc', op='status')['reservations'], [])

    def test_terminal_resident_lane_without_confirmed_release_is_retained(self):
        """A terminal status alone never releases or kills a live resident lane child."""
        runs = pathlib.Path(self.temp.name, 'runs')
        data = pathlib.Path(self.temp.name, 'data')
        rdir = runs / 'resident-terminal'
        command = [str(BIN), 'gocc']
        policy = {'concurrency': {'total': 8}, 'fleet': {'recovery_ttl_seconds': 0.1}}
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'RUNS', str(runs)), \
             mock.patch.object(xs, 'DATA', str(data)), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=command):
            self.assertTrue(xs.reserve_provider_slot(str(rdir), 'pi', 'minimax-cn', policy,
                                                      keep_waiting=lambda: True))
        status = json.loads((rdir / 'status.json').read_text())
        supervisor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        resident = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        try:
            lane_state = data / 'lanes' / 'P1' / 'state.json'
            lane_state.parent.mkdir(parents=True)
            lane_state.write_text(json.dumps({'supervisor_pid': supervisor.pid, 'child_pid': resident.pid,
                                               'state': 'idle'}))
            xs.set_status(str(rdir), state='finished', lane='P1', runner_pid=supervisor.pid,
                          **xs.executor_status_fields(resident.pid, None))
            with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
                 mock.patch.object(xs, 'RUNS', str(runs)), \
                 mock.patch.object(xs, 'DATA', str(data)), \
                 mock.patch.object(xs, 'NOW', return_value=status['fleet_reserved_at'] + 1):
                self.assertEqual(xs.recover_expired_fleet_slots(command, policy), [])
            self.assertIsNone(supervisor.poll())
            self.assertIsNone(resident.poll())
            self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
        finally:
            supervisor.terminate()
            supervisor.wait(timeout=5)
            resident.terminate()
            resident.wait(timeout=5)
            self.assertTrue(self.call('gocc', op='release', token=status['fleet_reservation_token'],
                                      run_id='resident-terminal')['released'])

    def test_live_runner_with_persisted_reservation_is_not_recovered(self):
        """An in-flight run keeps capacity even if its durable token is past TTL."""
        runs = pathlib.Path(self.temp.name, 'runs')
        rdir = runs / 'still-running'
        command = [str(BIN), 'gocc']
        policy = {'concurrency': {'total': 8}, 'fleet': {'recovery_ttl_seconds': 0.1}}
        token = '1' * 32
        reserved_at = time.time()
        self.assertTrue(self.call('gocc', op='acquire', token=token, run_id='still-running',
                                  backend='pi', provider='minimax-cn')['admitted'])
        supervisor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            xs.set_status(str(rdir), id='still-running', state='running', runner_pid=supervisor.pid,
                          fleet_host='gocc', fleet_reserved_at=reserved_at,
                          fleet_reservation_token=token, executor_pid=None, executor_pgid=None)
            with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
                 mock.patch.object(xs, 'RUNS', str(runs)), \
                 mock.patch.object(xs, 'NOW', return_value=reserved_at + 1):
                self.assertEqual(xs.recover_expired_fleet_slots(command, policy), [])
            self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
        finally:
            supervisor.terminate()
            supervisor.wait(timeout=5)
            self.assertTrue(self.call('gocc', op='release', token=token, run_id='still-running')['released'])

    def test_remote_review_assignment_passes_read_only_to_lane(self):
        root = pathlib.Path(self.temp.name)
        binary = root / '.local/bin/openswarm'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                          'open(os.environ["FAKE_ARGS"],"w").write(json.dumps(sys.argv))\n'
                          'print(json.dumps({"ok":True,"id":"test"}))\n')
        binary.chmod(0o755)
        self.env['HOME'] = str(root)
        self.env['FAKE_ARGS'] = str(root / 'args.json')
        response = self.call('gores1', op='assign', lane='GH1', prompt='review', base='abcdef0',
                             budget_minutes=3, title='review', verify=None, read_only=True)
        self.assertTrue(response['ok'])
        self.assertIn('--read-only', json.loads((root / 'args.json').read_text()))

    def test_remote_completed_run_returns_git_bundle(self):
        root = pathlib.Path(self.temp.name)
        repo = root / 'repo'
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
        git('init', '-q')
        (repo / 'file').write_text('before')
        git('add', 'file')
        git('-c', 'user.name=test', '-c', 'user.email=test@test', 'commit', '-qm', 'base')
        base = git('rev-parse', 'HEAD')
        git('checkout', '-qb', 'openswarm/run-1')
        (repo / 'file').write_text('after')
        git('-c', 'user.name=test', '-c', 'user.email=test@test', 'commit', '-qam', 'result')
        commit = git('rev-parse', 'HEAD')
        fake = root / '.local/bin/openswarm'
        fake.parent.mkdir(parents=True)
        info = {'id':'run-1', 'result':{'branch':'openswarm/run-1', 'commit':commit,
                                       'workspace':{'repo':str(repo), 'base':base}}}
        fake.write_text('#!' + sys.executable + '\nimport json\nprint(' + repr(json.dumps(info)) + ')\n')
        fake.chmod(0o755)
        self.env['HOME'] = str(root)
        response = self.call('gores1', op='bundle', run_id='run-1')
        self.assertTrue(response['ok'])
        self.assertEqual(response['result']['commit'], commit)
        self.assertTrue(response['result']['bundle'])

    def test_html_403_is_not_misclassified_as_login_failure(self):
        error = 'request failed (code 403): HTML response "Error 403 (Forbidden)!!1"'
        self.assertEqual(xs.executor_error_class(1, None, error, 'agy'), 'upstream_forbidden')
        self.assertEqual(xs.executor_error_class(1, None, error, 'codex'), 'exec_error')

    def test_reaped_stopped_runner_releases_slot(self):
        guard = pathlib.Path(self.temp.name, 'guard.json')
        guard.write_text(json.dumps({'command': [str(BIN), 'gocc']}))
        env = {'OPENSWARM_FLEET_GUARD_CONFIG': str(guard), 'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}
        with mock.patch.dict(os.environ, env), mock.patch.dict(xs.EXECUTORS, {'pi': lambda *args: (133, None, {}, 'stopped')}):
            rdir = pathlib.Path(self.temp.name, 'runs', 'reaped')
            rdir.mkdir(parents=True)
            _, _, error = xs.run_executor_with_rate_limit('pi', {'backend': 'pi', 'model': 'fake'}, {}, 'review',
                                                          self.temp.name, str(rdir), xs.NOW() + 15,
                                                          {'concurrency': {'total': 8}})
            self.assertEqual(error, 'risk_blocked')
            self.assertEqual(self.call('gocc', op='status')['reservations'], [])

    def test_one_shot_agy_403_releases_slot_for_next_run(self):
        guard = pathlib.Path(self.temp.name, 'guard.json')
        guard.write_text(json.dumps({'command': [str(BIN), 'gocc']}))
        fake = HERE / 'fixtures/fake_agy.py'
        fake.chmod(0o755)
        env = {'OPENSWARM_FLEET_GUARD_CONFIG': str(guard),
               'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name,
               'OPENSWARM_AGY_BIN': str(fake), 'FAKE_AGY_MODE': 'forbidden-exit'}
        with mock.patch.dict(os.environ, env):
            for number in range(2):
                rdir = pathlib.Path(self.temp.name, 'runs', f'run-{number}')
                rdir.mkdir(parents=True)
                attempts, _, error = xs.run_executor_with_rate_limit(
                    'agy', {'backend': 'agy', 'model': 'fake-model'}, {}, 'review', self.temp.name,
                    str(rdir), xs.NOW() + 15, {'concurrency': {'total': 8}})
                self.assertEqual(error, 'upstream_forbidden')
                self.assertEqual(attempts[0]['rc'], 1)
                self.assertEqual(self.call('gocc', op='status')['reservations'], [])

    def test_recovery_requires_local_explicit_confirmation(self):
        token = 'a' * 32
        self.assertTrue(self.call('gores1', op='acquire', token=token, run_id='orphan',
                                  backend='cursor', provider=None)['admitted'])
        self.assertFalse(self.call('gores1', op='recover', host='gores1', run_id='orphan',
                                   confirm='all-executor-processes-stopped')['ok'])
        self.assertFalse(self.call('gocc', op='recover', host='gores1', run_id='orphan')['ok'])
        # A recovery without the exact token cannot address one reservation.
        self.assertFalse(self.call('gocc', op='recover', host='gores1', run_id='orphan',
                                   confirm='all-executor-processes-stopped')['ok'])
        # A live lease is never recovered: the owner may still be working.
        answer = self.call('gocc', op='recover', host='gores1', run_id='orphan', token=token,
                           confirm='all-executor-processes-stopped')
        self.assertEqual(answer['recovered'], 0)
        self.assertEqual(answer['reason'], 'lease not expired')
        self.assertEqual(len(self.call('gores1', op='status')['reservations']), 1)

    def test_leaderless_recorded_group_is_not_considered_confirmed(self):
        self.assertFalse(xs.recorded_executor_group_matches(
            {'pid': 999999999, 'pgid': os.getpgrp(), 'identity': 'stale-identity'}))

    def test_reused_executor_pid_never_discovers_or_kills_replacement_children(self):
        rdir = pathlib.Path(self.temp.name, 'runs', 'reused-executor-pid')
        rdir.mkdir(parents=True)
        replacement = subprocess.Popen([sys.executable, '-c',
                                        'import subprocess,sys,time; '
                                        'subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"], '
                                        'start_new_session=True); time.sleep(30)'], start_new_session=True,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline, children = time.monotonic() + 3, []
            while not children and time.monotonic() < deadline:
                children = xs.descendants(replacement.pid)
                time.sleep(.02)
            self.assertTrue(children)
            child = children[0]
            (rdir / 'status.json').write_text(json.dumps({
                'executor_pid': replacement.pid, 'executor_pgid': 999999999,
                'executor_identity': 'identity-of-an-earlier-process',
            }))
            self.assertFalse(xs.stop_orphaned_executor(str(rdir)))
            status = json.loads((rdir / 'status.json').read_text())
            self.assertEqual(status['executor_descendants'] if 'executor_descendants' in status else [], [])
            self.assertIn('identity cannot be confirmed', status['fleet_reservation_error'])
            self.assertTrue(xs.pid_alive(replacement.pid))
            self.assertTrue(xs.pid_alive(child))
        finally:
            if replacement.poll() is None:
                os.killpg(os.getpgid(replacement.pid), signal.SIGKILL)
                replacement.wait()
            if child and xs.pid_alive(child):
                try:
                    os.killpg(os.getpgid(child), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_killed_runner_recovers_after_ttl_when_executor_is_stopped(self):
        runs = pathlib.Path(self.temp.name, 'runs')
        rdir = runs / 'killed-runner'
        rdir.mkdir(parents=True)
        token = 'b' * 32
        self.assertTrue(self.call('gocc', op='acquire', token=token, run_id='killed-runner',
                                  backend='pi', provider='minimax-cn')['admitted'])
        runner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            os.kill(runner.pid, signal.SIGKILL)
            runner.wait()
            (rdir / 'status.json').write_text(json.dumps({
                'id': 'killed-runner', 'state': 'running', 'runner_pid': runner.pid,
                'executor_pid': None, 'executor_pgid': None, 'fleet_host': 'gocc',
                'fleet_reservation_token': token,
                'fleet_reserved_at': 100.0,
            }))
            policy = {'fleet': {'recovery_ttl_seconds': 10}}
            env = {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(xs, 'RUNS', str(runs)), \
                 mock.patch.object(xs, 'NOW', return_value=109.9):
                self.assertEqual(xs.recover_expired_fleet_slots([str(BIN), 'gocc'], policy), [])
            self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(xs, 'RUNS', str(runs)), \
                 mock.patch.object(xs, 'NOW', return_value=110.1):
                self.assertEqual(xs.recover_expired_fleet_slots([str(BIN), 'gocc'], policy), ['killed-runner'])
            self.assertEqual(self.call('gocc', op='status')['reservations'], [])
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()

    def test_killed_runner_ttl_recovery_waits_for_executor_group(self):
        runs = pathlib.Path(self.temp.name, 'runs')
        rdir = runs / 'live-descendant'
        rdir.mkdir(parents=True)
        token = 'c' * 32
        self.assertTrue(self.call('gocc', op='acquire', token=token, run_id='live-descendant',
                                  backend='pi', provider='minimax-cn')['admitted'])
        runner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        executor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.kill(runner.pid, signal.SIGKILL)
            runner.wait()
            (rdir / 'status.json').write_text(json.dumps({
                'id': 'live-descendant', 'state': 'running', 'runner_pid': runner.pid,
                'executor_pid': executor.pid, 'executor_pgid': os.getpgid(executor.pid), 'fleet_host': 'gocc',
                'executor_identity': xs.process_identity(executor.pid),
                'fleet_reservation_token': token,
                'fleet_reserved_at': 100.0,
            }))
            policy = {'fleet': {'recovery_ttl_seconds': 10}}
            env = {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(xs, 'RUNS', str(runs)), \
                 mock.patch.object(xs, 'stop_orphaned_executor') as stop, \
                 mock.patch.object(xs, 'NOW', return_value=110.1):
                self.assertEqual(xs.recover_expired_fleet_slots([str(BIN), 'gocc'], policy), [])
                stop.assert_called_once_with(str(rdir))
            self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(xs, 'RUNS', str(runs)), \
                 mock.patch.object(xs, 'NOW', return_value=110.1):
                # Termination alone is not enough: a still-unreaped process group retains its slot.
                self.assertEqual(xs.recover_expired_fleet_slots([str(BIN), 'gocc'], policy), [])
            self.assertIsNotNone(executor.poll())
            executor.wait()
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(xs, 'RUNS', str(runs)), \
                 mock.patch.object(xs, 'NOW', return_value=110.1):
                self.assertEqual(xs.recover_expired_fleet_slots([str(BIN), 'gocc'], policy), ['live-descendant'])
            self.assertEqual(self.call('gocc', op='status')['reservations'], [])
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()
            if executor.poll() is None:
                os.killpg(os.getpgid(executor.pid), signal.SIGKILL)
                executor.wait()

    def test_next_admission_reaps_a_confirmed_dead_runner_after_ttl(self):
        runs = pathlib.Path(self.temp.name, 'runs')
        old_dir = runs / 'old-run'
        old_dir.mkdir(parents=True)
        token = 'd' * 32
        self.assertTrue(self.call('gocc', op='acquire', token=token, run_id='old-run',
                                  backend='pi', provider='minimax-cn')['admitted'])
        (old_dir / 'status.json').write_text(json.dumps({
            'id': 'old-run', 'state': 'running', 'runner_pid': 999999999,
            'executor_pid': None, 'executor_pgid': None, 'fleet_host': 'gocc',
            'fleet_reservation_token': token,
            'fleet_reserved_at': 100.0,
        }))
        next_dir = runs / 'next-run'
        policy = {'concurrency': {'total': 8}, 'fleet': {'recovery_ttl_seconds': 10}}
        env = {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(xs, 'RUNS', str(runs)), \
             mock.patch.object(xs, 'NOW', return_value=110.1), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=[str(BIN), 'gocc']):
            self.assertTrue(xs.reserve_provider_slot(str(next_dir), 'pi', 'minimax-cn', policy,
                                                      keep_waiting=lambda: True))
            self.assertTrue(xs.release_fleet_slot(str(next_dir)))
        self.assertEqual(self.call('gocc', op='status')['reservations'], [])

    def test_host_limit_is_shared_across_backend_types(self):
        first = dict(op='acquire', token='1' * 32, run_id='one', backend='codex', provider=None)
        second = dict(op='acquire', token='2' * 32, run_id='two', backend='agy', provider=None)
        third = dict(op='acquire', token='3' * 32, run_id='three', backend='copilot', provider=None)
        fourth = dict(op='acquire', token='4' * 32, run_id='four', backend='cursor', provider=None)
        fifth = dict(op='acquire', token='5' * 32, run_id='five', backend='agy', provider=None)
        self.assertTrue(self.call('gocc', **first)['admitted'])
        self.assertTrue(self.call('gocc', **second)['admitted'])
        self.assertTrue(self.call('gocc', **third)['admitted'])
        self.assertTrue(self.call('gocc', **fourth)['admitted'])
        self.assertEqual(self.call('gocc', **fifth)['blocked'], ['host:gocc'])
        self.assertTrue(self.call('gores1', **fifth)['admitted'])

    def test_local_host_shares_backend_limit_and_cannot_recover(self):
        local = dict(op='acquire', token='1' * 32, run_id='local-turn', backend='codex', provider=None)
        remote = dict(op='acquire', token='2' * 32, run_id='remote-run', backend='codex', provider=None)
        self.assertTrue(self.call('local', **local)['admitted'])
        self.assertEqual(self.call('gocc', **remote)['blocked'], ['codex'])
        self.assertFalse(self.call('local', op='recover', host='local', run_id='local-turn',
                                   confirm='all-executor-processes-stopped')['ok'])
        self.assertFalse(self.call('gocc', op='recover', host='local', run_id='local-turn')['ok'])
        # The operator recovery needs the exact token, and a live lease is never recovered.
        answer = self.call('gocc', op='recover', host='local', run_id='local-turn', token='1' * 32,
                            confirm='all-executor-processes-stopped')
        self.assertEqual(answer['recovered'], 0)
        self.assertEqual(answer['reason'], 'lease not expired')
        # Only an explicit release frees the slot of a live lease.
        self.assertTrue(self.call('local', op='release', token='1' * 32, run_id='local-turn')['released'])
        self.assertTrue(self.call('gocc', **remote)['admitted'])

    def test_local_host_limit_applies_across_backends(self):
        for i, backend in enumerate(('codex', 'agy', 'copilot'), 1):
            result = self.call('local', op='acquire', token=f'{i:032x}', run_id=f'turn-{i}',
                               backend=backend, provider=None)
            if i < 3:
                self.assertTrue(result['admitted'])
            else:
                self.assertEqual(result['blocked'], ['host:local'])

    def test_lane_rate_limit_wait_cannot_exceed_task_budget(self):
        spec = {'started': 100.0, 'budget_minutes': 1}
        with mock.patch.object(xs, 'NOW', return_value=160.0), \
             mock.patch.object(xs, 'lane_control_messages'), \
             mock.patch.object(xs.time, 'sleep') as sleep:
            self.assertFalse(xs.lane_wait_rate_limit(120, {'backend': 'pi'}, spec,
                                                    mock.Mock(), None, {}, []))
            sleep.assert_not_called()

    def test_cancel_during_admission_releases_without_starting(self):
        rdir = os.path.join(self.temp.name, 'runs', 'cancelled')
        with mock.patch.object(xs, 'fleet_guard_command', return_value=['guard']), \
             mock.patch.object(xs, 'fleet_guard_call', side_effect=[{'admitted': True}, {'released': True}]) as call:
            self.assertFalse(xs.reserve_provider_slot(rdir, 'codex', None, {'concurrency': {'total': 2}},
                                                      keep_waiting=lambda: False))
            self.assertEqual([c.args[1]['op'] for c in call.call_args_list], ['acquire', 'release'])
            self.assertNotIn(os.path.realpath(rdir), xs._FLEET_RESERVATIONS)

    def test_early_agy_result_releases_one_shot_reservation(self):
        rdir = os.path.join(self.temp.name, 'runs', 'early-agy')
        os.makedirs(rdir)
        fake = mock.Mock(return_value=(1, None, {'usage':{}, 'finished_reason':'print_timeout'},
                                      'agy turn ended early: print_timeout'))
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=[str(BIN), 'gocc']), \
             mock.patch.dict(xs.EXECUTORS, {'agy':fake}):
            attempts, final, error = xs.run_executor_with_rate_limit(
                'agy', {'backend':'agy', 'model':'gemini-3.8-flash-medium', 'effort':None}, {}, 'task', self.temp.name, rdir,
                xs.NOW() + 30, xs.load_policy())
        self.assertEqual(error, 'exec_error')
        self.assertIsNone(final)
        self.assertEqual(self.call('gocc', op='status')['reservations'], [])

    def test_normal_executor_startup_failure_releases_one_shot_reservation(self):
        """A reaped non-zero executor must not strand a fleet slot."""
        rdir = os.path.join(self.temp.name, 'runs', 'startup-failure')
        os.makedirs(rdir)
        fake = mock.Mock(return_value=(1, None, {'usage': {}}, 'isolated Pi dependencies unavailable'))
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=[str(BIN), 'gocc']), \
             mock.patch.dict(xs.EXECUTORS, {'pi': fake}):
            attempts, final, error = xs.run_executor_with_rate_limit(
                'pi', {'backend': 'pi', 'model': 'MiniMax-M3.1-Flash', 'effort': None}, {}, 'task',
                self.temp.name, rdir, xs.NOW() + 30, xs.load_policy())
        self.assertEqual(error, 'exec_error')
        self.assertIsNone(final)
        self.assertEqual(attempts[0]['error_class'], 'exec_error')
        self.assertEqual(self.call('gocc', op='status')['reservations'], [])

    def test_normal_failure_retains_slot_while_executor_pid_is_live(self):
        """A missing group ID is not proof that the executor has stopped."""
        rdir = os.path.join(self.temp.name, 'runs', 'live-executor')
        os.makedirs(rdir)
        child = None

        def fake(_decision, _state, _prompt, _wd, run_dir, _timeout, _events):
            nonlocal child
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            xs.set_status(run_dir, executor_pid=child.pid)
            return 17, None, {'usage': {}}, 'executor failed'

        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=[str(BIN), 'gocc']), \
             mock.patch.dict(xs.EXECUTORS, {'pi': fake}):
            attempts, _final, error = xs.run_executor_with_rate_limit(
                'pi', {'backend': 'pi', 'model': 'MiniMax-M3.1-Flash', 'effort': None}, {}, 'task',
                self.temp.name, rdir, xs.NOW() + 30, xs.load_policy())
        try:
            self.assertEqual(error, 'exec_error')
            self.assertEqual(attempts[0]['rc'], 17)
            self.assertTrue(child and xs.pid_alive(child.pid))
            self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
        finally:
            if child and child.poll() is None:
                child.kill()
                child.wait()
            self.assertTrue(xs.release_fleet_slot(rdir))

    def test_signal_terminated_one_shot_retains_reservation(self):
        """A killed parent can leave an executor descendant alive; do not free its slot."""
        rdir = os.path.join(self.temp.name, 'runs', 'signal-terminated')
        os.makedirs(rdir)
        fake = mock.Mock(return_value=(-9, None, {'usage': {}}, 'executor killed'))
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=[str(BIN), 'gocc']), \
             mock.patch.dict(xs.EXECUTORS, {'pi': fake}):
            attempts, final, error = xs.run_executor_with_rate_limit(
                'pi', {'backend': 'pi', 'model': 'MiniMax-M3.1-Flash', 'effort': None}, {}, 'task',
                self.temp.name, rdir, xs.NOW() + 30, xs.load_policy())
        self.assertEqual(error, 'exec_error')
        self.assertIsNone(final)
        self.assertEqual(attempts[0]['rc'], -9)
        self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
        self.assertTrue(xs.release_fleet_slot(rdir))

    def test_normally_exited_one_shot_parent_with_child_retains_reservation(self):
        """A non-zero parent exit must not release while its process group lives."""
        rdir = os.path.join(self.temp.name, 'runs', 'normal-exit-with-child')
        os.makedirs(rdir)
        runner = pathlib.Path(self.temp.name, 'spawn-child.py')
        runner.write_text('import subprocess, sys, time\nsubprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\nsys.exit(17)\n')

        def fake_executor(_decision, _state, _prompt, wd, _rdir, timeout, events):
            return xs.run_proc([sys.executable, str(runner)], wd, {}, timeout, events), None, {}, 'executor exited'

        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=[str(BIN), 'gocc']), \
             mock.patch.dict(xs.EXECUTORS, {'pi': fake_executor}):
            attempts, _final, error = xs.run_executor_with_rate_limit(
                'pi', {'backend': 'pi', 'model': 'MiniMax-M3.1-Flash', 'effort': None}, {}, 'task',
                self.temp.name, rdir, xs.NOW() + 30, xs.load_policy())
        self.assertEqual(error, 'exec_error')
        self.assertEqual(attempts[0]['rc'], 17)
        self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
        pgid = json.loads(pathlib.Path(rdir, 'status.json').read_text())['executor_pgid']
        stop_executor_group(pgid)
        self.assertTrue(xs.release_fleet_slot(rdir))

    def test_detached_descendant_retains_then_ttl_reaper_stops_recorded_group(self):
        """A descendant which creates its own session remains in the execution boundary."""
        runs = pathlib.Path(self.temp.name, 'runs')
        rdir = runs / 'detached-descendant'
        rdir.mkdir(parents=True)
        script = pathlib.Path(self.temp.name, 'detached-child.py')
        marker = pathlib.Path(self.temp.name, 'detached-child.pid')
        script.write_text(
            'import os, subprocess, sys, time\n'
            'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],start_new_session=True)\n'
            f'open({str(marker)!r},"w").write(str(child.pid))\n'
            'time.sleep(.4)\n'
            'sys.exit(17)\n')
        policy = {'concurrency': {'total': 8}, 'fleet': {'recovery_ttl_seconds': 0.1}}

        def executor(_decision, _state, _prompt, wd, _rdir, timeout, events):
            return xs.run_proc([sys.executable, str(script)], wd, {}, timeout, events), None, {}, 'executor exited'

        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=[str(BIN), 'gocc']), \
             mock.patch.dict(xs.EXECUTORS, {'pi': executor}):
            attempts, _final, error = xs.run_executor_with_rate_limit(
                'pi', {'backend': 'pi', 'model': 'MiniMax-M3.1-Flash'}, {}, 'task', self.temp.name,
                str(rdir), xs.NOW() + 30, policy)
        try:
            self.assertEqual(error, 'exec_error')
            self.assertEqual(attempts[0]['rc'], 17)
            child = int(marker.read_text())
            self.assertTrue(xs.pid_alive(child))
            status = json.loads((rdir / 'status.json').read_text())
            self.assertTrue(status['executor_descendants'])
            self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
            status['runner_pid'] = 999999999
            status['fleet_host'], status['fleet_reserved_at'] = 'gocc', 0.0
            (rdir / 'status.json').write_text(json.dumps(status))
            with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
                 mock.patch.object(xs, 'RUNS', str(runs)), mock.patch.object(xs, 'NOW', return_value=1.0):
                self.assertEqual(xs.recover_expired_fleet_slots([str(BIN), 'gocc'], policy), ['detached-descendant'])
            self.assertEqual(self.call('gocc', op='status')['reservations'], [])
        finally:
            if marker.exists():
                try:
                    os.kill(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_exited_orphan_does_not_keep_a_capacity_slot(self):
        """An orphan which exited and which nobody will reap holds no capacity.

        A child of this process is reaped as soon as it is waited for, so its
        slot stays reserved until then.  An orphan which already exited holds
        no resources for ever: a slot it keeps would be a ghost reservation.
        """
        script = pathlib.Path(self.temp.name, 'orphan.py')
        script.write_text('import subprocess, sys\n'
                          'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],'
                          ' start_new_session=True)\n'
                          'print(child.pid, flush=True)\n')
        process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
        orphan_pid = int(process.stdout.readline().strip())
        process.stdout.close()
        process.wait()
        pgid = os.getpgid(orphan_pid)
        os.kill(orphan_pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not xs.process_exited(orphan_pid):
            time.sleep(0.05)
        self.assertFalse(xs.recorded_process_working({'pid': orphan_pid, 'pgid': pgid}))
        # A child of this process keeps its slot until it is reaped.
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(child.wait)
        child.kill()
        self.wait_until_exited(child.pid)
        self.assertTrue(xs.recorded_process_working(
            {'pid': child.pid, 'pgid': os.getpgid(child.pid)}))

    def wait_until_exited(self, pid, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not xs.process_exited(pid):
            time.sleep(0.05)

    def test_hermes_one_shot_parent_with_child_retains_reservation(self):
        """Hermes must publish its LaneChild group before one-shot release checks."""
        rdir = pathlib.Path(self.temp.name, 'runs', 'hermes-normal-exit-with-child')
        rdir.mkdir(parents=True)
        marker = pathlib.Path(self.temp.name, 'hermes-child.pid')
        fixture = HERE / 'fixtures' / 'fake_cursor_acp.py'
        fixture.chmod(0o755)
        policy = {'concurrency': {'total': 8},
                  'hermes': {'binary': str(fixture), 'provider': 'minimax-cn'}}
        env = {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name,
               'FAKE_CURSOR_CHILD_PID': str(marker), 'FAKE_CHILD_DEVNULL': '1'}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=[str(BIN), 'gocc']), \
             mock.patch.object(xs, 'load_policy', return_value=policy), \
             mock.patch.object(xs, 'minimax_key_from_pi', return_value=None), \
             mock.patch.dict(xs.EXECUTORS, {'pi': xs.exec_hermes}):
            attempts, _final, error = xs.run_executor_with_rate_limit(
                'pi', {'backend': 'pi', 'model': 'composer-2.5', 'provider': 'minimax-cn'}, {},
                'LANE_TEST_EXIT_WITH_CHILD', self.temp.name, str(rdir), xs.NOW() + 30, policy)
        try:
            self.assertEqual(error, 'exec_error')
            self.assertEqual(attempts[0]['rc'], 1)
            self.assertTrue(marker.exists(), attempts)
            self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
            pgid = json.loads((rdir / 'status.json').read_text())['executor_pgid']
            stop_executor_group(pgid)
            self.assertTrue(xs.release_fleet_slot(str(rdir)))
        finally:
            if marker.exists():
                try:
                    os.kill(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_hermes_init_exit_with_child_retains_reservation(self):
        """Record Hermes' group before an ACP initialization failure can raise."""
        rdir = pathlib.Path(self.temp.name, 'runs', 'hermes-init-exit-with-child')
        rdir.mkdir(parents=True)
        marker = pathlib.Path(self.temp.name, 'hermes-init-child.pid')
        fixture = HERE / 'fixtures' / 'fake_hermes_early_exit.py'
        fixture.chmod(0o755)
        policy = {'concurrency': {'total': 8},
                  'hermes': {'binary': str(fixture), 'provider': 'minimax-cn'}}
        env = {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name,
               'FAKE_CURSOR_CHILD_PID': str(marker)}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=[str(BIN), 'gocc']), \
             mock.patch.object(xs, 'load_policy', return_value=policy), \
             mock.patch.object(xs, 'minimax_key_from_pi', return_value=None), \
             mock.patch.dict(xs.EXECUTORS, {'pi': xs.exec_hermes}):
            attempts, _final, error = xs.run_executor_with_rate_limit(
                'pi', {'backend': 'pi', 'model': 'composer-2.5', 'provider': 'minimax-cn'}, {},
                'task', self.temp.name, str(rdir), xs.NOW() + 30, policy)
        try:
            self.assertEqual(error, 'exec_error')
            self.assertEqual(attempts[0]['rc'], 1)
            self.assertTrue(marker.exists(), attempts)
            self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
            pgid = json.loads((rdir / 'status.json').read_text())['executor_pgid']
            stop_executor_group(pgid)
            self.assertTrue(xs.release_fleet_slot(str(rdir)))
        finally:
            if marker.exists():
                try:
                    os.kill(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

if __name__ == '__main__':
    unittest.main()


class FleetTopologyTests(unittest.TestCase):
    """topology.json lets new executor hosts join with their own limits."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=self.temp.name)

    def call(self, host, **payload):
        p = subprocess.run([str(BIN), host], input=json.dumps(payload), text=True,
                           capture_output=True, env=self.env, timeout=15)
        return p.returncode, (json.loads(p.stdout) if p.stdout.strip().startswith('{') else p.stderr)

    def test_topology_hosts_and_limits(self):
        pathlib.Path(self.temp.name, 'topology.json').write_text(json.dumps(
            {'hosts': ['ib1', 'gocc'], 'limits': {'total': 10, 'host:ib1': 1, 'pi': 4, 'minimax-cn': 4}}))
        rc, out = self.call('ib1', op='acquire', token='a' * 32, run_id='r1', backend='pi', provider='minimax-cn')
        self.assertTrue(out['admitted'])
        rc, out = self.call('ib1', op='acquire', token='b' * 32, run_id='r2', backend='codex', provider=None)
        self.assertEqual(out['blocked'], ['host:ib1'])
        rc, out = self.call('gores1', op='status')
        self.assertNotEqual(rc, 0)

    def test_malformed_topology_fails_closed(self):
        pathlib.Path(self.temp.name, 'topology.json').write_text('{"hosts": "ib1"}')
        rc, out = self.call('gocc', op='status')
        self.assertNotEqual(rc, 0)
        self.assertFalse(out['ok'])

    def test_topology_missing_limits_fails_closed_without_traceback(self):
        """A capacity topology without its required field must fail closed readably."""
        pathlib.Path(self.temp.name, 'topology.json').write_text(json.dumps({'hosts': ['gocc']}))
        process = subprocess.run([str(BIN), 'gocc'], input=json.dumps({'op': 'status'}), text=True,
                                 capture_output=True, env=self.env, timeout=15)
        self.assertNotEqual(process.returncode, 0)
        answer = json.loads(process.stdout)
        self.assertFalse(answer['ok'])
        self.assertIn('invalid fleet topology', answer['error'])
        self.assertIn('limits', answer['error'])
        self.assertNotIn('Traceback', process.stdout)
        self.assertNotIn('Traceback', process.stderr)

    def test_valid_nonempty_topology_success_path(self):
        """A valid non-empty topology successfully admits and limits registered hosts."""
        topology = {
            'hosts': ['ib1', 'ib2', 'gocc'],
            'limits': {'total': 5, 'host:ib1': 1, 'host:ib2': 2, 'host:gocc': 2,
                       'pi': 4, 'minimax-cn': 4, 'codex': 2},
        }
        pathlib.Path(self.temp.name, 'topology.json').write_text(json.dumps(topology))
        # First acquire on ib1 succeeds.
        rc, out = self.call('ib1', op='acquire', token='1' * 32, run_id='run-1',
                            backend='pi', provider='minimax-cn')
        self.assertEqual(rc, 0)
        self.assertTrue(out['ok'])
        self.assertTrue(out['admitted'])

        # Second acquire on ib1 is blocked by host cap.
        rc, out = self.call('ib1', op='acquire', token='2' * 32, run_id='run-2',
                            backend='pi', provider='minimax-cn')
        self.assertEqual(rc, 0)
        self.assertTrue(out['ok'])
        self.assertFalse(out['admitted'])
        self.assertEqual(out['blocked'], ['host:ib1'])

        # Acquire on ib2 succeeds under its separate host cap.
        rc, out = self.call('ib2', op='acquire', token='3' * 32, run_id='run-3',
                            backend='codex', provider=None)
        self.assertEqual(rc, 0)
        self.assertTrue(out['ok'])
        self.assertTrue(out['admitted'])

        # Status check shows active reservations.
        rc, out = self.call('gocc', op='status')
        self.assertEqual(rc, 0)
        self.assertTrue(out['ok'])
        self.assertEqual(len(out['reservations']), 2)

        # Release frees the capacity slot.
        rc, out = self.call('ib1', op='release', token='1' * 32, run_id='run-1')
        self.assertEqual(rc, 0)
        self.assertTrue(out['released'])

    def test_topology_referencing_unknown_host_fails_closed_and_names_host(self):
        """A topology referencing an unknown host in limits must fail closed and name the host."""
        topology = {
            'hosts': ['ib1', 'gocc'],
            'limits': {'total': 8, 'host:ib1': 2, 'host:unknownhost': 2},
        }
        pathlib.Path(self.temp.name, 'topology.json').write_text(json.dumps(topology))
        process = subprocess.run([str(BIN), 'ib1'], input=json.dumps({'op': 'status'}), text=True,
                                 capture_output=True, env=self.env, timeout=15)
        self.assertNotEqual(process.returncode, 0)
        answer = json.loads(process.stdout)
        self.assertFalse(answer['ok'])
        self.assertIn('invalid fleet topology', answer['error'])
        self.assertIn('unknownhost', answer['error'])
        self.assertNotIn('Traceback', process.stdout)
        self.assertNotIn('Traceback', process.stderr)

    def test_topology_unknown_host_zero_limit_never_silently_continues(self):
        """An unknown host in limits must not be silently treated as 0 capacity."""
        topology = {
            'hosts': ['ib1', 'gocc'],
            'limits': {'total': 8, 'host:ib1': 2, 'host:ghosthost': 0},
        }
        pathlib.Path(self.temp.name, 'topology.json').write_text(json.dumps(topology))
        process = subprocess.run(
            [str(BIN), 'ib1'],
            input=json.dumps({'op': 'acquire', 'token': 'a' * 32, 'run_id': 'r-silent',
                              'backend': 'pi', 'provider': 'minimax-cn'}),
            text=True, capture_output=True, env=self.env, timeout=15)
        self.assertNotEqual(process.returncode, 0)
        answer = json.loads(process.stdout)
        self.assertFalse(answer['ok'])
        self.assertIn('invalid fleet topology', answer['error'])
        self.assertIn('ghosthost', answer['error'])
        self.assertNotIn('Traceback', process.stdout)
        self.assertNotIn('Traceback', process.stderr)

    def test_topology_bare_unknown_host_in_limits_fails_closed_naming_host(self):
        """Unprefixed unknown host limit keys fail closed and identify the key."""
        topology = {
            'hosts': ['ib1', 'gocc'],
            'limits': {'total': 8, 'phantom': 2},
        }
        pathlib.Path(self.temp.name, 'topology.json').write_text(json.dumps(topology))
        process = subprocess.run([str(BIN), 'gocc'], input=json.dumps({'op': 'status'}), text=True,
                                 capture_output=True, env=self.env, timeout=15)
        self.assertNotEqual(process.returncode, 0)
        answer = json.loads(process.stdout)
        self.assertFalse(answer['ok'])
        self.assertIn('invalid fleet topology', answer['error'])
        self.assertIn('phantom', answer['error'])
        self.assertNotIn('Traceback', process.stdout)
        self.assertNotIn('Traceback', process.stderr)


# A run of the client which holds a slot.  The parent kills it to show that the
# lease of a crashed run expires and its slot returns to the pool.
LEASE_RUNNER = """
import importlib.machinery, importlib.util, json, os, sys, time
loader = importlib.machinery.SourceFileLoader("openswarm", sys.argv[1])
spec = importlib.util.spec_from_loader("openswarm", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
policy = json.loads(sys.argv[2])
rdir = os.path.join(os.environ["OPENSWARM_HOME"], "runs", "in-flight")
admitted = mod.reserve_provider_slot(rdir, "codex", None, policy, keep_waiting=lambda: True)
print(json.dumps({"admitted": bool(admitted)}), flush=True)
time.sleep(300)
"""

# A run of the client which holds a slot and works with an executor of its own.
# The parent breaks the connection to the coordinator to show that the run stops
# its executor before the coordinator may hand the slot to another run.
LEASE_RUNNER_WITH_EXECUTOR = """
import importlib.machinery, importlib.util, json, os, subprocess, sys, time
loader = importlib.machinery.SourceFileLoader("openswarm", sys.argv[1])
spec = importlib.util.spec_from_loader("openswarm", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
policy = json.loads(sys.argv[2])
rdir = os.path.join(os.environ["OPENSWARM_HOME"], "runs", "in-flight")
admitted = mod.reserve_provider_slot(rdir, "codex", None, policy, keep_waiting=lambda: True)
executor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                            start_new_session=True)
mod.set_status(rdir, executor_pid=executor.pid, executor_pgid=os.getpgid(executor.pid),
                executor_identity=mod.process_identity(executor.pid))
print(json.dumps({"admitted": bool(admitted), "executor_pid": executor.pid}), flush=True)
time.sleep(300)
"""

# A fleet guard which forwards every request to the coordinator while the
# connection is up and refuses every request while the outage file is there:
# it reproduces a coordinator which cannot be reached any more.
GUARD_WITH_OUTAGE = '''#!/usr/bin/env python3
import json, os, subprocess, sys

outage = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outage")
if os.path.exists(outage):
    print(json.dumps({"ok": False, "error": "coordinator unreachable"}))
    sys.exit(1)
request = json.load(sys.stdin)
process = subprocess.run([sys.executable, sys.argv[1], sys.argv[2]], input=json.dumps(request),
                          text=True, capture_output=True)
sys.stdout.write(process.stdout)
sys.exit(process.returncode)
'''


class FleetLeaseTests(unittest.TestCase):
    """A reservation is a lease: it is renewed while the run lives and reclaimed when it expires."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=self.temp.name)

    def call(self, authenticated_host, env=None, **payload):
        p = subprocess.run([str(BIN), authenticated_host], input=json.dumps(payload), text=True,
                           capture_output=True, env=env or self.env, timeout=15)
        return json.loads(p.stdout)

    def wait_for(self, predicate, description, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        self.fail(f'timed out waiting for {description}')

    def coordinator_rows(self):
        """The reservations of the coordinator database, read without a request."""
        connection = sqlite3.connect(str(pathlib.Path(self.temp.name, 'capacity.sqlite')))
        try:
            return connection.execute('SELECT token,run_id,expires_at FROM reservations').fetchall()
        finally:
            connection.close()

    def audit_rows(self):
        """The audit trail of the coordinator database, read without a request."""
        connection = sqlite3.connect(str(pathlib.Path(self.temp.name, 'capacity.sqlite')))
        try:
            return [json.loads(row[0]) for row in
                    connection.execute('SELECT entry FROM lease_audit ORDER BY id').fetchall()]
        finally:
            connection.close()

    def legacy_database(self, created):
        """A coordinator database with one reservation without lease fields."""
        connection = sqlite3.connect(str(pathlib.Path(self.temp.name, 'capacity.sqlite')))
        connection.execute('CREATE TABLE reservations (token TEXT PRIMARY KEY, host TEXT NOT NULL,'
                           ' run_id TEXT NOT NULL, backend TEXT NOT NULL, provider TEXT,'
                           ' created REAL NOT NULL)')
        connection.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?)',
                           ('e' * 32, 'gocc', 'legacy', 'codex', None, created))
        connection.commit()
        connection.close()

    def test_expired_lease_returns_the_slot_to_the_pool(self):
        """An expired lease is reclaimed by the coordinator and leaves an audit record."""
        token = 'a' * 32
        admitted = self.call('gocc', op='acquire', token=token, run_id='ghost',
                             backend='codex', provider=None, ttl_seconds=2)
        self.assertTrue(admitted['admitted'])
        self.assertGreater(admitted['expires_at'], time.time())
        # While the lease lives, the slot is taken.
        blocked = self.call('gocc', op='acquire', token='b' * 32, run_id='next',
                            backend='codex', provider=None)
        self.assertFalse(blocked['admitted'])
        self.assertIn('codex', blocked['blocked'])
        time.sleep(2.2)
        # The expired lease is reclaimed and the slot serves the next run.
        answer = self.call('gocc', op='acquire', token='b' * 32, run_id='next',
                           backend='codex', provider=None)
        self.assertTrue(answer['admitted'])
        self.assertEqual([item['run_id'] for item in answer['expired']], ['ghost'])
        audit = (pathlib.Path(self.temp.name) / 'lease-audit.jsonl').read_text().splitlines()
        self.assertTrue(any('lease-expired' in line and 'ghost' in line for line in audit))

    def test_renew_extends_the_lease_of_a_live_run(self):
        """A live run renews its lease: the slot outlives the first expiry."""
        token = 'c' * 32
        admitted = self.call('gocc', op='acquire', token=token, run_id='live',
                             backend='codex', provider=None, ttl_seconds=2)
        self.assertTrue(admitted['admitted'])
        renewed = self.call('gocc', op='renew', token=token, run_id='live', ttl_seconds=30)
        self.assertTrue(renewed['renewed'])
        self.assertGreater(renewed['expires_at'], admitted['expires_at'])
        time.sleep(2.2)
        self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
        # Only the owner of a reservation may renew it.
        self.assertFalse(self.call('gores1', op='renew', token=token, run_id='live')['ok'])

    def test_renewal_does_not_resurrect_a_reclaimed_lease(self):
        """A renewal after the expiry does not bring the slot back."""
        token = 'd' * 32
        self.assertTrue(self.call('gocc', op='acquire', token=token, run_id='gone',
                                  backend='codex', provider=None, ttl_seconds=1)['admitted'])
        time.sleep(1.2)
        self.call('gocc', op='status')
        answer = self.call('gocc', op='renew', token=token, run_id='gone', ttl_seconds=30)
        self.assertFalse(answer['renewed'])
        self.assertEqual(answer['reason'], 'unknown reservation')
        self.assertEqual(len(self.call('gocc', op='status')['reservations']), 0)

    def test_legacy_reservation_without_lease_fields_is_migrated(self):
        """An old row without lease fields gets a fallback expiry instead of living forever."""
        self.legacy_database(time.time())
        env = dict(self.env, OPENSWARM_FLEET_LEASE_LEGACY_TTL_SECONDS='1')
        row = self.call('gocc', op='status', env=env)['reservations'][0]
        self.assertIsNotNone(row['expires_at'])
        self.assertGreater(row['expires_at'], time.time())
        # The migrated lease expires like every other lease: the slot returns.
        time.sleep(1.2)
        self.assertEqual(len(self.call('gocc', op='status', env=env)['reservations']), 0)

    def test_legacy_reservation_keeps_its_slot_during_the_migration_window(self):
        """A live old run keeps its slot while the fleet rolls out the lease."""
        self.legacy_database(time.time() - 30)
        row = self.call('gocc', op='status')['reservations'][0]
        self.assertIsNotNone(row['expires_at'])
        # The migration window covers the longest run a client may hold.
        self.assertGreater(row['expires_at'], time.time() + 1800)
        self.assertEqual(row['lease_ttl'], 3600.0)

    def test_legacy_ghost_is_reclaimed_with_the_migration_window(self):
        """An old row of a run which is long gone is reclaimed with the same rule."""
        self.legacy_database(time.time() - 7200)
        self.assertEqual(self.call('gocc', op='status')['reservations'], [])
        audit = self.call('gocc', op='audit')['audit']
        self.assertTrue(any(item['event'] == 'lease-expired' and item['run_id'] == 'legacy'
                            for item in audit))

    def test_mixed_legacy_and_leased_reservations_are_counted_together(self):
        """An old row and a new lease hold their slots together and are reclaimed apart."""
        self.legacy_database(time.time() - 30)
        self.assertTrue(self.call('gocc', op='acquire', token='4' * 32, run_id='new',
                                  backend='pi', provider=None, ttl_seconds=1)['admitted'])
        rows = self.call('gocc', op='status')['reservations']
        self.assertEqual(sorted(row['run_id'] for row in rows), ['legacy', 'new'])
        # The old row still holds its slot: the cap sees both reservations.
        blocked = self.call('gocc', op='acquire', token='5' * 32, run_id='next',
                            backend='codex', provider=None)
        self.assertFalse(blocked['admitted'])
        self.assertIn('codex', blocked['blocked'])
        # The new lease is reclaimed, the protected old row stays.
        time.sleep(1.2)
        self.call('gocc', op='status')
        rows = self.call('gocc', op='status')['reservations']
        self.assertEqual([row['run_id'] for row in rows], ['legacy'])

    def test_renewal_does_not_shorten_the_migration_window(self):
        """A new client which adopts an old reservation does not cut its window short."""
        self.legacy_database(time.time() - 30)
        before = self.call('gocc', op='status')['reservations'][0]['expires_at']
        renewed = self.call('gocc', op='renew', token='e' * 32, run_id='legacy', ttl_seconds=60)
        self.assertTrue(renewed['renewed'])
        self.assertGreaterEqual(renewed['expires_at'], before)

    def test_sweep_loop_reclaims_an_expired_lease_without_any_request(self):
        """The coordinator frees the slot of a crashed run on its own."""
        token = '3' * 32
        self.assertTrue(self.call('gocc', op='acquire', token=token, run_id='ghost',
                                  backend='codex', provider=None, ttl_seconds=1)['admitted'])
        env = dict(self.env, OPENSWARM_FLEET_CAPACITY_SWEEP_INTERVAL='0.2')
        sweeper = subprocess.Popen([str(BIN), '--sweep-loop', 'gocc'], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # No request reaches the coordinator: the sweep alone frees the slot.
            self.wait_for(lambda: not self.coordinator_rows(), 'the sweep to reclaim the lease')
        finally:
            sweeper.terminate()
            sweeper.wait(timeout=10)
        audit = self.call('gocc', op='audit')['audit']
        self.assertTrue(any(item['event'] == 'lease-expired' and item['run_id'] == 'ghost'
                            for item in audit))
        lines = (pathlib.Path(self.temp.name) / 'lease-audit.jsonl').read_text().splitlines()
        self.assertTrue(any('lease-expired' in line and 'ghost' in line for line in lines))

    def test_the_coordinator_sweeps_the_expired_leases_without_any_request(self):
        """The deployment of the coordinator frees the slot of a crashed run on its own.

        The sweeper belongs to the coordinator: the request which took the slot
        starts it, so the reclaim of an expired lease does not wait for another
        request and does not depend on an operator.
        """
        env = dict(self.env, OPENSWARM_FLEET_CAPACITY_SWEEP_INTERVAL='0.2')
        token = '5' * 32
        self.assertTrue(self.call('gocc', op='acquire', token=token, run_id='ghost',
                                  backend='codex', provider=None, ttl_seconds=1, env=env)['admitted'])
        # No request reaches the coordinator: the sweeper of the deployment frees
        # the slot and writes the audit trail of the reclaim.
        self.wait_for(lambda: not self.coordinator_rows(), 'the sweeper to reclaim the lease')
        audit = self.audit_rows()
        self.assertTrue(any(item['event'] == 'lease-expired' and item['run_id'] == 'ghost'
                            for item in audit))
        lines = (pathlib.Path(self.temp.name) / 'lease-audit.jsonl').read_text().splitlines()
        self.assertTrue(any('lease-expired' in line and 'ghost' in line for line in lines))

    def test_the_sweeper_belongs_to_the_deployment_of_the_coordinator(self):
        """The coordinator starts one sweeper of the expired leases by itself."""
        answer = self.call('gocc', op='status')
        self.assertTrue(answer['sweeper']['running'])
        # The sweeper writes its pid next to the coordinator database.
        self.wait_for(lambda: self.call('gocc', op='status')['sweeper'].get('pid'),
                      'the pid of the sweeper')
        pid = self.call('gocc', op='status')['sweeper']['pid']
        self.assertTrue(xs.pid_alive(pid))
        # A later request does not start a second sweeper of the same coordinator.
        self.assertEqual(self.call('gocc', op='status')['sweeper']['pid'], pid)

    def test_a_disconnected_client_stops_its_executor_before_a_new_run_takes_the_slot(self):
        """A run which loses the coordinator stops before the slot is given away.

        The coordinator reclaims the lease of the lost run and hands its slot to
        another run.  The old executor has to be gone before that, or the real
        concurrency of the fleet exceeds its cap.
        """
        root = pathlib.Path(self.temp.name)
        home = root / 'home'
        home.mkdir()
        guard = root / 'guard.py'
        guard.write_text(GUARD_WITH_OUTAGE)
        guard.chmod(0o755)
        config = root / 'guard.json'
        config.write_text(json.dumps({'command': [sys.executable, str(guard), str(BIN), 'gocc']}))
        env = dict(os.environ, OPENSWARM_HOME=str(home),
                   OPENSWARM_FLEET_GUARD_CONFIG=str(config),
                   OPENSWARM_FLEET_CAPACITY_DIR=str(root / 'capacity'),
                   OPENSWARM_FLEET_CAPACITY_SWEEP_INTERVAL='0.2')
        policy = {'concurrency': {'total': 8},
                  'fleet': {'lease': {'ttl_seconds': 4, 'renew_interval_seconds': 0.5}}}
        status_path = home / 'runs' / 'in-flight' / 'status.json'
        capacity = root / 'capacity' / 'capacity.sqlite'

        def rows():
            """The reservations of the coordinator, read without a request."""
            connection = sqlite3.connect(str(capacity))
            try:
                return connection.execute('SELECT token,run_id,expires_at FROM reservations').fetchall()
            finally:
                connection.close()

        log = root / 'runner.log'
        with log.open('w') as stream:
            child = subprocess.Popen([sys.executable, '-c', LEASE_RUNNER_WITH_EXECUTOR,
                                      str(CLI), json.dumps(policy)],
                                     env=env, stdout=stream, stderr=stream)
        try:
            self.wait_for(lambda: self.run_status(status_path).get('executor_pid'),
                          'the run to take its slot and to start its executor')
            status = self.run_status(status_path)
            self.assertEqual(len(rows()), 1)
            expires_at = rows()[0][2]
            # The coordinator becomes unreachable: every renewal fails.
            (root / 'outage').write_text('down')
            # The run stops its executor before the lease may expire, so the slot
            # is free before the coordinator hands it to another run.
            self.wait_for(lambda: self.run_status(status_path).get('fleet_lease_state') == 'lost',
                          'the run to lose its lease')
            self.assertLess(time.time(), expires_at)
            self.assertFalse(xs.process_group_has_live_member(status['executor_pgid']))
            # The lease of the lost run expires and the next run takes the slot.
            self.wait_for(lambda: not rows(), 'the lease of the lost run to expire')
            answer = self.call('gocc', op='acquire', token='e' * 32, run_id='next',
                               backend='codex', provider=None, env=env)
            self.assertTrue(answer['admitted'])
            # The old executor is gone: the two runs never share the slot.
            self.assertFalse(xs.process_group_has_live_member(status['executor_pgid']))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    @staticmethod
    def run_status(path):
        """The status of a run, read while the run writes it."""
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def test_the_heartbeat_counts_the_lease_from_the_start_of_the_request(self):
        """A slow answer does not push the deadline of the lease beyond its TTL.

        The coordinator computes the expiry from its own clock when it processes
        the request, so the latency of the answer may only make the real expiry
        earlier.  The client counts the lease from the start of the request.
        """
        rdir = pathlib.Path(self.temp.name, 'runs', 'slow')
        rdir.mkdir(parents=True)
        (rdir / 'status.json').write_text(json.dumps({'id': 'slow', 'state': 'running'}))
        calls = []

        def slow_call(_command, payload, timeout=15):
            calls.append((time.monotonic(), timeout))
            time.sleep(0.5)
            return {'renewed': True, 'expires_at': time.time() + 30}

        policy = {'fleet': {'lease': {'ttl_seconds': 3, 'renew_interval_seconds': 0.2}}}
        with mock.patch.object(xs, 'fleet_guard_call', side_effect=slow_call):
            heartbeat = xs.start_fleet_lease_heartbeat(
                str(rdir), ['guard'], {'run_id': 'slow', 'token': 'c' * 32, 'ttl_seconds': 3}, policy)
            self.addCleanup(heartbeat.stop)
            initial = heartbeat.deadline
            self.wait_for(lambda: heartbeat.deadline > initial, 'the heartbeat to renew the lease')
            self.wait_for(lambda: len(calls) >= 1, 'the heartbeat to renew the lease')
            # The deadline is the start of the request plus the TTL: the answer
            # latency is not part of it.
            self.assertGreater(heartbeat.deadline, calls[0][0] + 2.5)
            self.assertLessEqual(heartbeat.deadline, calls[-1][0] + 3)
            # The renewal call never outlives the deadline of the lease.
            self.assertLessEqual(calls[-1][1], 3)

    def test_the_renewal_call_never_outlives_the_deadline_of_the_lease(self):
        """A renewal which hangs cannot keep the run alive past its deadline."""
        rdir = pathlib.Path(self.temp.name, 'runs', 'hanging')
        rdir.mkdir(parents=True)
        (rdir / 'status.json').write_text(json.dumps({'id': 'hanging', 'state': 'running'}))
        calls = []

        def hanging_call(_command, payload, timeout=15):
            calls.append(timeout)
            time.sleep(timeout)
            return {'renewed': True, 'expires_at': time.time() + 30}

        policy = {'fleet': {'lease': {'ttl_seconds': 2, 'renew_interval_seconds': 0.2}}}
        with mock.patch.object(xs, 'fleet_guard_call', side_effect=hanging_call):
            heartbeat = xs.start_fleet_lease_heartbeat(
                str(rdir), ['guard'], {'run_id': 'hanging', 'token': 'd' * 32, 'ttl_seconds': 2}, policy)
            self.addCleanup(heartbeat.stop)
            self.wait_for(lambda: len(calls) >= 1, 'the heartbeat to renew the lease')
            # The timeout of the call is shorter than the lease: a hanging
            # coordinator cannot push the deadline of the lease away.
            self.assertLessEqual(calls[0], 2)

    def test_audit_trail_survives_a_failing_audit_file(self):
        """A mirror which cannot be written does not lose the audit record."""
        (pathlib.Path(self.temp.name) / 'lease-audit.jsonl').mkdir()
        self.assertTrue(self.call('gocc', op='acquire', token='6' * 32, run_id='audited',
                                  backend='codex', provider=None, ttl_seconds=60)['admitted'])
        released = self.call('gocc', op='release', token='6' * 32, run_id='audited')
        self.assertTrue(released['released'])
        # The failing mirror is reported instead of ignored.
        self.assertIn('audit_error', released)
        # The database keeps the authoritative trail of the committed change.
        audit = self.call('gocc', op='audit')['audit']
        self.assertTrue(any(item['event'] == 'released' and item['run_id'] == 'audited'
                            for item in audit))

    def test_rolled_back_transaction_leaves_no_audit_record(self):
        """A failing request rolls its reclaim back: no slot is freed, no record is left."""
        token = '7' * 32
        self.assertTrue(self.call('gocc', op='acquire', token=token, run_id='rollback',
                                  backend='codex', provider=None, ttl_seconds=1)['admitted'])
        time.sleep(1.2)
        # The request fails after the reclaim: the transaction is rolled back.
        answer = self.call('gocc', op='acquire', token='8' * 32, run_id='invalid',
                            backend='not-a-backend', provider=None)
        self.assertFalse(answer['ok'])
        # The expired reservation is still there and no record claims a reclaim.
        self.assertEqual([row[1] for row in self.coordinator_rows()], ['rollback'])
        self.assertFalse(any(item['event'] == 'lease-expired' for item in self.audit_rows()))
        mirror = pathlib.Path(self.temp.name) / 'lease-audit.jsonl'
        self.assertFalse(mirror.exists() and 'lease-expired' in mirror.read_text())

    def test_release_and_sweep_free_a_slot_once(self):
        """The explicit release and the sweep of the coordinator never free a slot twice."""
        token = '8' * 32
        self.assertTrue(self.call('gocc', op='acquire', token=token, run_id='race',
                                  backend='codex', provider=None, ttl_seconds=1)['admitted'])
        time.sleep(1.2)
        # The lease expired: the reclaim of the coordinator frees the slot and
        # the release of the owner finds nothing left to free.
        answer = self.call('gocc', op='release', token=token, run_id='race')
        self.assertFalse(answer['released'])
        self.assertEqual(self.coordinator_rows(), [])
        events = [item['event'] for item in self.audit_rows() if item['run_id'] == 'race']
        self.assertEqual(events, ['lease-expired'])

    def test_lost_lease_stops_the_executor_of_the_run(self):
        """A run whose lease was reclaimed stops its executor instead of running on."""
        rdir = pathlib.Path(self.temp.name, 'runs', 'lost')
        rdir.mkdir(parents=True)
        executor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                    start_new_session=True)
        self.addCleanup(executor.wait)
        (rdir / 'status.json').write_text(json.dumps({
            'id': 'lost', 'state': 'running', 'executor_pid': executor.pid,
            'executor_pgid': os.getpgid(executor.pid),
            'executor_identity': xs.process_identity(executor.pid)}))
        policy = {'fleet': {'lease': {'ttl_seconds': 3, 'renew_interval_seconds': 0.2}}}
        with mock.patch.object(xs, 'fleet_guard_call',
                               return_value={'renewed': False, 'reason': 'unknown reservation'}):
            heartbeat = xs.start_fleet_lease_heartbeat(
                str(rdir), ['guard'], {'run_id': 'lost', 'token': 'a' * 32, 'ttl_seconds': 3}, policy)
            self.addCleanup(heartbeat.stop)
            self.wait_for(lambda: not heartbeat.thread.is_alive(), 'the heartbeat to notice the lost lease')
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['fleet_lease_state'], 'lost')
        # The executor is stopped: it must not use capacity it does not own.
        self.wait_for(lambda: not xs.process_running(executor.pid), 'the executor to be stopped')

    def test_unreachable_coordinator_beyond_the_ttl_stops_the_executor(self):
        """A network outage longer than the lease stops the run instead of overbooking."""
        rdir = pathlib.Path(self.temp.name, 'runs', 'offline')
        rdir.mkdir(parents=True)
        executor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                    start_new_session=True)
        self.addCleanup(executor.wait)
        (rdir / 'status.json').write_text(json.dumps({
            'id': 'offline', 'state': 'running', 'executor_pid': executor.pid,
            'executor_pgid': os.getpgid(executor.pid),
            'executor_identity': xs.process_identity(executor.pid)}))
        policy = {'fleet': {'lease': {'ttl_seconds': 0.6, 'renew_interval_seconds': 0.1}}}
        with mock.patch.object(xs, 'fleet_guard_call', side_effect=RuntimeError('coordinator unavailable')):
            heartbeat = xs.start_fleet_lease_heartbeat(
                str(rdir), ['guard'], {'run_id': 'offline', 'token': 'b' * 32, 'ttl_seconds': 0.6}, policy)
            self.addCleanup(heartbeat.stop)
            self.wait_for(lambda: not heartbeat.thread.is_alive(), 'the heartbeat to stop the run')
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['fleet_lease_state'], 'lost')
        self.assertIn('coordinator unreachable', status['fleet_lease_error'])
        self.wait_for(lambda: not xs.process_running(executor.pid), 'the executor to be stopped')

    def test_every_termination_path_leaves_the_slot_to_the_lease(self):
        """rebase, exec_error and budget_exceeded all lose their slot with the lease."""
        terminations = {
            'rebase': {'state': 'failed', 'status': 'failed', 'error_class': 'rebase'},
            'exec_error': {'state': 'failed', 'status': 'failed', 'error_class': 'exec_error'},
            'budget_exceeded': {'state': 'failed', 'status': 'budget_exceeded',
                                'error_class': 'budget_exceeded'},
        }
        for outcome, recorded in terminations.items():
            with self.subTest(outcome=outcome):
                with tempfile.TemporaryDirectory() as directory:
                    env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=directory)
                    run_id = f'run-{outcome}'
                    token = 'f' * 32
                    answer = self.call('gocc', op='acquire', token=token, run_id=run_id,
                                       backend='codex', provider=None, ttl_seconds=2, env=env)
                    self.assertTrue(answer['admitted'])
                    runs = pathlib.Path(directory, 'runs', run_id)
                    runs.mkdir(parents=True)
                    (runs / 'status.json').write_text(json.dumps({
                        'id': run_id, 'fleet_host': 'gocc', 'fleet_reservation_token': token,
                        'fleet_reserved_at': time.time(), **recorded}))
                    # The run ended, but no termination path released the slot.
                    self.assertEqual(len(self.call('gocc', op='status', env=env)['reservations']), 1)
                    time.sleep(2.2)
                    # The lease expired: the coordinator reclaimed the slot.
                    self.assertEqual(len(self.call('gocc', op='status', env=env)['reservations']), 0)
                    self.assertTrue(self.call('gocc', op='acquire', token='1' * 32, run_id='next',
                                              backend='codex', provider=None, env=env)['admitted'])

    def test_owner_reads_the_token_of_its_own_run_only(self):
        """A record without a token releases its slot by the token of its own reservation."""
        token = '9' * 32
        self.assertTrue(self.call('gocc', op='acquire', token=token, run_id='owner-run',
                                  backend='codex', provider=None)['admitted'])
        self.assertEqual(self.call('gocc', op='token', run_id='owner-run')['token'], token)
        # Another host learns nothing about a reservation it does not own.
        self.assertIsNone(self.call('gores1', op='token', run_id='owner-run')['token'])
        self.assertIsNone(self.call('gocc', op='token', run_id='other-run')['token'])

    def test_record_without_a_token_releases_the_exact_reservation(self):
        """A record which lost its token still frees the slot of its own run."""
        runs = pathlib.Path(self.temp.name, 'runs')
        rdir = runs / 'legacy-run'
        rdir.mkdir(parents=True)
        token = '7' * 32
        self.assertTrue(self.call('gocc', op='acquire', token=token, run_id='legacy-run',
                                  backend='codex', provider=None)['admitted'])
        (rdir / 'status.json').write_text(json.dumps({
            'id': 'legacy-run', 'state': 'running', 'runner_pid': 999999999,
            'fleet_host': 'gocc', 'fleet_reserved_at': 100.0}))
        policy = {'fleet': {'recovery_ttl_seconds': 10}}
        env = {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(xs, 'RUNS', str(runs)), \
             mock.patch.object(xs, 'NOW', return_value=110.1), \
             mock.patch.object(xs, 'pid_alive', return_value=False), \
             mock.patch.object(xs, 'executor_stopped', return_value=True):
            self.assertEqual(xs.recover_expired_fleet_slots([str(BIN), 'gocc'], policy), ['legacy-run'])
        self.assertEqual(self.call('gocc', op='status')['reservations'], [])

    def test_heartbeat_renews_the_lease_until_the_run_stops(self):
        """The client renews the lease of a live run and stops with the run."""
        rdir = pathlib.Path(self.temp.name, 'runs', 'live')
        rdir.mkdir(parents=True)
        (rdir / 'status.json').write_text(json.dumps({'id': 'live', 'state': 'running'}))
        calls = []

        def fake_call(_command, payload, timeout=15):
            calls.append(payload)
            return {'renewed': True, 'expires_at': time.time() + 30}

        policy = {'fleet': {'lease': {'ttl_seconds': 3, 'renew_interval_seconds': 0.2}}}
        with mock.patch.object(xs, 'fleet_guard_call', side_effect=fake_call):
            heartbeat = xs.start_fleet_lease_heartbeat(
                str(rdir), ['guard'], {'run_id': 'live', 'token': 'a' * 32, 'ttl_seconds': 3}, policy)
            self.addCleanup(heartbeat.stop)
            self.wait_for(lambda: len(calls) >= 2, 'the heartbeat to renew the lease')
            self.assertEqual(calls[0]['op'], 'renew')
            self.assertEqual(calls[0]['token'], 'a' * 32)
            # A terminated run stops renewing: its lease has to expire instead.
            xs.set_status(str(rdir), state='failed')
            self.wait_for(lambda: not heartbeat.thread.is_alive(), 'the heartbeat to stop')
            renewals = len(calls)
            time.sleep(0.6)
            self.assertEqual(len(calls), renewals)

    def test_killed_in_flight_run_releases_its_slot_within_the_ttl(self):
        """A killed run loses its slot with the lease: the next run takes the slot."""
        home = pathlib.Path(self.temp.name, 'home')
        home.mkdir()
        guard = pathlib.Path(self.temp.name, 'guard.json')
        guard.write_text(json.dumps({'command': [str(BIN), 'gocc']}))
        env = dict(os.environ, OPENSWARM_HOME=str(home),
                   OPENSWARM_FLEET_GUARD_CONFIG=str(guard),
                   OPENSWARM_FLEET_CAPACITY_DIR=self.temp.name)
        policy = {'concurrency': {'total': 8},
                  'fleet': {'lease': {'ttl_seconds': 2, 'renew_interval_seconds': 0.4}}}
        log = pathlib.Path(self.temp.name, 'runner.log')
        with log.open('w') as stream:
            child = subprocess.Popen([sys.executable, '-c', LEASE_RUNNER, str(CLI), json.dumps(policy)],
                                     env=env, stdout=stream, stderr=stream)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if len(self.call('gocc', op='status')['reservations']) == 1:
                    break
                time.sleep(0.1)
            else:
                self.fail(f'the in-flight run did not take its slot: {log.read_text()}')
            # The heartbeat of the live run renews the lease: the slot survives
            # more than one TTL.
            time.sleep(3)
            self.assertEqual(len(self.call('gocc', op='status')['reservations']), 1)
            os.kill(child.pid, signal.SIGKILL)
            child.wait()
            self.wait_for(lambda: not self.call('gocc', op='status')['reservations'],
                          'the lease of the killed run to expire')
            # The freed slot serves the next run.
            self.assertTrue(self.call('gocc', op='acquire', token='9' * 32, run_id='next',
                                      backend='codex', provider=None)['admitted'])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


# The rebase card of the dispatcher: a lane run which rebases a PR.
REBASE_CARD = """Rebase-PR: #1792 fleet/A4-issue1792-20261103-114416-2bc809

PR #1792 head `504d685e0` conflicts with main. Start from branch `rebase-pr-1792` (the PR head),
rebase onto main, resolve conflicts keeping main's merged changes and this PR's intent,
run the tests, and commit.

LANE_TEST_CRASH
"""

# A fleet guard which forwards every request to the coordinator but refuses an
# explicit release.  It reproduces the leak of the field incidents: the run ends
# and its termination path does not free the slot, so only the lease can.
GUARD_WITHOUT_RELEASE = '''#!/usr/bin/env python3
import json, subprocess, sys

request = json.load(sys.stdin)
if request.get("op") == "release":
    print(json.dumps({"ok": False, "error": "release refused by the test guard"}))
    sys.exit(1)
process = subprocess.run([sys.executable, sys.argv[1], sys.argv[2]], input=json.dumps(request),
                          text=True, capture_output=True)
sys.stdout.write(process.stdout)
sys.exit(process.returncode)
'''


class FleetLeaseTerminalStateTests(unittest.TestCase):
    """Every terminal path of a run frees its slot with the lease, not with the path.

    The runs are real lane turns: the terminal state is produced by the lane
    code, and the slot is freed by the lease of the coordinator alone.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.home = self.root / 'home'
        (self.home / 'lanes').mkdir(parents=True)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Lease Test')
        self.git('config', 'user.email', 'lease-test@example.invalid')
        (self.repo / 'README.md').write_text('base\n')
        self.git('add', 'README.md')
        self.git('commit', '-q', '-m', 'base')
        fixture = HERE / 'fixtures' / 'fake_app_server.py'
        fixture.chmod(0o755)
        guard = self.root / 'guard.py'
        guard.write_text(GUARD_WITHOUT_RELEASE)
        guard.chmod(0o755)
        config = self.root / 'guard.json'
        config.write_text(json.dumps({'command': [sys.executable, str(guard), str(BIN), 'gocc']}))
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.home),
                        OPENSWARM_FLEET_GUARD_CONFIG=str(config),
                        OPENSWARM_FLEET_CAPACITY_DIR=str(self.root / 'capacity'))
        (self.home / 'lanes.json').write_text(json.dumps({'lanes': [{
            'name': 'L1', 'backend': 'codex', 'model': 'fake-model', 'effort': 'xhigh',
            'repo': str(self.repo), 'binary': str(fixture)}]}))
        # A short lease: the slot returns within the TTL of the run.
        (self.home / 'policy.local.json').write_text(json.dumps({'fleet': {'lease': {
            'ttl_seconds': 3, 'renew_interval_seconds': 0.5}}}))
        self.lane = 'L1'
        # The coordinator sweeps the expired leases on its own.
        self.sweeper = subprocess.Popen(
            [str(BIN), '--sweep-loop', 'gocc'],
            env=dict(self.env, OPENSWARM_FLEET_CAPACITY_SWEEP_INTERVAL='0.2'),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_sweeper)

    def stop_sweeper(self):
        if self.sweeper.poll() is None:
            self.sweeper.terminate()
            try:
                self.sweeper.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.sweeper.kill()

    def tearDown(self):
        try:
            self.cli('lane', 'down', check=False, timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def cli(self, *args, check=True, timeout=30):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], env=self.env,
                                text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            self.fail(f'CLI failed ({result.returncode}): {result.stdout}\n{result.stderr}')
        return result

    def run_state(self, run_id):
        try:
            return json.loads((self.home / 'runs' / run_id / 'status.json').read_text())
        except (OSError, ValueError):
            return {}

    def run_result(self, run_id):
        try:
            return json.loads((self.home / 'runs' / run_id / 'result.json').read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(self, predicate, description, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail(f'timed out waiting for {description}')

    def coordinator_rows(self):
        """The reservations of the coordinator database, read without a request."""
        connection = sqlite3.connect(str(self.root / 'capacity' / 'capacity.sqlite'))
        try:
            return connection.execute('SELECT token,run_id FROM reservations').fetchall()
        finally:
            connection.close()

    def audit_rows(self):
        """The audit trail of the coordinator database, read without a request."""
        connection = sqlite3.connect(str(self.root / 'capacity' / 'capacity.sqlite'))
        try:
            return [json.loads(row[0]) for row in
                    connection.execute('SELECT entry FROM lease_audit ORDER BY id').fetchall()]
        finally:
            connection.close()

    def assert_slot_returns_with_the_lease(self, task, expected_status, expected_error_class, *extra):
        """A run which ends in ``expected_status`` gives its slot back with the lease."""
        self.cli('lane', 'up', self.lane)
        run_id = json.loads(self.cli('lane', 'assign', self.lane, '--prompt', task, *extra).stdout)['id']
        state = self.wait_for(lambda: (s if (s := self.run_state(run_id)).get('state') == 'finished' else None),
                              description=f'run {run_id} to finish')
        # The terminal state is the one of the real lane code, not a hand written file.
        self.assertEqual(state['status'], expected_status)
        result = self.run_result(run_id)
        self.assertEqual(result.get('status'), expected_status)
        if expected_error_class:
            self.assertEqual(result.get('error_class'), expected_error_class)
        # The termination path could not free the slot: the guard refuses a release.
        self.assertEqual([row[1] for row in self.coordinator_rows()], [run_id])
        # The lease of the coordinator frees the slot within its TTL.
        self.wait_for(lambda: not self.coordinator_rows(), 'the lease to free the slot')
        audit = self.audit_rows()
        self.assertTrue(any(item['event'] == 'lease-expired' and item['run_id'] == run_id
                            for item in audit), audit)

    def test_exec_error_run_gives_its_slot_back_with_the_lease(self):
        """A run which ended with an exec error frees its slot with the lease."""
        self.assert_slot_returns_with_the_lease('LANE_TEST_CRASH', 'failed', 'exec_error')

    def test_budget_exceeded_run_gives_its_slot_back_with_the_lease(self):
        """A run which hit its budget frees its slot with the lease."""
        self.assert_slot_returns_with_the_lease('LANE_TEST_WAIT exceed the budget',
                                                'budget_exceeded', None, '--budget-minutes', '0.02')

    def test_rebase_run_gives_its_slot_back_with_the_lease(self):
        """A run of the rebase path frees its slot with the lease."""
        # The task is a rebase card of the dispatcher, read with its own parser.
        self.assertEqual(self.rebase_of(REBASE_CARD),
                         (1792, 'fleet/A4-issue1792-20261103-114416-2bc809'))
        self.assert_slot_returns_with_the_lease(REBASE_CARD, 'failed', 'exec_error')

    @staticmethod
    def dispatch_engine():
        """The dispatcher which builds the rebase cards."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'dispatch_engine_lease_test', str(HERE.parent / 'lib' / 'dispatch' / 'engine.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def rebase_of(body):
        """The rebase card of a body, read with the parser of the dispatcher."""
        module = FleetLeaseTerminalStateTests.dispatch_engine()
        match = re.search(module.DEFAULTS['rebase_line_re'], body or '', re.M)
        return (int(match.group(1)), match.group(2)) if match else None
