"""Cross-host admission is atomic and uncertain reservations are never expired."""
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
CLI = HERE.parent / 'bin' / 'swrm'
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
                                       'workspace':{'repo':str(repo),'base':base}}}
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

    def test_reaped_stopped_runner_releases_slot(self):
        guard = pathlib.Path(self.temp.name, 'guard.json')
        guard.write_text(json.dumps({'command': [str(BIN), 'broker']}))
        env = {'OPENSWARM_FLEET_GUARD_CONFIG': str(guard), 'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}
        with mock.patch.dict(os.environ, env), mock.patch.dict(xs.EXECUTORS, {'pi': lambda *args: (133, None, {}, 'stopped')}):
            rdir = pathlib.Path(self.temp.name, 'runs', 'reaped')
            rdir.mkdir(parents=True)
            _, _, error = xs.run_executor_with_rate_limit('pi', {'backend': 'pi', 'model': 'fake'}, {}, 'review',
                                                          self.temp.name, str(rdir), xs.NOW() + 15,
                                                          {'concurrency': {'total': 8}})
            self.assertEqual(error, 'risk_blocked')
            self.assertEqual(self.call('broker', op='status')['reservations'], [])

    def test_one_shot_agy_403_releases_slot_for_next_run(self):
        guard = pathlib.Path(self.temp.name, 'guard.json')
        guard.write_text(json.dumps({'command': [str(BIN), 'broker']}))
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
                self.assertEqual(self.call('broker', op='status')['reservations'], [])

    def test_recovery_requires_local_explicit_confirmation(self):
        token = 'a' * 32
        self.assertTrue(self.call('worker', op='acquire', token=token, run_id='orphan',
                                  backend='cursor', provider=None)['admitted'])
        self.assertFalse(self.call('worker', op='recover', host='worker', run_id='orphan',
                                   confirm='all-executor-processes-stopped')['ok'])
        self.assertFalse(self.call('broker', op='recover', host='worker', run_id='orphan')['ok'])
        # A live lease is never taken away: its owner may still be working.
        answer = self.call('broker', op='recover', host='worker', run_id='orphan',
                           confirm='all-executor-processes-stopped')
        self.assertEqual(answer['recovered'], 0)
        self.assertEqual(answer['reason'], 'lease not expired')
        self.assertEqual(len(self.call('worker', op='status')['reservations']), 1)
        # Only the owner of a reservation frees its own slot.
        self.assertTrue(self.call('worker', op='release', token=token, run_id='orphan')['released'])
        self.assertEqual(self.call('worker', op='status')['reservations'], [])

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
        answer = self.call('broker', op='recover', host='local', run_id='local-turn',
                           confirm='all-executor-processes-stopped')
        self.assertEqual(answer['recovered'], 0)
        self.assertEqual(answer['reason'], 'lease not expired')
        self.assertTrue(self.call('local', op='release', token='1' * 32, run_id='local-turn')['released'])
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

    def test_early_agy_result_releases_one_shot_reservation(self):
        rdir = os.path.join(self.temp.name, 'runs', 'early-agy')
        os.makedirs(rdir)
        fake = mock.Mock(return_value=(1, None, {'usage':{}, 'finished_reason':'print_timeout'},
                                      'agy turn ended early: print_timeout'))
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': self.temp.name}), \
             mock.patch.object(xs, 'fleet_guard_command', return_value=[str(BIN), 'broker']), \
             mock.patch.dict(xs.EXECUTORS, {'agy':fake}):
            attempts, final, error = xs.run_executor_with_rate_limit(
                'agy', {'backend':'agy', 'model':'gemini-3.8-flash-medium', 'effort':None}, {}, 'task', self.temp.name, rdir,
                xs.NOW() + 30, xs.load_policy())
        self.assertEqual(error, 'exec_error')
        self.assertIsNone(final)
        self.assertEqual(self.call('broker', op='status')['reservations'], [])

    def test_lease_ttl_follows_the_budget_tier_of_the_run(self):
        """The TTL of a lease follows the budget of its run (Refs #1792)."""
        policy = {'fleet': {'lease': {}}}
        self.assertEqual(xs.fleet_lease_ttl_seconds(policy, 3), 600)
        self.assertEqual(xs.fleet_lease_ttl_seconds(policy, 15), 1200)
        self.assertEqual(xs.fleet_lease_ttl_seconds(policy, 40), 2700)
        # A run without a known budget uses the default of the policy.
        self.assertEqual(xs.fleet_lease_ttl_seconds(policy, None), 900)
        # A run whose budget exceeds every tier gets its budget plus the grace.
        self.assertEqual(xs.fleet_lease_ttl_seconds(policy, 90), 90 * 60 + 300)


# A run of the client which holds a slot.  The parent kills it to show that the
# lease of a crashed run expires and its slot returns to the pool.
LEASE_RUNNER = """
import importlib.machinery, importlib.util, json, os, sys, time
loader = importlib.machinery.SourceFileLoader("swrm", sys.argv[1])
spec = importlib.util.spec_from_loader("swrm", loader)
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
# its executor before the coordinator may hand the slot to another run (B2).
LEASE_RUNNER_WITH_EXECUTOR = """
import importlib.machinery, importlib.util, json, os, subprocess, sys, time
loader = importlib.machinery.SourceFileLoader("swrm", sys.argv[1])
spec = importlib.util.spec_from_loader("swrm", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
policy = json.loads(sys.argv[2])
rdir = os.path.join(os.environ["OPENSWARM_HOME"], "runs", "in-flight")
admitted = mod.reserve_provider_slot(rdir, "codex", None, policy, keep_waiting=lambda: True)
executor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                            start_new_session=True)
mod.set_status(rdir, executor_pid=executor.pid)
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
    """A reservation is a lease: it is renewed while the run lives and reclaimed when it expires.

    The lease mechanism is the structural fix of the ghost reservations of
    #1792: the reclaim of an expired lease belongs to the coordinator alone and
    does not depend on any termination path of a run.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=self.temp.name)

    def call(self, authenticated_host, env=None, **payload):
        argv = [str(BIN), authenticated_host]
        if os.name == "nt":
            argv = [sys.executable, *argv]
        p = subprocess.run(argv, input=json.dumps(payload), text=True,
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
                           ('e' * 32, 'broker', 'legacy', 'codex', None, created))
        connection.commit()
        connection.close()

    def test_expired_lease_returns_the_slot_to_the_pool(self):
        """An expired lease is reclaimed by the coordinator and leaves an audit record."""
        token = 'a' * 32
        admitted = self.call('broker', op='acquire', token=token, run_id='ghost',
                             backend='codex', provider=None, ttl_seconds=2)
        self.assertTrue(admitted['admitted'])
        self.assertGreater(admitted['expires_at'], time.time())
        # While the lease lives, the slot is taken.
        blocked = self.call('broker', op='acquire', token='b' * 32, run_id='next',
                            backend='codex', provider=None)
        self.assertFalse(blocked['admitted'])
        self.assertIn('codex', blocked['blocked'])
        time.sleep(2.2)
        # The expired lease is reclaimed and the slot serves the next run.
        answer = self.call('broker', op='acquire', token='b' * 32, run_id='next',
                           backend='codex', provider=None)
        self.assertTrue(answer['admitted'])
        self.assertEqual([item['run_id'] for item in answer['expired']], ['ghost'])
        audit = (pathlib.Path(self.temp.name) / 'lease-audit.jsonl').read_text().splitlines()
        self.assertTrue(any('lease-expired' in line and 'ghost' in line for line in audit))

    def test_renew_extends_the_lease_of_a_live_run(self):
        """A live run renews its lease: the slot outlives the first expiry."""
        token = 'c' * 32
        admitted = self.call('broker', op='acquire', token=token, run_id='live',
                             backend='codex', provider=None, ttl_seconds=2)
        self.assertTrue(admitted['admitted'])
        renewed = self.call('broker', op='renew', token=token, run_id='live', ttl_seconds=30)
        self.assertTrue(renewed['renewed'])
        self.assertGreater(renewed['expires_at'], admitted['expires_at'])
        time.sleep(2.2)
        self.assertEqual(len(self.call('broker', op='status')['reservations']), 1)
        # Only the owner of a reservation may renew it.
        self.assertFalse(self.call('worker', op='renew', token=token, run_id='live')['ok'])

    def test_renewal_does_not_resurrect_a_reclaimed_lease(self):
        """A renewal after the expiry does not bring the slot back."""
        token = 'd' * 32
        self.assertTrue(self.call('broker', op='acquire', token=token, run_id='gone',
                                  backend='codex', provider=None, ttl_seconds=1)['admitted'])
        time.sleep(1.2)
        self.call('broker', op='status')
        answer = self.call('broker', op='renew', token=token, run_id='gone', ttl_seconds=30)
        self.assertFalse(answer['renewed'])
        self.assertEqual(answer['reason'], 'unknown reservation')
        self.assertEqual(len(self.call('broker', op='status')['reservations']), 0)

    def test_legacy_reservation_without_lease_fields_is_migrated(self):
        """An old row without lease fields gets a fallback expiry instead of living forever."""
        self.legacy_database(time.time())
        env = dict(self.env, OPENSWARM_FLEET_LEASE_LEGACY_TTL_SECONDS='1')
        row = self.call('broker', op='status', env=env)['reservations'][0]
        self.assertIsNotNone(row['expires_at'])
        self.assertGreater(row['expires_at'], time.time())
        # The migrated lease expires like every other lease: the slot returns.
        time.sleep(1.2)
        self.assertEqual(len(self.call('broker', op='status', env=env)['reservations']), 0)

    def test_legacy_reservation_keeps_its_slot_during_the_migration_window(self):
        """A live old run keeps its slot while the fleet rolls out the lease."""
        self.legacy_database(time.time() - 30)
        row = self.call('broker', op='status')['reservations'][0]
        self.assertIsNotNone(row['expires_at'])
        # The migration window covers the longest run a client may hold.
        self.assertGreater(row['expires_at'], time.time() + 1800)
        self.assertEqual(row['lease_ttl'], 3600.0)

    def test_legacy_ghost_is_reclaimed_with_the_migration_window(self):
        """An old row of a run which is long gone is reclaimed with the same rule."""
        self.legacy_database(time.time() - 7200)
        self.assertEqual(self.call('broker', op='status')['reservations'], [])
        audit = self.call('broker', op='audit')['audit']
        self.assertTrue(any(item['event'] == 'lease-expired' and item['run_id'] == 'legacy'
                            for item in audit))

    def test_mixed_legacy_and_leased_reservations_are_counted_together(self):
        """An old row and a new lease hold their slots together and are reclaimed apart."""
        self.legacy_database(time.time() - 30)
        self.assertTrue(self.call('broker', op='acquire', token='4' * 32, run_id='new',
                                  backend='pi', provider=None, ttl_seconds=1)['admitted'])
        rows = self.call('broker', op='status')['reservations']
        self.assertEqual(sorted(row['run_id'] for row in rows), ['legacy', 'new'])
        # The old row still holds its slot: the cap sees both reservations.
        blocked = self.call('broker', op='acquire', token='5' * 32, run_id='next',
                            backend='codex', provider=None)
        self.assertFalse(blocked['admitted'])
        self.assertIn('codex', blocked['blocked'])
        # The new lease is reclaimed, the protected old row stays.
        time.sleep(1.2)
        self.call('broker', op='status')
        rows = self.call('broker', op='status')['reservations']
        self.assertEqual([row['run_id'] for row in rows], ['legacy'])

    def test_renewal_does_not_shorten_the_migration_window(self):
        """A new client which adopts an old reservation does not cut its window short."""
        self.legacy_database(time.time() - 30)
        before = self.call('broker', op='status')['reservations'][0]['expires_at']
        renewed = self.call('broker', op='renew', token='e' * 32, run_id='legacy', ttl_seconds=60)
        self.assertTrue(renewed['renewed'])
        self.assertGreaterEqual(renewed['expires_at'], before)

    def test_sweep_loop_reclaims_an_expired_lease_without_any_request(self):
        """The coordinator frees the slot of a crashed run on its own."""
        token = '3' * 32
        # The sweeper of this test owns the sweep, so the deployment of the
        # coordinator does not start a second one.
        acquire_env = dict(self.env, OPENSWARM_FLEET_CAPACITY_SWEEP='0')
        self.assertTrue(self.call('broker', op='acquire', token=token, run_id='ghost',
                                  backend='codex', provider=None, ttl_seconds=1,
                                  env=acquire_env)['admitted'])
        env = dict(self.env, OPENSWARM_FLEET_CAPACITY_SWEEP_INTERVAL='0.2')
        sweeper = subprocess.Popen([str(BIN), '--sweep-loop', 'broker'], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # No request reaches the coordinator: the sweep alone frees the slot.
            self.wait_for(lambda: not self.coordinator_rows(), 'the sweep to reclaim the lease')
        finally:
            sweeper.terminate()
            sweeper.wait(timeout=10)
        audit = self.call('broker', op='audit')['audit']
        self.assertTrue(any(item['event'] == 'lease-expired' and item['run_id'] == 'ghost'
                            for item in audit))
        lines = (pathlib.Path(self.temp.name) / 'lease-audit.jsonl').read_text().splitlines()
        self.assertTrue(any('lease-expired' in line and 'ghost' in line for line in lines))

    def test_the_coordinator_sweeps_the_expired_leases_without_any_request(self):
        """B1: the deployment of the coordinator frees the slot of a crashed run on its own.

        The sweeper belongs to the coordinator: the request which took the slot
        starts it, so the reclaim of an expired lease does not wait for another
        request and does not depend on an operator wiring a timer up.
        """
        env = dict(self.env, OPENSWARM_FLEET_CAPACITY_SWEEP_INTERVAL='0.2')
        token = '5' * 32
        self.assertTrue(self.call('broker', op='acquire', token=token, run_id='ghost',
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
        """B1: the coordinator starts one sweeper of the expired leases by itself."""
        answer = self.call('broker', op='status')
        self.assertTrue(answer['sweeper']['running'])
        # The sweeper writes its pid next to the coordinator database.
        self.wait_for(lambda: self.call('broker', op='status')['sweeper'].get('pid'),
                      'the pid of the sweeper')
        pid = self.call('broker', op='status')['sweeper']['pid']
        self.assertTrue(xs.pid_alive(pid))
        # A later request does not start a second sweeper of the same coordinator.
        self.assertEqual(self.call('broker', op='status')['sweeper']['pid'], pid)

    def test_a_disconnected_client_stops_its_executor_before_a_new_run_takes_the_slot(self):
        """B2: a run which loses the coordinator stops before the slot is given away.

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
        config.write_text(json.dumps({'command': [sys.executable, str(guard), str(BIN), 'broker']}))
        env = dict(os.environ, OPENSWARM_HOME=str(home),
                   OPENSWARM_FLEET_GUARD_CONFIG=str(config),
                   OPENSWARM_FLEET_CAPACITY_DIR=str(root / 'capacity'),
                   OPENSWARM_FLEET_CAPACITY_SWEEP_INTERVAL='0.2')
        policy = {'concurrency': {'total': 8},
                  'fleet': {'lease': {'ttl_seconds': 6, 'renew_interval_seconds': 0.5}}}
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
            executor_pid = status['executor_pid']
            self.wait_for(lambda: not xs.pid_alive(executor_pid) or xs.process_exited(executor_pid),
                          'the executor of the lost run to be stopped')
            # The lease of the lost run expires and the next run takes the slot.
            self.wait_for(lambda: not rows(), 'the lease of the lost run to expire')
            answer = self.call('broker', op='acquire', token='e' * 32, run_id='next',
                               backend='codex', provider=None, env=env)
            self.assertTrue(answer['admitted'])
            # The old executor is gone: the two runs never share the slot.
            self.assertFalse(xs.pid_alive(executor_pid) and not xs.process_exited(executor_pid))
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
        """B2: a slow answer does not push the deadline of the lease beyond its TTL.

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
        """B2: a renewal which hangs cannot keep the run alive past its deadline."""
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
        self.assertTrue(self.call('broker', op='acquire', token='6' * 32, run_id='audited',
                                  backend='codex', provider=None, ttl_seconds=60)['admitted'])
        released = self.call('broker', op='release', token='6' * 32, run_id='audited')
        self.assertTrue(released['released'])
        # The failing mirror is reported instead of ignored.
        self.assertIn('audit_error', released)
        # The database keeps the authoritative trail of the committed change.
        audit = self.call('broker', op='audit')['audit']
        self.assertTrue(any(item['event'] == 'released' and item['run_id'] == 'audited'
                            for item in audit))

    def test_rolled_back_transaction_leaves_no_audit_record(self):
        """A failing request rolls its reclaim back: no slot is freed, no record is left."""
        token = '7' * 32
        self.assertTrue(self.call('broker', op='acquire', token=token, run_id='rollback',
                                  backend='codex', provider=None, ttl_seconds=1)['admitted'])
        time.sleep(1.2)
        # The request fails after the reclaim: the transaction is rolled back.
        answer = self.call('broker', op='acquire', token='8' * 32, run_id='invalid',
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
        self.assertTrue(self.call('broker', op='acquire', token=token, run_id='race',
                                  backend='codex', provider=None, ttl_seconds=1)['admitted'])
        time.sleep(1.2)
        # The lease expired: the reclaim of the coordinator frees the slot and
        # the release of the owner finds nothing left to free.
        answer = self.call('broker', op='release', token=token, run_id='race')
        self.assertFalse(answer['released'])
        self.assertEqual(self.coordinator_rows(), [])
        events = [item['event'] for item in self.audit_rows() if item['run_id'] == 'race']
        self.assertEqual(events, ['lease-expired'])

    def test_lost_lease_stops_the_executor_of_the_run(self):
        """B2: a run whose lease was reclaimed stops its executor instead of running on."""
        rdir = pathlib.Path(self.temp.name, 'runs', 'lost')
        rdir.mkdir(parents=True)
        executor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                    start_new_session=True)
        self.addCleanup(executor.wait)
        (rdir / 'status.json').write_text(json.dumps({
            'id': 'lost', 'state': 'running', 'executor_pid': executor.pid}))
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
        self.wait_for(lambda: xs.process_exited(executor.pid) or not xs.pid_alive(executor.pid),
                      'the executor to be stopped')

    def test_unreachable_coordinator_beyond_the_ttl_stops_the_executor(self):
        """B2: a network outage longer than the lease stops the run instead of overbooking."""
        rdir = pathlib.Path(self.temp.name, 'runs', 'offline')
        rdir.mkdir(parents=True)
        executor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                    start_new_session=True)
        self.addCleanup(executor.wait)
        (rdir / 'status.json').write_text(json.dumps({
            'id': 'offline', 'state': 'running', 'executor_pid': executor.pid}))
        policy = {'fleet': {'lease': {'ttl_seconds': 0.6, 'renew_interval_seconds': 0.1}}}
        with mock.patch.object(xs, 'fleet_guard_call', side_effect=RuntimeError('coordinator unavailable')):
            heartbeat = xs.start_fleet_lease_heartbeat(
                str(rdir), ['guard'], {'run_id': 'offline', 'token': 'b' * 32, 'ttl_seconds': 0.6}, policy)
            self.addCleanup(heartbeat.stop)
            self.wait_for(lambda: not heartbeat.thread.is_alive(), 'the heartbeat to stop the run')
        status = json.loads((rdir / 'status.json').read_text())
        self.assertEqual(status['fleet_lease_state'], 'lost')
        self.assertIn('coordinator unreachable', status['fleet_lease_error'])
        self.wait_for(lambda: xs.process_exited(executor.pid) or not xs.pid_alive(executor.pid),
                      'the executor to be stopped')

    def test_every_termination_path_leaves_the_slot_to_the_lease(self):
        """rebase, exec_error and budget_exceeded all lose their slot with the lease.

        The slot of a run which ended is freed by the coordinator alone: the
        reclaim does not depend on the termination path of the run.
        """
        terminations = {
            'rebase': {'state': 'finished', 'status': 'failed', 'error_class': 'rebase'},
            'exec_error': {'state': 'finished', 'status': 'failed', 'error_class': 'exec_error'},
            'budget_exceeded': {'state': 'finished', 'status': 'budget_exceeded',
                                'error_class': 'budget_exceeded'},
        }
        for outcome, recorded in terminations.items():
            with self.subTest(outcome=outcome):
                with tempfile.TemporaryDirectory() as directory:
                    env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=directory)
                    run_id = f'run-{outcome}'
                    token = 'f' * 32
                    answer = self.call('broker', op='acquire', token=token, run_id=run_id,
                                       backend='codex', provider=None, ttl_seconds=2, env=env)
                    self.assertTrue(answer['admitted'])
                    runs = pathlib.Path(directory, 'runs', run_id)
                    runs.mkdir(parents=True)
                    (runs / 'status.json').write_text(json.dumps({
                        'id': run_id, 'fleet_host': 'broker', 'fleet_reservation_token': token,
                        'fleet_reserved_at': time.time(), **recorded}))
                    # The run ended, but no termination path released the slot.
                    self.assertEqual(len(self.call('broker', op='status', env=env)['reservations']), 1)
                    time.sleep(2.2)
                    # The lease expired: the coordinator reclaimed the slot.
                    self.assertEqual(len(self.call('broker', op='status', env=env)['reservations']), 0)
                    self.assertTrue(self.call('broker', op='acquire', token='1' * 32, run_id='next',
                                              backend='codex', provider=None, env=env)['admitted'])

    def test_owner_reads_the_token_of_its_own_run_only(self):
        """A record without a token releases its slot by the token of its own reservation."""
        token = '9' * 32
        self.assertTrue(self.call('broker', op='acquire', token=token, run_id='owner-run',
                                  backend='codex', provider=None)['admitted'])
        self.assertEqual(self.call('broker', op='token', run_id='owner-run')['token'], token)
        # Another host learns nothing about a reservation it does not own.
        self.assertIsNone(self.call('worker', op='token', run_id='owner-run')['token'])
        self.assertIsNone(self.call('broker', op='token', run_id='other-run')['token'])

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
            xs.set_status(str(rdir), state='finished')
            self.wait_for(lambda: not heartbeat.thread.is_alive(), 'the heartbeat to stop')
            renewals = len(calls)
            time.sleep(0.6)
            self.assertEqual(len(calls), renewals)

    def test_killed_in_flight_run_releases_its_slot_within_the_ttl(self):
        """A killed run loses its slot with the lease: the next run takes the slot."""
        home = pathlib.Path(self.temp.name, 'home')
        home.mkdir()
        guard = pathlib.Path(self.temp.name, 'guard.json')
        guard.write_text(json.dumps({'command': [str(BIN), 'broker']}))
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
                if len(self.call('broker', op='status')['reservations']) == 1:
                    break
                time.sleep(0.1)
            else:
                self.fail(f'the in-flight run did not take its slot: {log.read_text()}')
            # The heartbeat of the live run renews the lease: the slot survives
            # more than one TTL.
            time.sleep(3)
            self.assertEqual(len(self.call('broker', op='status')['reservations']), 1)
            os.kill(child.pid, signal.SIGKILL)
            child.wait()
            self.wait_for(lambda: not self.call('broker', op='status')['reservations'],
                          'the lease of the killed run to expire')
            # The freed slot serves the next run.
            self.assertTrue(self.call('broker', op='acquire', token='9' * 32, run_id='next',
                                      backend='codex', provider=None)['admitted'])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


if __name__ == '__main__':
    unittest.main()

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


# The rebase card of the dispatcher: a lane run which rebases a PR.  The swrm
# tree has no rebase command of its own, so the rebase path is a lane run with
# the card of the dispatcher.
REBASE_CARD = """Rebase-PR: #1792 fleet/A4-issue1792-20261103-114416-2bc809

PR #1792 head `504d685e0` conflicts with main. Start from branch `rebase-pr-1792` (the PR head),
rebase onto main, resolve conflicts keeping main's merged changes and this PR's intent,
run the tests, and commit.
"""


class FleetLeaseLaneTests(unittest.TestCase):
    """A real lane run gives its slot back with the lease, not with its termination path.

    The runs are real lane turns: the terminal state is produced by the lane code
    of `swrm`, and the slot is freed by the lease of the coordinator alone.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oslane-", dir=tempfile.gettempdir())
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.data = self.root / "data"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Lease Test")
        self.git("config", "user.email", "lease-test@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        fixture = HERE / "fixtures" / "fake_app_server.py"
        fixture.chmod(0o755)
        guard = self.root / "guard.py"
        guard.write_text(GUARD_WITHOUT_RELEASE)
        guard.chmod(0o755)
        config = self.root / "guard.json"
        config.write_text(json.dumps({'command': [sys.executable, str(guard), str(BIN), 'broker']}))
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.data),
                        OPENSWARM_FLEET_GUARD_CONFIG=str(config),
                        OPENSWARM_FLEET_CAPACITY_DIR=str(self.root / 'capacity'),
                        OPENSWARM_FLEET_CAPACITY_SWEEP_INTERVAL='0.5')
        (self.data / 'lanes').mkdir(parents=True)
        (self.data / 'lanes.json').write_text(json.dumps({'lanes': [{
            'name': 'L1', 'backend': 'codex', 'model': 'fake-model', 'effort': 'xhigh',
            'repo': str(self.repo), 'binary': str(fixture)}]}))
        # A short lease: the slot returns within the TTL of the run.
        (self.data / 'policy.local.json').write_text(json.dumps({'fleet': {'lease': {
            'ttl_seconds': 4, 'renew_interval_seconds': 0.5}}}))
        self.lane = 'L1'

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
            return json.loads((self.data / 'runs' / run_id / 'status.json').read_text())
        except (OSError, ValueError):
            return {}

    def run_result(self, run_id):
        try:
            return json.loads((self.data / 'runs' / run_id / 'result.json').read_text())
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
        self.assert_slot_returns_with_the_lease(REBASE_CARD, 'done', None)
