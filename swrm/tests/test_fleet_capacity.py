"""Cross-host admission is atomic and uncertain reservations are never expired."""
import concurrent.futures
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
BIN = HERE.parent / 'bin' / 'fleet-capacity'
sys.path.insert(0, str(HERE))
import _load
xs = _load.load()

class FleetCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=self.temp.name)

    def call(self, authenticated_host, **payload):
        argv = [str(BIN), authenticated_host]
        if os.name == "nt":  # Windows cannot CreateProcess a shebang script (WinError 193)
            argv = [sys.executable, *argv]
        p = subprocess.run(argv, input=json.dumps(payload), text=True,
                           capture_output=True, env=self.env, timeout=15)
        return json.loads(p.stdout)

    def test_atomic_limit_and_idempotent_recovery(self):
        def attempt(i):
            return self.call('broker' if i % 2 else 'worker', op='acquire',
                             token=f'{i:032x}', run_id=f'run-{i}', backend='pi', provider='minimax-cn')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            answers = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(bool(a.get('admitted')) for a in answers), 1)
        winner = next(i for i, a in enumerate(answers) if a.get('admitted'))
        host = 'broker' if winner % 2 else 'worker'
        token = f'{winner:032x}'
        self.assertTrue(self.call(host, op='acquire', token=token, run_id=f'run-{winner}',
                                  backend='pi', provider='minimax-cn')['existing'])
        self.assertFalse(self.call('worker' if host == 'broker' else 'broker', op='release',
                                   token=token, run_id=f'run-{winner}')['ok'])
        self.assertEqual(len(self.call(host, op='status')['reservations']), 1)
        self.assertTrue(self.call(host, op='release', token=token, run_id=f'run-{winner}')['released'])
        self.assertEqual(self.call(host, op='status')['reservations'], [])

    def test_disconnected_coordinator_fails_closed(self):
        config = pathlib.Path(self.temp.name, 'guard.json')
        config.write_text(json.dumps({'command': ['/nonexistent/coordinator']}))
        old = os.environ.get('OPENSWARM_FLEET_GUARD_CONFIG')
        os.environ['OPENSWARM_FLEET_GUARD_CONFIG'] = str(config)
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

    def test_remote_review_assignment_passes_read_only_to_lane(self):
        root = pathlib.Path(self.temp.name)
        binary = root / '.local/bin/swrm'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                          'open(os.environ["FAKE_ARGS"],"w").write(json.dumps(sys.argv))\n'
                          'print(json.dumps({"ok":True,"id":"test"}))\n')
        binary.chmod(0o755)
        self.env['HOME'] = str(root)
        self.env['FAKE_ARGS'] = str(root / 'args.json')
        response = self.call('worker', op='assign', lane='GH1', prompt='review', base='abcdef0',
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
        git('checkout', '-qb', 'swrm/run-1')
        (repo / 'file').write_text('after')
        git('-c', 'user.name=test', '-c', 'user.email=test@test', 'commit', '-qam', 'result')
        commit = git('rev-parse', 'HEAD')
        fake = root / '.local/bin/swrm'
        fake.parent.mkdir(parents=True)
        info = {'id':'run-1', 'result':{'branch':'swrm/run-1', 'commit':commit,
                                       'workspace':{'repo':str(repo), 'base':base}}}
        fake.write_text('#!' + sys.executable + '\nimport json\nprint(' + repr(json.dumps(info)) + ')\n')
        fake.chmod(0o755)
        self.env['HOME'] = str(root)
        response = self.call('worker', op='bundle', run_id='run-1')
        self.assertTrue(response['ok'])
        self.assertEqual(response['result']['commit'], commit)
        self.assertTrue(response['result']['bundle'])

    def test_html_403_is_not_misclassified_as_login_failure(self):
        error = 'request failed (code 403): HTML response "Error 403 (Forbidden)!!1"'
        self.assertEqual(xs.executor_error_class(1, None, error, 'agy'), 'upstream_forbidden')
        self.assertEqual(xs.executor_error_class(1, None, error, 'codex'), 'exec_error')

    def test_reaped_stopped_runner_retains_slot_without_boundary(self):
        guard = pathlib.Path(self.temp.name, 'guard.json')
        guard.write_text(json.dumps({'command': [str(BIN), 'broker']}))
        env = {'OPENSWARM_FLEET_GUARD_CONFIG': str(guard), 'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(xs.procboundary.CgroupBoundary, 'create', return_value=None), \
             mock.patch.dict(xs.EXECUTORS, {'pi': lambda *args: (133, None, {}, 'stopped')}):
            rdir = pathlib.Path(self.temp.name, 'runs', 'reaped')
            rdir.mkdir(parents=True)
            _, _, error = xs.run_executor_with_rate_limit('pi', {'backend': 'pi', 'model': 'fake'}, {}, 'review',
                                                          self.temp.name, str(rdir), xs.NOW() + 15,
                                                          {'concurrency': {'total': 8}})
            self.assertEqual(error, 'risk_blocked')
            self.assertTrue(self.call('broker', op='status')['reservations'])
            xs.release_fleet_slot(str(rdir))

    @unittest.skipIf(os.name == 'nt', 'fixture relies on POSIX detached sessions')
    def test_successful_executor_retains_slot_for_unobserved_detached_descendant(self):
        guard = pathlib.Path(self.temp.name, 'guard.json')
        guard.write_text(json.dumps({'command': [str(BIN), 'broker']}))
        child_pid_file = pathlib.Path(self.temp.name, 'detached-child.pid')
        fixture = pathlib.Path(self.temp.name, 'detached_executor.py')
        fixture.write_text(
            'import os, pathlib, subprocess, sys, time\n'
            'if sys.argv[1] == "child":\n'
            '    pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))\n'
            '    time.sleep(60)\n'
            'else:\n'
            '    subprocess.Popen([sys.executable, __file__, "child", sys.argv[2]], start_new_session=True)\n'
            '    deadline = time.monotonic() + 5\n'
            '    while not pathlib.Path(sys.argv[2]).exists() and time.monotonic() < deadline: time.sleep(0.01)\n'
            '    if not pathlib.Path(sys.argv[2]).exists(): sys.exit(2)\n'
        )

        def executor(_decision, _state, _prompt, wd, _rdir, timeout, event_file):
            parent = subprocess.Popen([sys.executable, str(fixture), 'parent', str(child_pid_file)],
                                      cwd=wd, start_new_session=True)
            rc = parent.wait(timeout=timeout)
            return rc, {}, {}, ''

        rdir = pathlib.Path(self.temp.name, 'runs', 'detached')
        rdir.mkdir(parents=True)
        child_pid = None
        try:
            with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_GUARD_CONFIG': str(guard),
                                               'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
                    mock.patch.dict(xs.EXECUTORS, {'pi': executor}):
                _, _, error = xs.run_executor_with_rate_limit(
                    'pi', {'backend': 'pi', 'model': 'fake'}, {}, 'review', self.temp.name,
                    str(rdir), xs.NOW() + 15, {'concurrency': {'total': 8}})
                self.assertIsNone(error)
                child_pid = int(child_pid_file.read_text())
                self.assertTrue(xs.pid_alive(child_pid))
                self.assertTrue(self.call('broker', op='status')['reservations'],
                                'fleet capacity was released while a detached executor descendant remained alive')
        finally:
            if child_pid is None and child_pid_file.exists():
                child_pid = int(child_pid_file.read_text())
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except OSError:
                    pass
            if os.path.realpath(rdir) in xs._FLEET_RESERVATIONS:
                with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_GUARD_CONFIG': str(guard),
                                                   'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}):
                    xs.release_fleet_slot(str(rdir))

    def test_one_shot_agy_403_retains_slot_without_boundary(self):
        guard = pathlib.Path(self.temp.name, 'guard.json')
        guard.write_text(json.dumps({'command': [str(BIN), 'broker']}))
        fake = HERE / 'fixtures/fake_agy.py'
        fake.chmod(0o755)
        env = {'OPENSWARM_FLEET_GUARD_CONFIG': str(guard),
               'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name,
               'OPENSWARM_AGY_BIN': str(fake), 'FAKE_AGY_MODE': 'forbidden-exit'}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(xs.procboundary.CgroupBoundary, 'create', return_value=None):
            for number in range(2):
                rdir = pathlib.Path(self.temp.name, 'runs', f'run-{number}')
                rdir.mkdir(parents=True)
                attempts, _, error = xs.run_executor_with_rate_limit(
                    'agy', {'backend': 'agy', 'model': 'fake-model'}, {}, 'review', self.temp.name,
                    str(rdir), xs.NOW() + 15, {'concurrency': {'total': 8}})
                self.assertEqual(error, 'upstream_forbidden')
                self.assertEqual(attempts[0]['rc'], 1)
                self.assertTrue(self.call('broker', op='status')['reservations'])
                xs.release_fleet_slot(str(rdir))

    def test_recovery_requires_local_explicit_confirmation(self):
        token = 'a' * 32
        self.assertTrue(self.call('worker', op='acquire', token=token, run_id='orphan',
                                  backend='cursor', provider=None)['admitted'])
        self.assertFalse(self.call('worker', op='recover', host='worker', run_id='orphan',
                                   confirm='all-executor-processes-stopped')['ok'])
        self.assertFalse(self.call('broker', op='recover', host='worker', run_id='orphan')['ok'])
        self.assertEqual(self.call('broker', op='recover', host='worker', run_id='orphan',
                                   confirm='all-executor-processes-stopped')['recovered'], 1)

    def test_host_limit_is_shared_across_backend_types(self):
        first = dict(op='acquire', token='1' * 32, run_id='one', backend='codex', provider=None)
        second = dict(op='acquire', token='2' * 32, run_id='two', backend='agy', provider=None)
        third = dict(op='acquire', token='3' * 32, run_id='three', backend='copilot', provider=None)
        self.assertTrue(self.call('broker', **first)['admitted'])
        self.assertTrue(self.call('broker', **second)['admitted'])
        self.assertEqual(self.call('broker', **third)['blocked'], ['host:broker'])
        self.assertTrue(self.call('worker', **third)['admitted'])

    def test_local_host_shares_backend_limit_and_cannot_recover(self):
        local = dict(op='acquire', token='1' * 32, run_id='local-turn', backend='codex', provider=None)
        remote = dict(op='acquire', token='2' * 32, run_id='remote-run', backend='codex', provider=None)
        self.assertTrue(self.call('local', **local)['admitted'])
        self.assertEqual(self.call('broker', **remote)['blocked'], ['codex'])
        self.assertFalse(self.call('local', op='recover', host='local', run_id='local-turn',
                                   confirm='all-executor-processes-stopped')['ok'])
        self.assertEqual(self.call('broker', op='recover', host='local', run_id='local-turn',
                                   confirm='all-executor-processes-stopped')['recovered'], 1)
        self.assertTrue(self.call('broker', **remote)['admitted'])

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

    def test_early_agy_result_retains_reservation_without_boundary(self):
        rdir = os.path.join(self.temp.name, 'runs', 'early-agy')
        os.makedirs(rdir)
        fake = mock.Mock(return_value=(1, None, {'usage':{}, 'finished_reason':'print_timeout'},
                                      'agy turn ended early: print_timeout'))
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=[str(BIN), 'broker']), \
             mock.patch.object(xs.procboundary.CgroupBoundary, 'create', return_value=None), \
             mock.patch.dict(xs.EXECUTORS, {'agy':fake}):
            attempts, final, error = xs.run_executor_with_rate_limit(
                'agy', {'backend':'agy', 'model':'gemini-3.8-flash-medium', 'effort':None}, {}, 'task', self.temp.name, rdir,
                xs.NOW() + 30, xs.load_policy())
        self.assertEqual(error, 'exec_error')
        self.assertIsNone(final)
        self.assertTrue(self.call('broker', op='status')['reservations'])
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}):
            xs.release_fleet_slot(rdir)

if __name__ == '__main__':
    unittest.main()
