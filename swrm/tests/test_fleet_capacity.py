"""Cross-host admission is atomic and an expired lease frees its slot (Refs #2160).

The admission of a run is atomic: two runs never share one slot.  A
reservation is a lease, so a run which crashed, was killed or whose release
never reached the coordinator gives its slot back with the lease instead of
holding it forever (Refs #2160, #1792).
"""
import concurrent.futures
import json
import os
import pathlib
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
BOOT = '11111111-1111-1111-1111-111111111111'
NEXT_BOOT = '22222222-2222-2222-2222-222222222222'
sys.path.insert(0, str(HERE))
import _load
xs = _load.load()

class FleetCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(self.temp.cleanup)
        self.env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=self.temp.name)
        # Model coordinator-owned, authenticated kernel reads separately from
        # the untrusted JSON submitted by a reservation owner.
        self.boot_files = {}
        probes = {}
        for host in ('broker', 'worker', 'local'):
            path = pathlib.Path(self.temp.name, host + '.boot')
            path.write_text(BOOT)
            self.boot_files[host] = path
            probes[host] = [sys.executable, '-c',
                            'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())', str(path)]
        self.probe_config = pathlib.Path(self.temp.name, 'boot-probes.json')
        self.probe_config.write_text(json.dumps(probes))

    def call(self, authenticated_host, **payload):
        if payload.get('op') == 'status':
            deadline = time.monotonic() + 5
            while list(pathlib.Path(self.temp.name).glob('runs/*/fleet-release-pending.json')):
                if time.monotonic() >= deadline:
                    self.fail('release worker did not drain its queue')
                time.sleep(0.02)
        if payload.get('op') == 'acquire':
            payload.setdefault('boot_id', BOOT)
        argv = [str(BIN), authenticated_host]
        if os.name == "nt":  # Windows cannot CreateProcess a shebang script (WinError 193)
            argv = [sys.executable, *argv]
        p = subprocess.run(argv, input=json.dumps(payload), text=True,
                           capture_output=True, env=self.env, timeout=15)
        return json.loads(p.stdout)

    def expire(self, token):
        import sqlite3
        db = sqlite3.connect(pathlib.Path(self.temp.name, 'capacity.sqlite'))
        db.execute('UPDATE reservations SET lease_expires=0 WHERE token=?', (token,))
        db.commit()
        db.close()

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
            with mock.patch.object(xs, 'start_fleet_release_worker'):
                ok = xs.reserve_provider_slot(str(rdir), 'codex', None, {'concurrency': {'total': 1}},
                                              keep_waiting=lambda: False)
            self.assertFalse(ok)
            status = json.loads((rdir / 'status.json').read_text())
            self.assertEqual(status['state'], 'queued')
            self.assertIn('coordinator unavailable', status['fleet_reservation_error'])
        finally:
            if old is None: os.environ.pop('OPENSWARM_FLEET_GUARD_CONFIG', None)
            else: os.environ['OPENSWARM_FLEET_GUARD_CONFIG'] = old

    def test_acquire_gives_up_after_three_slow_coordinator_responses(self):
        rdir = pathlib.Path(self.temp.name, 'runs', 'slow-coordinator')

        def bounded_backoff(_seconds):
            if bounded_backoff.calls >= 2:
                raise AssertionError('acquire kept retrying after three coordinator failures')
            bounded_backoff.calls += 1

        bounded_backoff.calls = 0
        with mock.patch.object(xs, 'fleet_guard_command', return_value=['guard']), \
                mock.patch.object(xs, 'fleet_guard_call', side_effect=RuntimeError('coordinator timeout')) as call, \
                mock.patch.object(xs.time, 'sleep', side_effect=bounded_backoff):
            admitted = xs.reserve_provider_slot(str(rdir), 'codex', None, {'concurrency': {'total': 1}},
                                                keep_waiting=lambda: True)

        self.assertFalse(admitted)
        self.assertEqual(call.call_count, 3)
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['state'], 'queued')
        self.assertEqual(status['queue_reason'], 'capacity-unreachable')
        self.assertTrue(status['capacity_unreachable'])
        self.assertIn('coordinator timeout', status['fleet_reservation_error'])

    def test_required_missing_configuration_fails_closed(self):
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_GUARD_REQUIRED': '1',
                                          'OPENSWARM_FLEET_GUARD_CONFIG': os.path.join(self.temp.name, 'missing')}):
            with self.assertRaisesRegex(RuntimeError, 'required but missing'):
                xs.fleet_guard_command()

    def test_release_retries_transient_connection_failure(self):
        rdir = os.path.realpath(os.path.join(self.temp.name, 'run'))
        xs._FLEET_RESERVATIONS[rdir] = (['guard'], {'run_id': 'run', 'token': 'a' * 32})
        with mock.patch.object(xs, 'fleet_guard_call', side_effect=[RuntimeError('unavailable'), {'ok': True}]) as call, \
                mock.patch.object(xs, 'start_fleet_release_worker') as worker:
            self.assertFalse(xs.release_fleet_slot(rdir))
            call.assert_not_called()
            worker.assert_called_once_with(rdir)
            self.assertFalse(xs.retry_fleet_release(rdir))
            self.assertTrue(xs.retry_fleet_release(rdir))
        self.assertEqual(call.call_count, 2)
        self.assertNotIn(rdir, xs._FLEET_RESERVATIONS)
        self.assertFalse(pathlib.Path(rdir, 'fleet-release-pending.json').exists())

    def test_release_retains_reservation_after_repeated_failure(self):
        rdir = os.path.realpath(os.path.join(self.temp.name, 'run'))
        xs._FLEET_RESERVATIONS[rdir] = (['guard'], {'run_id': 'run', 'token': 'a' * 32})
        with mock.patch.object(xs, 'fleet_guard_call', side_effect=RuntimeError('unavailable')) as call, \
                mock.patch.object(xs, 'start_fleet_release_worker'):
            self.assertFalse(xs.release_fleet_slot(rdir))
            for _ in range(3):
                self.assertFalse(xs.retry_fleet_release(rdir))
        self.assertEqual(call.call_count, 3)
        pending = pathlib.Path(rdir, 'fleet-release-pending.json')
        self.assertEqual(json.loads(pending.read_text())['payload']['token'], 'a' * 32)

    def test_supervisor_renews_owned_fleet_lease(self):
        stop = mock.Mock()
        stop.wait.side_effect = [False, True]
        payload = {'run_id': 'renew-run', 'token': 'f' * 32}
        with mock.patch.object(xs, 'fleet_guard_call', return_value={'ok': True}) as call:
            xs.renew_fleet_slot(['guard'], payload, 1, stop)
        call.assert_called_once_with(['guard'], {'op': 'renew', 'run_id': 'renew-run', 'token': 'f' * 32})

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

    def test_recovery_requires_expiry_and_authenticated_owner_reboot(self):
        token = 'a' * 32
        self.assertTrue(self.call('worker', op='acquire', token=token, run_id='orphan',
                                  backend='cursor', provider=None)['admitted'])
        self.assertFalse(self.call('worker', op='recover', host='worker', run_id='orphan', token=token)['ok'])
        self.assertFalse(self.call('broker', op='recover', host='worker', run_id='orphan', token=token,
                                   confirm='all-executor-processes-stopped')['ok'])
        self.assertEqual(len(self.call('broker', op='status')['reservations']), 1)
        self.expire(token)
        self.assertFalse(self.call('broker', op='recover', host='worker', run_id='orphan', token=token)['ok'])
        self.boot_files['worker'].write_text(NEXT_BOOT)
        self.assertTrue(self.call('worker', op='attest_boot', boot_id=NEXT_BOOT)['ok'])
        row = self.call('broker', op='status')['reservations'][0]
        self.assertNotIn('token', row)
        recovery_id = row['recovery_id']
        self.assertEqual(self.call('broker', op='recover', host='worker', run_id='orphan',
                                   recovery_id=recovery_id)['recovered'], 1)
        self.assertEqual(self.call('broker', op='recover', host='worker', run_id='orphan',
                                   recovery_id=recovery_id)['recovered'], 0)

    def test_remote_recovery_rejects_unverified_local_confirmation(self):
        token = 'b' * 32
        self.assertTrue(self.call('worker', op='acquire', token=token, run_id='remote-orphan',
                                  backend='cursor', provider=None)['admitted'])

        # A broker-side assertion cannot establish that a worker's process
        # tree is stopped; remote recovery needs verifiable cross-host evidence.
        result = self.call('broker', op='recover', host='worker', run_id='remote-orphan',
                           token=token, confirm='all-executor-processes-stopped')

        self.assertFalse(result['ok'], result)
        self.assertEqual(len(self.call('broker', op='status')['reservations']), 1)

    def test_expired_lease_with_live_executor_and_failed_renewal_is_not_recoverable(self):
        self.env['OPENSWARM_FLEET_CAPACITY_TTL_SECONDS'] = '0.05'
        token = '7' * 32
        payload = {'token': token, 'run_id': 'partitioned'}
        self.assertTrue(self.call('worker', op='acquire', backend='cursor', **payload)['admitted'])
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            # Simulate the supervisor losing its coordinator connection while
            # the remote executor continues running; expiry is real, not a DB edit.
            stop = mock.Mock()
            stop.wait.side_effect = [False, True]
            with mock.patch.object(xs, 'fleet_guard_call', side_effect=RuntimeError('partition')):
                xs.renew_fleet_slot(['guard'], payload, 0.01, stop)
            time.sleep(0.1)
            self.assertIsNone(process.poll())
            for authenticated_host in ('broker', 'worker'):
                result = self.call(authenticated_host, op='recover', host='worker',
                                   confirm='all-executor-processes-stopped', **payload)
                self.assertFalse(result['ok'], result)
                self.assertIn('reboot evidence required', result['error'])
            # Neither broker-side claims nor its own reboot prove worker death.
            self.assertFalse(self.call('broker', op='attest_boot', host='worker', boot_id=NEXT_BOOT)['ok'])
            self.boot_files['broker'].write_text(NEXT_BOOT)
            self.assertTrue(self.call('broker', op='attest_boot', boot_id=NEXT_BOOT)['ok'])
            self.assertFalse(self.call('broker', op='recover', host='worker', boot_id=NEXT_BOOT, **payload)['ok'])
            self.assertEqual(self.call('broker', op='acquire', token='8' * 32, run_id='overlap',
                                       boot_id=NEXT_BOOT, backend='cursor')['blocked'], ['cursor'])
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_worker_cannot_forge_reboot_while_executor_is_alive(self):
        self.env['OPENSWARM_FLEET_CAPACITY_TTL_SECONDS'] = '0.05'
        payload = {'token': '5' * 32, 'run_id': 'forged-reboot'}
        self.assertTrue(self.call('worker', op='acquire', backend='cursor', **payload)['admitted'])
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            time.sleep(0.1)
            for op in ('attest_boot', 'acquire'):
                forged = {'op': op, 'boot_id': NEXT_BOOT}
                if op == 'acquire':
                    forged.update(token='4' * 32, run_id='forged-acquire', backend='pi')
                self.assertFalse(self.call('worker', **forged)['ok'])
                result = self.call('broker', op='recover', host='worker', **payload)
                self.assertFalse(result['ok'], result)
                self.assertEqual(self.call('broker', op='acquire', token='3' * 32,
                                           run_id='overlap', backend='cursor')['blocked'], ['cursor'])
                self.assertIsNone(process.poll())
                self.assertEqual(self.boot_files['worker'].read_text(), BOOT)
        finally:
            process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(pathlib.Path('/proc/sys/kernel/random/boot_id').is_file(),
                         'requires a Linux kernel boot id')
    def test_probe_reads_real_kernel_and_rejects_forged_initial_and_next_boot(self):
        kernel = pathlib.Path('/proc/sys/kernel/random/boot_id')
        actual_boot = kernel.read_text().strip()
        probes = json.loads(self.probe_config.read_text())
        probes['worker'][-1] = str(kernel)
        self.probe_config.write_text(json.dumps(probes))
        payload = {'token': '4' * 32, 'run_id': 'real-kernel'}
        fake_boot = NEXT_BOOT if actual_boot != NEXT_BOOT else BOOT
        self.assertFalse(self.call('worker', op='acquire', boot_id=fake_boot,
                                   backend='cursor', **payload)['ok'])
        self.assertEqual(self.call('broker', op='status')['reservations'], [])
        self.assertTrue(self.call('worker', op='acquire', boot_id=actual_boot,
                                  backend='cursor', **payload)['admitted'])
        self.expire(payload['token'])
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            self.assertFalse(self.call('worker', op='attest_boot', boot_id=fake_boot)['ok'])
            self.assertFalse(self.call('broker', op='recover', host='worker',
                                       boot_id=fake_boot, **payload)['ok'])
            self.assertEqual(self.call('broker', op='acquire', token='3' * 32,
                                       run_id='overlap', backend='cursor')['blocked'], ['cursor'])
            self.assertIsNone(process.poll())
            self.assertEqual(kernel.read_text().strip(), actual_boot)
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_recover_observes_remote_reboot_without_owner_attestation(self):
        payload = {'token': '4' * 32, 'run_id': 'probe-reboot'}
        self.assertTrue(self.call('worker', op='acquire', backend='cursor', **payload)['admitted'])
        self.expire(payload['token'])
        self.assertFalse(self.call('broker', op='recover', host='worker',
                                   boot_id=NEXT_BOOT, **payload)['ok'])
        self.boot_files['worker'].write_text(NEXT_BOOT)
        self.assertEqual(self.call('broker', op='recover', host='worker', **payload)['recovered'], 1)
        self.assertTrue(self.call('broker', op='acquire', token='3' * 32,
                                  run_id='replacement', backend='cursor')['admitted'])

    def test_missing_or_failed_probe_never_reclaims_verified_reservation(self):
        payload = {'token': '4' * 32, 'run_id': 'probe-failure'}
        self.assertTrue(self.call('worker', op='acquire', backend='cursor', **payload)['admitted'])
        self.expire(payload['token'])
        for config in (None, {}, {'worker': 'invalid command'},
                       {'worker': [sys.executable, '-c', 'print("invalid boot id")']},
                       {'worker': [sys.executable, '-c', f'print("{NEXT_BOOT}"); exit(1)']},
                       {'worker': [sys.executable, '-c', 'import time; time.sleep(10)']}):
            with self.subTest(config=config):
                if config is None:
                    self.probe_config.unlink()
                else:
                    self.probe_config.write_text(json.dumps(config))
                self.assertFalse(self.call('broker', op='recover', host='worker', **payload)['ok'])
                self.assertEqual(len(self.call('broker', op='status')['reservations']), 1)

    def test_prior_self_reported_boot_records_are_not_migrated_as_proof(self):
        import sqlite3
        payload = {'token': '4' * 32, 'run_id': 'untrusted-legacy'}
        with sqlite3.connect(pathlib.Path(self.temp.name, 'capacity.sqlite')) as db:
            db.execute('CREATE TABLE reservations (token TEXT PRIMARY KEY, host TEXT NOT NULL, '
                       'run_id TEXT NOT NULL, backend TEXT NOT NULL, provider TEXT, '
                       'created REAL NOT NULL, lease_expires REAL, boot_id TEXT)')
            db.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?)',
                       (payload['token'], 'worker', payload['run_id'], 'cursor', None, 1, 0, BOOT))
            db.execute('CREATE TABLE host_boots (host TEXT, boot_id TEXT, retired INTEGER)')
            db.execute('INSERT INTO host_boots VALUES (?,?,?)', ('worker', BOOT, 1))
            db.execute('INSERT INTO host_boots VALUES (?,?,?)', ('worker', NEXT_BOOT, 0))
        self.boot_files['worker'].write_text(NEXT_BOOT)
        self.assertTrue(self.call('worker', op='attest_boot', boot_id=NEXT_BOOT)['ok'])
        self.assertFalse(self.call('broker', op='recover', host='worker', **payload)['ok'])
        row = self.call('broker', op='status')['reservations'][0]
        self.assertIsNone(row['boot_id'])
        self.assertTrue(self.call('worker', op='release', **payload)['released'])

    def test_reboot_evidence_fences_old_supervisor_and_requires_expiry(self):
        payload = {'token': '9' * 32, 'run_id': 'old-boot'}
        self.assertTrue(self.call('worker', op='acquire', backend='cursor', **payload)['admitted'])
        self.boot_files['worker'].write_text(NEXT_BOOT)
        self.assertTrue(self.call('worker', op='attest_boot', boot_id=NEXT_BOOT)['ok'])
        self.assertFalse(self.call('broker', op='recover', host='worker', **payload)['ok'])
        self.assertFalse(self.call('worker', op='renew', **payload)['ok'])
        self.assertFalse(self.call('worker', op='attest_boot', boot_id=BOOT)['ok'])
        self.assertFalse(self.call('worker', op='acquire', backend='cursor', **payload)['ok'])
        self.assertFalse(self.call('worker', op='acquire', boot_id=NEXT_BOOT, backend='cursor', **payload)['ok'])
        self.expire(payload['token'])
        self.assertEqual(self.call('broker', op='recover', host='worker', **payload)['recovered'], 1)
        self.assertFalse(self.call('worker', op='acquire', backend='cursor', **payload)['ok'])
        self.assertTrue(self.call('worker', op='acquire', boot_id=NEXT_BOOT, backend='cursor', **payload)['admitted'])

    def test_legacy_reservation_without_boot_evidence_cannot_be_recovered(self):
        payload = {'token': '6' * 32, 'run_id': 'unproven'}
        probes = self.probe_config.read_text()
        self.probe_config.unlink()
        self.assertTrue(self.call('worker', op='acquire', boot_id=BOOT, backend='cursor', **payload)['admitted'])
        self.assertIsNone(self.call('broker', op='status')['reservations'][0]['boot_id'])
        self.expire(payload['token'])
        self.probe_config.write_text(probes)
        for boot in (BOOT, NEXT_BOOT):
            self.boot_files['worker'].write_text(boot)
            self.assertTrue(self.call('worker', op='attest_boot', boot_id=boot)['ok'])
        self.assertFalse(self.call('broker', op='recover', host='worker', **payload)['ok'])
        self.assertEqual(len(self.call('broker', op='status')['reservations']), 1)

    def test_renew_requires_authenticated_owner_and_exact_reservation(self):
        token = 'c' * 32
        acquired = self.call('worker', op='acquire', token=token, run_id='renew-me',
                             backend='cursor', provider=None)
        self.assertTrue(acquired['admitted'])
        before = acquired['lease_expires']
        self.assertFalse(self.call('broker', op='renew', token=token, run_id='renew-me')['ok'])
        self.assertFalse(self.call('worker', op='renew', token='d' * 32, run_id='renew-me')['ok'])
        self.assertFalse(self.call('worker', op='renew', token=token, run_id='wrong')['ok'])
        renewed = self.call('worker', op='renew', token=token, run_id='renew-me')
        self.assertTrue(renewed['ok'])
        self.assertGreaterEqual(renewed['lease_expires'], before)
        self.assertFalse(self.call('worker', op='renew', token=token, run_id='renew-me',
                                   lease_expires=before + 1000)['ok'])

    def test_legacy_reservations_migrate_to_conservative_lease(self):
        import sqlite3
        created = 1_800_000_000.0
        db = sqlite3.connect(pathlib.Path(self.temp.name, 'capacity.sqlite'))
        db.execute('CREATE TABLE reservations (token TEXT PRIMARY KEY, host TEXT NOT NULL, run_id TEXT NOT NULL, '
                   'backend TEXT NOT NULL, provider TEXT, created REAL NOT NULL)')
        db.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?)',
                   ('e' * 32, 'worker', 'legacy', 'cursor', None, created))
        db.commit()
        db.close()
        status = self.call('broker', op='status')
        row = status['reservations'][0]
        self.assertEqual(row['lease_expires'], created + 300)
        self.assertEqual(self.call('broker', op='status')['reservations'][0]['lease_expires'], created + 300)

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
                                   token=local['token'])['ok'])
        self.expire(local['token'])
        self.boot_files['local'].write_text(NEXT_BOOT)
        self.assertTrue(self.call('local', op='attest_boot', boot_id=NEXT_BOOT)['ok'])
        self.assertEqual(self.call('broker', op='recover', host='local', run_id='local-turn',
                                   token=local['token'])['recovered'], 1)
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
             mock.patch.object(xs, 'start_fleet_release_worker'), \
             mock.patch.object(xs, 'fleet_guard_call', side_effect=[{'admitted': True, 'fence': 1}, {'released': True}]) as call:
            self.assertFalse(xs.reserve_provider_slot(rdir, 'codex', None, {'concurrency': {'total': 2}},
                                                      keep_waiting=lambda: False))
            self.assertTrue(xs.retry_fleet_release(rdir))
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

# The grace period of the reclaim: the coordinator may reclaim a lease whose
# expiry lies before "now - grace".  The grace is configurable; the primary
# name of the knob comes first, the aliases keep the contract of this suite
# readable for a coordinator which reads one of them.
GRACE_ENV_NAMES = ('OPENSWARM_FLEET_CAPACITY_GRACE_SECONDS',
                    'OPENSWARM_FLEET_CAPACITY_RECLAIM_GRACE_SECONDS',
                    'OPENSWARM_FLEET_CAPACITY_TTL_GRACE_SECONDS',
                    'OPENSWARM_FLEET_LEASE_GRACE_SECONDS')

# The tables of the coordinator database which are not the audit trail.
KNOWN_TABLES = ('reservations', 'fence_counter', 'verified_host_boots')


def grace_env(seconds):
    """The environment of a coordinator whose grace period is `seconds`."""
    return {name: str(seconds) for name in GRACE_ENV_NAMES}


def numbers(payload):
    """Every integer of a nested answer (a negative count is a bug)."""
    if isinstance(payload, bool):
        return
    if isinstance(payload, int):
        yield payload
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from numbers(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from numbers(value)


class SafeBoundary:
    """A boundary which proves that the executor left no descendants behind."""

    def finish(self):
        return True, None

    def attach_current_process(self):
        pass


class FleetCapacityTTLReclaimTests(unittest.TestCase):
    """An expired lease frees its slot without any terminal path (Refs #2160).

    The coordinator of ``swrm/bin/fleet-capacity`` used to keep a reservation
    until it was released explicitly: a run which crashed, was killed or whose
    release never reached the coordinator held its slot forever (the ghost
    reservations of #1792).  Since the fencing token of #2158 is in place, the
    coordinator may reclaim an expired lease without any boot evidence: the
    fence of the reclaimed reservation refuses the write-back of the old run.

    This suite is the contract of that reclaim:

    * the reclaim needs no boot evidence and no terminal path of a run;
    * every request of the coordinator cleans the expired leases up on the way,
      and ``--sweep-once`` reclaims them without any request at all (a janitor
      or a timer may call it);
    * the reclaim of a reservation invalidates its fence;
    * the reclaim is written to the audit trail of the coordinator in the same
      transaction: a failing audit write rolls the whole reclaim back;
    * a lease which is renewed is never reclaimed and a lease inside the grace
      period of the coordinator waits;
    * the reclaim and the explicit release of one reservation never count
      twice and never produce a negative count;
    * a run which lost its lease stops its executor and leaves a trace.

    The mapping of the blockers of the old PR #1808 (issue #1792) to the
    contract of this suite and to its tests:

    | Blocker | Contract | Test |
    | --- | --- | --- |
    | B1 (the sweep of the expired leases was not wired into the coordinator) | the coordinator reclaims an expired lease without any request and audits the reclaim | ``test_the_sweep_reclaims_an_expired_lease_without_any_request``, ``test_a_request_reclaims_the_expired_leases_of_the_other_runs`` |
    | B2 (the executor of a lost lease kept running after the reclaim) | a run which cannot prove its lease stops its executor, the write-back of the old run is refused | ``test_a_run_which_lost_its_lease_stops_its_executor``, ``test_a_run_whose_lease_was_reclaimed_cannot_write_its_result_back`` |
    | the acceptance of the card (a killed run gives its slot back) | the slot of a run which stopped renewing comes back after the TTL and the grace | ``test_a_killed_run_gives_its_slot_back_after_its_lease_expired`` |
    | the migration protection of a live run | a renewal pushes the expiry of the lease | ``test_a_renewed_lease_is_not_reclaimed`` |
    | the three terminal paths of a run | the reclaim never depends on a terminal path | ``test_a_rebase_run_gives_its_slot_back_with_the_lease``, ``test_an_exec_error_run_gives_its_slot_back_with_the_lease``, ``test_a_budget_exceeded_run_gives_its_slot_back_with_the_lease`` |
    | the audit of the reclaim | the audit record is written in the same transaction | ``test_a_failing_audit_rolls_the_reclaim_back`` |
    | the race of the reclaim and the release | one reservation is reclaimed at most once | ``test_a_reservation_is_never_reclaimed_twice``, ``test_the_reclaim_and_the_release_of_one_reservation_never_count_twice`` |
    | the fence of a reclaimed reservation | the reclaim invalidates the fence | ``test_the_reclaim_invalidates_the_fence_of_the_reservation`` |
    | the grace period of the reclaim | the grace is configurable, a lease inside it waits | ``test_a_lease_inside_the_grace_period_is_not_reclaimed`` |
    | the trace of a lost lease | a run which lost its lease leaves a trace | ``test_a_run_which_lost_its_lease_leaves_a_trace`` |
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=self.temp.name)
        self.db_path = self.root / 'capacity.sqlite'
        # The coordinator reads the kernel boot id over its own trusted channel.
        self.boot_files = {}
        probes = {}
        for host in ('broker', 'worker', 'local'):
            path = pathlib.Path(self.temp.name, host + '.boot')
            path.write_text(BOOT)
            self.boot_files[host] = path
            probes[host] = [sys.executable, '-c',
                            'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())', str(path)]
        self.probe_config = pathlib.Path(self.temp.name, 'boot-probes.json')
        self.probe_config.write_text(json.dumps(probes))
        # The client keeps its own state inside the fixture, never in the home.
        for name, value in (('DATA', str(self.root / 'data')),
                            ('RUNS', str(self.runs))):
            patch = mock.patch.object(xs, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    # ------------------------------------------------------------- coordinator

    def ask(self, authenticated_host, **payload):
        """One request to the coordinator."""
        if payload.get('op') == 'acquire':
            payload.setdefault('boot_id', BOOT)
        argv = [str(BIN), authenticated_host]
        if os.name == "nt":  # Windows cannot CreateProcess a shebang script (WinError 193)
            argv = [sys.executable, *argv]
        process = subprocess.run(argv, input=json.dumps(payload), text=True,
                                 capture_output=True, env=self.env, timeout=30)
        try:
            return json.loads(process.stdout)
        except ValueError:
            return {'ok': False, 'error': (process.stderr or process.stdout or 'no answer')[:300]}

    def sweep(self):
        """Reclaim the expired leases without any request (the janitor entry point)."""
        attempts = []
        for argv in ([BIN, '--sweep-once'], [BIN, 'broker', '--sweep-once'],
                     [BIN, '--sweep-once', 'broker']):
            if os.name == "nt":
                argv = [sys.executable, *argv]
            process = subprocess.run(argv, input='', text=True, capture_output=True,
                                     env=self.env, timeout=30)
            attempts.append((' '.join(argv[1:]), process.returncode,
                             process.stdout.strip()[:200], process.stderr.strip()[:200]))
            if process.returncode != 0:
                continue
            try:
                answer = json.loads(process.stdout)
            except ValueError:
                answer = {'ok': True}
            if answer.get('ok', True):
                return answer
        self.fail('the coordinator offers no --sweep-once which reclaims the expired leases '
                  'without a request (Refs #2160): %r' % (attempts,))

    def rows(self):
        """Every reservation of the coordinator database."""
        with sqlite3.connect(self.db_path) as db:
            cursor = db.execute('SELECT * FROM reservations')
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def expire(self, token):
        """Let the lease of a reservation run out."""
        with sqlite3.connect(self.db_path) as db:
            db.execute('UPDATE reservations SET lease_expires=0 WHERE token=?', (token,))

    def audit_tables(self):
        """The audit tables of the coordinator database."""
        with sqlite3.connect(self.db_path) as db:
            tables = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")]
        tables = [name for name in tables if name not in KNOWN_TABLES]
        preferred = [name for name in tables
                     if any(word in name.lower() for word in ('audit', 'trail', 'log', 'event'))]
        return preferred or tables

    def audit_rows(self):
        """Every audit record of the coordinator database."""
        records = []
        with sqlite3.connect(self.db_path) as db:
            for name in self.audit_tables():
                cursor = db.execute('SELECT * FROM %s' % name)
                names = [column[0] for column in cursor.description]
                for row in cursor.fetchall():
                    records.append(dict(zip(names, row)))
        return records

    # ------------------------------------------------------------------ cases

    def test_an_expired_lease_is_reclaimed_without_any_boot_evidence(self):
        token = 'a' * 32
        self.assertTrue(self.ask('worker', op='acquire', token=token, run_id='ghost',
                                 backend='cursor', provider=None)['admitted'])
        # The boot evidence of the owner is gone: the reclaim of an expired
        # lease must not need it (Refs #2160).
        self.probe_config.unlink()
        self.expire(token)
        self.sweep()
        self.assertEqual(self.rows(), [], 'the expired lease still holds the slot')

    def test_a_request_reclaims_the_expired_leases_of_the_other_runs(self):
        token = 'a' * 32
        self.assertTrue(self.ask('worker', op='acquire', token=token, run_id='ghost',
                                 backend='cursor', provider=None)['admitted'])
        self.expire(token)
        # The next request of the coordinator cleans the expired leases up on
        # the way, so the slot of the ghost is free again (one cursor only).
        answer = self.ask('broker', op='acquire', token='b' * 32, run_id='fresh',
                          backend='cursor', provider=None)
        self.assertTrue(answer.get('admitted'), answer)
        self.assertEqual([row['token'] for row in self.rows()], ['b' * 32])

    def test_a_killed_run_gives_its_slot_back_after_its_lease_expired(self):
        # A short lease and no grace: the slot of a run which stopped renewing
        # comes back with its lease, without any release of the run.
        self.env['OPENSWARM_FLEET_CAPACITY_TTL_SECONDS'] = '1'
        self.env.update(grace_env(0))
        token = 'a' * 32
        acquired = self.ask('worker', op='acquire', token=token, run_id='killed',
                             backend='cursor', provider=None)
        self.assertTrue(acquired['admitted'], acquired)
        fence = acquired.get('fence')
        # While the lease is alive the slot belongs to the run.
        blocked = self.ask('broker', op='acquire', token='b' * 32, run_id='fresh',
                           backend='cursor', provider=None)
        self.assertEqual(blocked.get('blocked'), ['cursor'])
        # The run is killed: it stops renewing its lease.
        time.sleep(1.3)
        self.sweep()
        self.assertEqual(self.rows(), [], 'the killed run still holds the slot')
        # The next run takes the slot of the killed run.
        answer = self.ask('broker', op='acquire', token='b' * 32, run_id='fresh',
                          backend='cursor', provider=None)
        self.assertTrue(answer.get('admitted'), answer)
        # The write-back of the killed run is refused: its fence is gone.
        refused = self.ask('worker', op='validate', run_id='killed', token=token, fence=fence)
        self.assertFalse(refused.get('ok'), refused)

    def test_the_sweep_reclaims_an_expired_lease_without_any_request(self):
        token = 'a' * 32
        self.assertTrue(self.ask('worker', op='acquire', token=token, run_id='ghost',
                                 backend='cursor', provider=None)['admitted'])
        self.expire(token)
        before = len(self.audit_rows())
        self.sweep()
        # The database itself shows the reclaim: no request was needed.
        self.assertEqual(self.rows(), [], 'the expired lease still holds the slot')
        records = self.audit_rows()
        self.assertEqual(len(records) - before, 1,
                         'the reclaim left no audit record (Refs #2160): %r' % (records,))
        self.assertTrue(any('ghost' in json.dumps(record, default=str) for record in records),
                        'the audit record does not name the reclaimed run: %r' % (records,))

    def test_the_reclaim_invalidates_the_fence_of_the_reservation(self):
        token = 'a' * 32
        acquired = self.ask('worker', op='acquire', token=token, run_id='ghost',
                            backend='cursor', provider=None)
        fence = acquired.get('fence')
        self.assertIsInstance(fence, int)
        self.expire(token)
        self.sweep()
        refused = self.ask('worker', op='validate', run_id='ghost', token=token, fence=fence)
        self.assertFalse(refused.get('ok'), refused)
        # The fence of a reclaimed reservation is never handed out again.
        fresh = self.ask('worker', op='acquire', token='b' * 32, run_id='ghost',
                         backend='cursor', provider=None)
        self.assertTrue(fresh.get('admitted'), fresh)
        self.assertGreater(fresh['fence'], fence)

    def test_a_renewed_lease_is_not_reclaimed(self):
        token = 'a' * 32
        self.assertTrue(self.ask('worker', op='acquire', token=token, run_id='alive',
                                 backend='cursor', provider=None)['admitted'])
        renewed = self.ask('worker', op='renew', token=token, run_id='alive')
        self.assertTrue(renewed.get('ok'), renewed)
        self.sweep()
        self.assertEqual([row['token'] for row in self.rows()], [token],
                         'a lease which was renewed was reclaimed')

    def test_a_reservation_is_never_reclaimed_twice(self):
        token = 'a' * 32
        self.assertTrue(self.ask('worker', op='acquire', token=token, run_id='ghost',
                                 backend='cursor', provider=None)['admitted'])
        self.expire(token)
        before = len(self.audit_rows())
        self.sweep()
        self.assertEqual(len(self.audit_rows()) - before, 1,
                         'the reclaim left no audit record (Refs #2160)')
        self.sweep()
        self.assertEqual(len(self.audit_rows()) - before, 1,
                         'the same reservation was reclaimed twice')
        self.assertEqual(self.rows(), [])

    def test_the_reclaim_and_the_release_of_one_reservation_never_count_twice(self):
        token = 'a' * 32
        self.assertTrue(self.ask('worker', op='acquire', token=token, run_id='ghost',
                                 backend='cursor', provider=None)['admitted'])
        self.expire(token)
        before = len(self.audit_rows())
        answers = []

        def release():
            answers.append(self.ask('worker', op='release', token=token, run_id='ghost'))

        def sweep():
            answers.append(self.sweep())

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda job: job(), [release, sweep, sweep]))
        self.assertEqual(self.rows(), [], 'the reservation survived the race')
        self.assertLessEqual(len(self.audit_rows()) - before, 1,
                             'the reservation was reclaimed more than once')
        for answer in answers:
            for number in numbers(answer):
                self.assertGreaterEqual(number, 0, 'a negative count: %r' % (answer,))

    def test_a_failing_audit_rolls_the_reclaim_back(self):
        token = 'a' * 32
        self.assertTrue(self.ask('worker', op='acquire', token=token, run_id='first',
                                 backend='cursor', provider=None)['admitted'])
        self.expire(token)
        self.sweep()
        tables = self.audit_tables()
        self.assertTrue(tables, 'the coordinator wrote no audit trail (Refs #2160)')
        second = 'b' * 32
        self.assertTrue(self.ask('worker', op='acquire', token=second, run_id='second',
                                 backend='cursor', provider=None)['admitted'])
        self.expire(second)
        # The audit of the reclaim cannot be written: the reclaim must not
        # survive it (a reclaim without an audit record is not allowed).
        for table in tables:
            with sqlite3.connect(self.db_path) as db:
                db.execute("CREATE TRIGGER poison_audit_%s BEFORE INSERT ON %s "
                           "BEGIN SELECT RAISE(ABORT, 'audit write failed'); END" % (table, table))
        self.ask('broker', op='status')
        self.assertTrue(any(row['token'] == second for row in self.rows()),
                        'the reservation was reclaimed although the audit write failed')

    def test_a_lease_inside_the_grace_period_is_not_reclaimed(self):
        self.env.update(grace_env(3600))
        token = 'a' * 32
        self.assertTrue(self.ask('worker', op='acquire', token=token, run_id='grace',
                                 backend='cursor', provider=None)['admitted'])
        # The lease ran out a moment ago: the grace period is still running.
        with sqlite3.connect(self.db_path) as db:
            db.execute('UPDATE reservations SET lease_expires=? WHERE token=?', (time.time(), token))
        self.sweep()
        self.assertEqual([row['token'] for row in self.rows()], [token],
                         'a lease inside the grace period was reclaimed')
        # After the grace period the very same lease is reclaimed.
        self.env.update(grace_env(0))
        with sqlite3.connect(self.db_path) as db:
            db.execute('UPDATE reservations SET lease_expires=? WHERE token=?',
                       (time.time() - 1, token))
        self.sweep()
        self.assertEqual(self.rows(), [], 'the lease was not reclaimed after the grace period')

    # ------------------------------------------------------------------ client

    def guard(self):
        """The guard configuration of the client: the real coordinator."""
        path = self.root / 'guard.json'
        path.write_text(json.dumps({'command': [sys.executable, str(BIN), 'broker']}))
        return path

    def test_a_run_whose_lease_was_reclaimed_cannot_write_its_result_back(self):
        rdir = self.runs / 'fenced'
        rdir.mkdir()
        token = 'f' * 32
        acquired = self.ask('worker', op='acquire', token=token, run_id='fenced',
                            backend='cursor', provider=None)
        fence = acquired.get('fence')
        self.assertIsInstance(fence, int)
        payload = {'op': 'acquire', 'token': token, 'run_id': 'fenced', 'backend': 'cursor',
                   'provider': None, 'fence': fence}
        # The lease runs out and the coordinator reclaims the reservation.
        self.expire(token)
        self.sweep()
        self.assertEqual(self.rows(), [], 'the expired lease was not reclaimed')
        # The run tries to write its result back: the fence refuses it.
        command = [sys.executable, str(BIN), 'worker']
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}):
            out = {'status': 'done'}
            reason = xs.publish_fleet_result(str(rdir), out, [], (command, payload))
        self.assertIn(reason, ('missing', 'token_mismatch', 'fence_mismatch', 'expired'), out)
        self.assertEqual(out.get('status'), 'fenced')
        self.assertEqual(out.get('error_class'), 'fenced')

    def lost_lease_client(self):
        """A run whose lease is lost: the renewal fails, the fence is gone."""
        rdir = self.runs / 'lost-lease'
        rdir.mkdir()
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],
                                 start_new_session=True)
        self.addCleanup(self.kill_child, child)
        xs.set_status(str(rdir), executor_pid=child.pid)

        def answer(_command, payload):
            op = payload.get('op')
            if op == 'acquire':
                return 0, {'ok': True, 'admitted': True, 'fence': 7,
                           'lease_expires': time.time() + 0.3, 'lease_ttl_seconds': 0.3}
            if op == 'renew':
                return 1, {'ok': False,
                           'error': 'reservation owner mismatch or reservation missing'}
            if op == 'validate':
                return 0, {'ok': False, 'valid': False, 'reason': 'missing', 'error': 'missing'}
            raise AssertionError('unexpected coordinator request: %r' % (payload,))

        def call(command, payload):
            code, body = answer(command, payload)
            if code != 0 or body.get('ok') is not True:
                raise RuntimeError('fleet capacity coordinator unavailable: %s'
                                   % (body.get('error') or body.get('reason')
                                      or 'coordinator rejected request'))
            return body

        # The heartbeat of the lease outlives the call which started it, so the
        # stubs stay in place until the end of the test.
        for patcher in (mock.patch.object(xs, 'fleet_guard_command', return_value=['guard']),
                        mock.patch.object(xs, 'fleet_guard_answer', side_effect=answer),
                        mock.patch.object(xs, 'fleet_guard_call', side_effect=call)):
            patcher.start()
            self.addCleanup(patcher.stop)
        admitted = xs.reserve_provider_slot(str(rdir), 'codex', None,
                                            {'concurrency': {'total': 8}})
        self.assertTrue(admitted, 'the run did not get its slot')
        return rdir, child

    def kill_child(self, child):
        """Stop the executor of the fixture."""
        if child.poll() is None:
            try:
                os.killpg(os.getpgid(child.pid), signal.SIGKILL)
            except OSError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def stop_renewal(self, rdir):
        """Stop the heartbeat of a run: the lease is not renewed any more."""
        renewal = xs._FLEET_RENEWALS.pop(os.path.realpath(str(rdir)), None)
        if renewal:
            renewal[0].set()
            renewal[1].join(5)

    def lease_lost_trace(self, rdir):
        """Whether the loss of the lease left a trace in the run."""
        try:
            status = json.loads((pathlib.Path(rdir) / 'status.json').read_text())
        except (OSError, ValueError):
            status = {}
        for key, value in status.items():
            name = key.lower()
            if 'lost' in name:
                return True
            if 'lease' in name and isinstance(value, str) and any(
                    word in value.lower()
                    for word in ('lost', 'expired', 'reclaim', 'missing', 'fenced')):
                return True
        for path in sorted(pathlib.Path(rdir).glob('*.jsonl')):
            text = path.read_text(errors='replace').lower()
            if 'lease' in text and any(word in text for word in ('lost', 'expired', 'reclaim')):
                return True
        return False

    def test_a_run_which_lost_its_lease_stops_its_executor(self):
        rdir, child = self.lost_lease_client()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.05)
            self.assertIsNotNone(child.poll(),
                                 'the executor of the run kept running after its lease was lost')
        finally:
            self.stop_renewal(rdir)
            xs._FLEET_RESERVATIONS.pop(os.path.realpath(str(rdir)), None)

    def test_a_run_which_lost_its_lease_leaves_a_trace(self):
        rdir, child = self.lost_lease_client()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not self.lease_lost_trace(rdir):
                time.sleep(0.05)
            self.assertTrue(self.lease_lost_trace(rdir),
                            'the loss of the lease left no trace in the run')
        finally:
            self.stop_renewal(rdir)
            xs._FLEET_RESERVATIONS.pop(os.path.realpath(str(rdir)), None)

    # ------------------------------------------------------- the terminal paths

    def run_to_terminal_state(self, rdir, rc):
        """Run the executor of a run to its terminal state (the real client path)."""
        def executor(_decision, _state, _prompt, _wd, _rdir, _timeout, _event_file):
            return rc, None, {}, 'the executor failed' if rc else ''

        env = {'OPENSWARM_FLEET_GUARD_CONFIG': str(self.guard()),
               'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}
        with mock.patch.dict(os.environ, env), \
                mock.patch.dict(xs.EXECUTORS, {'pi': executor}), \
                mock.patch.object(xs.procboundary.CgroupBoundary, 'create',
                                  return_value=SafeBoundary()), \
                mock.patch.object(xs, 'start_fleet_release_worker'):
            return xs.run_executor_with_rate_limit(
                'pi', {'backend': 'pi', 'model': 'fake'}, {}, 'task', str(self.root),
                str(rdir), xs.NOW() + 30, {'concurrency': {'total': 8}}, defer_fleet_release=True)

    def rebase_workspace(self, rdir):
        """Rebase the workspace of a run onto a new base (the rebase path).

        ``swrm`` has no separate rebase termination path any more (the one of
        the old openswarm was migrated away with #1894), so the case is
        simulated by a real rebase of the workspace of the run.
        """
        repo = rdir / 'repo'
        repo.mkdir()

        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()

        git('init', '-q')
        git('config', 'user.name', 'test')
        git('config', 'user.email', 'test@test')
        git('checkout', '-q', '-b', 'base')
        (repo / 'file').write_text('base\n')
        git('add', 'file')
        git('commit', '-qm', 'base')
        git('checkout', '-q', '-b', 'swrm/run')
        (repo / 'file').write_text('work\n')
        git('commit', '-qam', 'work')
        git('checkout', '-q', 'base')
        (repo / 'other').write_text('upstream\n')
        git('add', 'other')
        git('commit', '-qm', 'upstream')
        git('checkout', '-q', 'swrm/run')
        git('rebase', 'base')
        self.assertEqual((repo / 'file').read_text(), 'work\n')
        self.assertEqual(git('log', '--oneline').splitlines()[0].split(' ', 1)[1], 'work')

    def test_a_rebase_run_gives_its_slot_back_with_the_lease(self):
        self.check_terminal_path('rebase')

    def test_an_exec_error_run_gives_its_slot_back_with_the_lease(self):
        self.check_terminal_path('exec_error')

    def test_a_budget_exceeded_run_gives_its_slot_back_with_the_lease(self):
        self.check_terminal_path('budget_exceeded')

    def check_terminal_path(self, kind):
        """The slot of a run which ended in `kind` comes back with the lease."""
        rdir = self.runs / kind
        rdir.mkdir()
        if kind == 'rebase':
            token = 'c' * 32
            self.assertTrue(self.ask('broker', op='acquire', token=token, run_id=kind,
                                     backend='cursor', provider=None)['admitted'])
            self.rebase_workspace(rdir)
        else:
            rc = 1 if kind == 'exec_error' else 124
            attempts, final, error = self.run_to_terminal_state(rdir, rc)
            self.assertEqual(error, kind)
            rows = [row for row in self.rows() if row['run_id'] == kind]
            self.assertEqual(len(rows), 1, 'the run did not hold a slot')
            token = rows[0]['token']
            self.stop_renewal(rdir)
        # The release of the run never reached the coordinator (the supervisor
        # died with the run): the lease has to free the slot.
        self.expire(token)
        self.sweep()
        self.assertEqual([row for row in self.rows() if row['run_id'] == kind], [],
                         'the slot of the %s run was not reclaimed by its lease' % kind)
        xs._FLEET_RESERVATIONS.pop(os.path.realpath(str(rdir)), None)


if __name__ == '__main__':
    unittest.main()
