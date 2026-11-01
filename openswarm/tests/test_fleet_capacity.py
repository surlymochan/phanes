"""Cross-host admission is atomic and uncertain reservations are never expired."""
import concurrent.futures
import json
import os
import pathlib
import signal
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

    def test_provider_capacity_ignores_zero_negative_and_missing_limits(self):
        keys = xs.concurrency_keys('pi', 'minimax-cn')
        self.assertEqual(xs.provider_concurrency_limits(
            {'concurrency': {'pi': 4, 'minimax-cn': 0, 'total': -1}}, keys), {'pi': 4})
        self.assertEqual(xs.provider_concurrency_limits(
            {'concurrency': {'pi': 4}}, ['missing']), {})

    def test_batch_capacity_aggregates_normal_and_full_load_without_negative_slots(self):
        spec = {'backend': 'pi', 'provider': 'minimax-cn',
                'concurrency': {'pi': 2, 'minimax-cn': 2, 'total': 2},
                'hosts': {'local': {'kind': 'local'}}}
        row = {'host': 'local'}
        snapshots = {'local': {'active_runs': []}}
        with mock.patch.object(xs, 'fleet_config', return_value=spec['hosts']), \
                mock.patch.object(xs, 'instabox_states', return_value={}), \
                mock.patch.object(xs, 'host_json', return_value=snapshots['local']):
            self.assertTrue(xs.batch_provider_slot_available(spec, row))
            snapshots['local']['active_runs'] = [
                {'state': 'running', 'backend': 'pi', 'provider': 'minimax-cn'},
                {'state': 'running', 'backend': 'pi', 'provider': 'minimax-cn'},
            ]
            self.assertFalse(xs.batch_provider_slot_available(spec, row))
            snapshots['local']['active_runs'].append(
                {'state': 'running', 'backend': 'pi', 'provider': 'minimax-cn'})
            self.assertFalse(xs.batch_provider_slot_available(spec, row))

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
             mock.patch.object(xs, 'fleet_guard_call', return_value={'recovered': 1}) as call:
            self.assertEqual(xs.recover_expired_fleet_slots(command, {'fleet': {'recovery_ttl_seconds': 600}}),
                             ['gores-turn'])
        self.assertEqual(call.call_args.args[1]['host'], 'gores1')

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
        self.assertEqual(self.call('gocc', op='recover', host='gores1', run_id='orphan',
                                   confirm='all-executor-processes-stopped')['recovered'], 1)

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
        self.assertEqual(self.call('gocc', op='recover', host='local', run_id='local-turn',
                                   confirm='all-executor-processes-stopped')['recovered'], 1)
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
