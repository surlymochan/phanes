"""Hub bridge must preserve repository, acknowledgement, and recovery boundaries."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MOD = runpy.run_path(str(ROOT / 'bin/hub-swrm-bridge'))
G = MOD['run_submit'].__globals__

class HubBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.repo = self.path / 'repo'; self.repo.mkdir()
        self.workspace = self.path / 'workspace'; self.workspace.mkdir()
        def git(*args): subprocess.run(['git','-C',str(self.repo),*args],check=True,capture_output=True)
        git('init','-q'); (self.repo/'a').write_text('x')
        git('add','a'); git('-c','user.name=test','-c','user.email=test@test','commit','-qm','init')
        self.card = self.path/'card'; self.card.write_text('Repository: openbot\nExecutor-Type: mechanical\nDifficulty: 1\nAdd a function.\n')
        self.config = self.path/'config.json'
        self.config.write_text(json.dumps({'workspace':str(self.workspace),'repo':str(self.repo),
                                                'hosts':['executor-local','executor-remote'],
                                                'ssh_host':'gateway.example.invalid'}))
        self.args = ['--detach','--cwd',str(self.workspace),'--task-file',str(self.card),'--title','test',
                     '--budget-minutes','4']
        self.patches = [mock.patch.dict(G, {'config_path':self.config, 'store':self.path/'store'})]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    def test_configured_box_host_routes_local_lifecycle_directly(self):
        info = {'id':'box-run', 'result':{}}
        replies = [info, {'ok':True}, info, {'bundle':'AA=='}]
        command = mock.Mock(side_effect=replies)
        legacy = mock.Mock(side_effect=AssertionError('box routed to legacy gateway'))
        with mock.patch.dict(os.environ, {'OPENSWARM_HUB_LOCAL_ROUTER':'1',
                                          'OPENSWARM_AUTO_LOCAL_HOST':'box',
                                          'OPENSWARM_AUTO_FLEET_HOSTS':'["gores1","gores2"]'}), \
             mock.patch.dict(G, {'local_command':command, 'gateway':legacy}):
            for op in ('info', 'stop', 'bundle'):
                MOD['dispatch_gateway']({'op':op,'id':'box-run','host':'box'})
        self.assertEqual([call.args[0] for call in command.call_args_list], [
            [G['native'],'fleet','_run-info','box-run'],
            [G['native'],'stop','box-run'],
            [G['native'],'fleet','_run-info','box-run'],
            [sys.executable,str(ROOT / 'bin/fleet-result-bundle')],
        ])
        self.assertEqual(command.call_args.args[1], info)
        legacy.assert_not_called()

    def test_direct_submit_and_mapped_lifecycle_preserve_remote_host(self):
        reply = {'id':'remote-run','state':'queued','host':'gores2','backend':'pi','model':'MiniMax'}
        def command(argv, payload=None, timeout=100):
            if argv[:2] == [G['native'],'auto']:
                self.assertEqual(Path(argv[argv.index('--task-file') + 1]).read_text(), self.card.read_text())
                self.assertGreater(timeout, 2 * 35 + 45)
                return reply
            self.assertEqual(argv[:5], [G['native'],'fleet','exec','gores2','--'])
            self.assertIn(argv[5:], [['fleet','_run-info','remote-run'], ['stop','remote-run']])
            return {'id':'remote-run','result':{'result':{'summary':'done'}}}
        legacy = mock.Mock(side_effect=AssertionError('direct handoff reached legacy gateway'))
        with mock.patch.dict(os.environ, {'OPENSWARM_HUB_LOCAL_ROUTER':'1',
                                          'OPENSWARM_AUTO_LOCAL_HOST':'box',
                                          'OPENSWARM_AUTO_FLEET_HOSTS':'["gores1","gores2"]'}), \
             mock.patch.dict(G, {'local_command':command, 'gateway':legacy}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            MOD['run_submit'](self.args)
            self.assertEqual(json.loads(output.getvalue())['executor_host'], 'gores2')
            for op in ('info','stop'):
                output.seek(0); output.truncate()
                self.assertTrue(MOD['mapped']('remote-run', op))
        db = MOD['db_open']()
        self.assertEqual(db.execute('select host,route,state from handoffs').fetchone(), ('gores2','fleet','accepted'))
        db.close()
        legacy.assert_not_called()

    def test_recorded_fleet_route_fails_closed_after_configuration_changes(self):
        db = MOD['db_open']()
        db.execute('INSERT INTO handoffs VALUES (?,?,?,?,?,?)',
                   ('request','accepted','remote-run','gores2',0,'fleet'))
        db.close()
        legacy = mock.Mock(side_effect=AssertionError('recorded fleet handoff changed routes'))
        command = mock.Mock(side_effect=AssertionError('unconfigured route executed a command'))
        changes = [
            {'OPENSWARM_HUB_LOCAL_ROUTER':'0', 'OPENSWARM_AUTO_FLEET_HOSTS':'["gores1","gores2"]'},
            {'OPENSWARM_HUB_LOCAL_ROUTER':'1', 'OPENSWARM_AUTO_FLEET_HOSTS':'["gores1"]'},
            {'OPENSWARM_HUB_LOCAL_ROUTER':'1', 'OPENSWARM_AUTO_FLEET_HOSTS':'[]'},
        ]
        for env in changes:
            with self.subTest(env=env), \
                 mock.patch.dict(os.environ, dict(env, OPENSWARM_AUTO_LOCAL_HOST='box')), \
                 mock.patch.dict(G, {'gateway':legacy, 'local_command':command}):
                for op in ('info', 'stop'):
                    with self.subTest(op=op), self.assertRaises(RuntimeError):
                        MOD['mapped']('remote-run', op)
                with self.assertRaises(RuntimeError):
                    MOD['fetch_result']('remote-run', 'gores2',
                                        {'branch':'swrm/remote-run', 'commit':'f' * 40}, 'fleet')
        legacy.assert_not_called()
        command.assert_not_called()

    def test_direct_rejection_allows_retry_but_lost_ack_keeps_pending(self):
        env = {'OPENSWARM_HUB_LOCAL_ROUTER':'1', 'OPENSWARM_AUTO_LOCAL_HOST':'box',
               'OPENSWARM_AUTO_FLEET_HOSTS':'["gores1","gores2"]'}
        with mock.patch.dict(os.environ, env), \
             mock.patch.dict(G, {'local_command':mock.Mock(side_effect=MOD['NoAvailableLane']('no available fleet lane'))}):
            with self.assertRaisesRegex(RuntimeError, 'no available fleet lane.*retry'):
                MOD['run_submit'](self.args)
        with mock.patch.dict(os.environ, env), \
             mock.patch.dict(G, {'local_command':mock.Mock(side_effect=MOD['PreDispatchRejected']('fleet lane discovery unavailable: gores1,gores2'))}):
            with self.assertRaisesRegex(RuntimeError, 'fleet lane discovery unavailable:'):
                MOD['run_submit'](self.args)
        with mock.patch.dict(os.environ, env), \
             mock.patch.dict(G, {'local_command':mock.Mock(side_effect=RuntimeError('lost acknowledgement'))}):
            with self.assertRaisesRegex(RuntimeError, 'unresolved'):
                MOD['run_submit'](self.args)
            with self.assertRaisesRegex(RuntimeError, 'unresolved'):
                MOD['run_submit'](self.args)
        db = MOD['db_open']()
        self.assertEqual(db.execute('select state,route from handoffs order by created').fetchall(),
                         [('rejected','fleet'), ('rejected','fleet'), ('pending','fleet')])
        db.close()

    def test_old_journal_keeps_legacy_route_when_direct_routing_is_enabled(self):
        store = G['store']; store.mkdir()
        db = sqlite3.connect(str(store / 'dispatch.sqlite'))
        db.execute('CREATE TABLE handoffs (request_id TEXT PRIMARY KEY, state TEXT NOT NULL, run_id TEXT, host TEXT, created REAL NOT NULL)')
        db.execute("INSERT INTO handoffs VALUES ('old-request','accepted','old-run','gores1',0)")
        db.commit(); db.close()
        gateway = mock.Mock(return_value={'id':'old-run','result':None})
        direct = mock.Mock(side_effect=AssertionError('old handoff changed routes'))
        with mock.patch.dict(os.environ, {'OPENSWARM_HUB_LOCAL_ROUTER':'1',
                                          'OPENSWARM_AUTO_LOCAL_HOST':'box',
                                          'OPENSWARM_AUTO_FLEET_HOSTS':'["gores1","gores2"]'}), \
             mock.patch.dict(G, {'gateway':gateway, 'local_gateway':direct}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(MOD['mapped']('old-run', 'info'))
        gateway.assert_called_once_with({'op':'info','id':'old-run','host':'gores1'})
        direct.assert_not_called()

    def test_submit_and_result_keep_executor_host(self):
        def fake(req):
            if req['op']=='submit':
                self.assertEqual((req['type'],req['difficulty']),('mechanical',1))
                return {'id':'run-1','state':'queued','host':'executor-local','backend':'pi','model':'MiniMax'}
            return {'id':'run-1','result':{'result':{'summary':'done'}}}
        with mock.patch.dict(G, {'gateway':fake}), contextlib.redirect_stdout(io.StringIO()) as output:
            MOD['run_submit'](self.args)
            self.assertEqual(json.loads(output.getvalue())['executor_host'],'executor-local')
            output.seek(0); output.truncate()
            self.assertTrue(MOD['mapped']('run-1','info'))
            self.assertIn('Executor host: executor-local',json.loads(output.getvalue())['result']['result']['summary'])

    def test_submit_rejects_unconfigured_executor_host(self):
        reply = {'id':'run-1', 'state':'queued', 'host':'unconfigured.example.invalid'}
        with mock.patch.dict(G, {'gateway':mock.Mock(return_value=reply)}):
            with self.assertRaisesRegex(RuntimeError, 'invalid acknowledgement'):
                MOD['run_submit'](self.args)
        db = MOD['db_open']()
        self.assertEqual(db.execute('select state from handoffs').fetchone(), ('pending',))
        db.close()

    def test_finished_remote_commit_is_fetched_for_local_review(self):
        self.check_finished_remote_commit_is_fetched(direct=False)

    def test_direct_fleet_result_bundle_is_fetched_for_local_review(self):
        self.check_finished_remote_commit_is_fetched(direct=True)

    def check_finished_remote_commit_is_fetched(self, direct):
        remote = self.path / 'remote'
        subprocess.run(['git', 'clone', '-q', str(self.repo), str(remote)], check=True)
        base = subprocess.check_output(['git', '-C', str(remote), 'rev-parse', 'HEAD'], text=True).strip()
        subprocess.run(['git', '-C', str(remote), 'checkout', '-qb', 'swrm/run-1'], check=True)
        (remote / 'a').write_text('remote result')
        subprocess.run(['git', '-C', str(remote), '-c', 'user.name=test', '-c', 'user.email=test@test',
                        'commit', '-qam', 'result'], check=True)
        commit = subprocess.check_output(['git', '-C', str(remote), 'rev-parse', 'HEAD'], text=True).strip()
        info = {'id': 'run-1', 'result': {'branch': 'swrm/run-1', 'commit': commit,
                                           'workspace': {'repo': str(remote), 'base': base},
                                           'result': {'summary': 'done'}}}
        def gateway(req):
            if req['op'] == 'submit':
                return {'id':'run-1','state':'queued','host':'gores1' if direct else 'executor-local','backend':'pi','model':'MiniMax'}
            if req['op'] == 'info':
                return info
            bundle = subprocess.run([sys.executable, str(ROOT / 'bin/fleet-result-bundle')],
                                    input=json.dumps(info), text=True, capture_output=True, check=True)
            return json.loads(bundle.stdout)
        runtime = self.path / 'runtime'; runtime.mkdir()
        (runtime / 'fleet.json').write_text(json.dumps({'hosts': [
            {'name': 'gores1', 'kind': 'ssh', 'ssh': 'configured-executor.example.invalid'}]}))
        def local_command(command, payload=None, timeout=100):
            if command[:2] == [G['native'], 'auto']:
                return gateway({'op':'submit'})
            if command[:6] == [G['native'], 'fleet', 'exec', 'gores1', '--', 'fleet']:
                self.assertEqual(command[6:], ['_run-info', 'run-1'])
                return info
            self.assertEqual(command[:3], ['/usr/bin/ssh', 'configured-executor.example.invalid', '--'])
            self.assertEqual(payload, info)
            # Execute the remote helper locally against the fixture repository;
            # transport itself is mocked, bundle creation and fetch are real.
            bundle = subprocess.run(shlex.split(command[3]), input=json.dumps(payload),
                                    text=True, capture_output=True, check=True)
            return json.loads(bundle.stdout)
        env = {'OPENSWARM_HUB_LOCAL_ROUTER':'1' if direct else '0',
               'OPENSWARM_AUTO_LOCAL_HOST':'box', 'OPENSWARM_AUTO_FLEET_HOSTS':'["gores1","gores2"]',
               'OPENSWARM_HOME': str(runtime)}
        legacy = mock.Mock(side_effect=AssertionError('direct handoff reached legacy gateway')) if direct else gateway
        with mock.patch.dict(os.environ, env), \
             mock.patch.dict(G, {'gateway':legacy, 'local_command':local_command}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            MOD['run_submit'](self.args)
            output.seek(0); output.truncate()
            self.assertTrue(MOD['mapped']('run-1', 'info'))
            result = json.loads(output.getvalue())['result']
        self.assertEqual(result['local_review_ref'], 'refs/swrm/run-1')
        actual = subprocess.check_output(['git', '-C', str(self.repo), 'show', 'refs/swrm/run-1:a'], text=True)
        self.assertEqual(actual, 'remote result')
        self.assertEqual((self.repo / 'a').read_text(), 'x')

    def test_result_fetch_failure_keeps_completed_result_visible(self):
        def gateway(req):
            if req['op'] == 'submit':
                return {'id':'run-1','state':'queued','host':'executor-local','backend':'pi','model':'MiniMax'}
            if req['op'] == 'bundle':
                raise RuntimeError('transport unavailable')
            return {'id':'run-1', 'result':{'branch':'swrm/run-1', 'commit':'f' * 40,
                                            'result':{'summary':'done'}}}
        with mock.patch.dict(G, {'gateway':gateway}), contextlib.redirect_stdout(io.StringIO()) as output:
            MOD['run_submit'](self.args)
            output.seek(0); output.truncate()
            self.assertTrue(MOD['mapped']('run-1', 'info'))
            result = json.loads(output.getvalue())['result']
        self.assertIn('could not be fetched', result['local_review_error'])
        self.assertIn('done', result['result']['summary'])
        self.assertIn('could not fetch', result['result']['summary'])

    def test_lost_ack_blocks_repeated_dispatch(self):
        with mock.patch.dict(G, {'gateway':mock.Mock(side_effect=RuntimeError('lost ack'))}):
            with self.assertRaisesRegex(RuntimeError,'unresolved'): MOD['run_submit'](self.args)
            with self.assertRaisesRegex(RuntimeError,'unresolved'): MOD['run_submit'](self.args)

    def test_no_lane_rejection_allows_later_dispatch(self):
        response = subprocess.CompletedProcess([], 1, json.dumps({'ok':False, 'error':'no available fleet lane'}), '')
        with mock.patch.object(subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'no available fleet lane'):
                MOD['gateway']({'op':'submit'})
        with mock.patch.dict(G, {'gateway':mock.Mock(side_effect=MOD['NoAvailableLane']('no available fleet lane'))}):
            with self.assertRaisesRegex(RuntimeError, 'no available fleet lane.*retry'):
                MOD['run_submit'](self.args)
        with mock.patch.dict(G, {'gateway':mock.Mock(return_value={'id':'run-after-busy','state':'queued',
                                                                  'host':'executor-remote','backend':'agy','model':'x'})}), \
             contextlib.redirect_stdout(io.StringIO()):
            MOD['run_submit'](self.args)
        db = MOD['db_open']()
        self.assertEqual(db.execute("select state from handoffs order by created").fetchall(),
                         [('rejected',), ('accepted',)])
        db.close()

    def test_exhausted_busy_lanes_allow_later_dispatch(self):
        response = subprocess.CompletedProcess([], 1, json.dumps({'ok':False, 'error':'available lanes changed or rejected the task'}), '')
        with mock.patch.object(subprocess, 'run', return_value=response):
            with self.assertRaises(MOD['NoAvailableLane']):
                MOD['gateway']({'op':'submit'})
        with mock.patch.dict(G, {'gateway':mock.Mock(side_effect=MOD['NoAvailableLane']('busy'))}):
            with self.assertRaisesRegex(RuntimeError, 'retry'):
                MOD['run_submit'](self.args)
        db = MOD['db_open']()
        self.assertEqual(db.execute('select state from handoffs').fetchone(), ('rejected',))
        db.close()

    def test_unknown_gateway_error_is_not_exposed(self):
        response = subprocess.CompletedProcess([], 1, json.dumps({'ok':False, 'error':'arbitrary remote text'}), '')
        with mock.patch.object(subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(RuntimeError, '^fleet gateway rejected request$'):
                MOD['gateway']({'op':'submit'})

    def test_gateway_result_host_uses_configured_target_or_local(self):
        for host in ('local', 'gateway.example.invalid', 'unknown.example.invalid'):
            with self.subTest(host=host):
                req = {'op': 'info', 'id': 'run-test', 'host': host}
                payload = {'result': {'id': 'run-test'}} if host == 'gateway.example.invalid' else {'id': 'run-test'}
                reply = subprocess.CompletedProcess([], 0, json.dumps(payload), '')
                with mock.patch.dict(os.environ, {'OPENSWARM_AUTO_SSH_HOST': 'gateway.example.invalid'}), \
                        mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(req))), \
                        mock.patch.object(subprocess, 'run', return_value=reply) as run, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    if host == 'unknown.example.invalid':
                        with self.assertRaises(SystemExit):
                            runpy.run_path(str(ROOT / 'bin/hub-fleet-gateway'))
                        run.assert_not_called()
                    else:
                        runpy.run_path(str(ROOT / 'bin/hub-fleet-gateway'))
                        command = run.call_args.args[0]
                        self.assertEqual(command[0].endswith('ssh'), host == 'gateway.example.invalid')
                        if host == 'gateway.example.invalid': self.assertEqual(command[-1], host)
                result = json.loads(output.getvalue())
                self.assertEqual(result['ok'], host != 'unknown.example.invalid')
                if result['ok']: self.assertEqual(result['result']['id'], 'run-test')

    def test_remote_gateway_preserves_known_dispatch_failure(self):
        req = {'op':'submit', 'prompt':'task', 'title':'test', 'base':'a'*40,
               'type':'migration', 'difficulty':1, 'budget':4, 'verify':None}
        result = subprocess.CompletedProcess([], 1, json.dumps({'ok':False, 'error':'no available fleet lane'}), '')
        with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(req))), \
             mock.patch.object(subprocess, 'run', return_value=result), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                runpy.run_path(str(ROOT / 'bin/hub-fleet-gateway'))
        self.assertEqual(json.loads(output.getvalue())['error'], 'no available fleet lane')

    def test_no_lane_through_gateway_then_retry(self):
        fake_home = self.path / 'gateway-home'
        fake_cli = fake_home / '.local/bin/swrm'
        fake_cli.parent.mkdir(parents=True)
        marker = self.path / 'called'
        fake_cli.write_text('#!' + sys.executable + '\n' +
            'import json,pathlib,sys\np=pathlib.Path(' + repr(str(marker)) + ')\n' +
            'if not p.exists():\n p.touch(); print(json.dumps({"ok":False,"error":"no available fleet lane"})); sys.exit(1)\n' +
            'print(json.dumps({"ok":True,"id":"run-after-busy","state":"queued","host":"executor-remote","backend":"agy","model":"x"}))\n')
        fake_cli.chmod(0o755)
        tunnel = self.path / 'gateway-tunnel.py'
        tunnel.write_text('import os,sys\nos.environ["HOME"]=' + repr(str(fake_home)) + '\n' +
                          'import subprocess\nsys.exit(subprocess.call([sys.executable,' + repr(str(ROOT / 'bin/hub-fleet-gateway')) + ']))\n')
        with mock.patch.dict(G, {'transport':[sys.executable, str(tunnel)]}):
            with self.assertRaisesRegex(RuntimeError, 'no available fleet lane.*retry'):
                MOD['run_submit'](self.args)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                MOD['run_submit'](self.args)
            self.assertEqual(json.loads(output.getvalue())['id'], 'run-after-busy')
        db = MOD['db_open']()
        self.assertEqual(db.execute('select state from handoffs order by created').fetchall(),
                         [('rejected',), ('accepted',)])
        db.close()

    def test_review_profile_reaches_gateway(self):
        self.card.write_text('Repository: openbot\nExecutor-Type: review\nDifficulty: 2\nReview the code.\n')
        gateway = mock.Mock(return_value={'id':'run-review','state':'queued','host':'executor-remote','backend':'copilot','model':'x'})
        with mock.patch.dict(G, {'gateway':gateway}), contextlib.redirect_stdout(io.StringIO()):
            MOD['run_submit'](self.args)
        self.assertEqual(gateway.call_args.args[0]['type'], 'review')

    def test_unknown_workspace_and_dirty_repo_fail_before_dispatch(self):
        with mock.patch.dict(G, {'gateway':mock.Mock()}) as _:
            bad=list(self.args); bad[bad.index('--cwd')+1]=str(self.path)
            with self.assertRaisesRegex(ValueError,'mapping'): MOD['run_submit'](bad)
            (self.repo/'a').write_text('modified')
            with self.assertRaisesRegex(ValueError,'clean checkout'): MOD['run_submit'](self.args)
            G['gateway'].assert_not_called()

    def test_other_project_is_not_silently_mapped(self):
        self.card.write_text('Repository: other-project\nDo the work.\n')
        with mock.patch.dict(G, {'gateway':mock.Mock()}):
            with self.assertRaisesRegex(ValueError,'Repository: openbot'):
                MOD['run_submit'](self.args)
            G['gateway'].assert_not_called()

    def test_oversized_card_does_not_create_a_pending_handoff(self):
        self.card.write_text('Repository: openbot\n' + 'x' * 50000)
        with mock.patch.dict(G, {'gateway':mock.Mock()}):
            with self.assertRaisesRegex(ValueError, 'invalid task card'):
                MOD['run_submit'](self.args)
            G['gateway'].assert_not_called()
        db = MOD['db_open']()
        self.assertEqual(db.execute('select count(*) from handoffs').fetchone()[0], 0)
        db.close()

    def test_oversized_title_and_verify_do_not_create_a_pending_handoff(self):
        for args in (self.args + ['--title', 'x' * 161], self.args + ['--verify', 'x' * 4097]):
            with self.assertRaisesRegex(ValueError, 'invalid task title or verify'):
                MOD['run_submit'](args)
        db = MOD['db_open']()
        self.assertEqual(db.execute('select count(*) from handoffs').fetchone()[0], 0)
        db.close()

    def test_pending_reconciliation_requires_verified_terminal_run(self):
        with mock.patch.dict(G, {'gateway':mock.Mock(side_effect=RuntimeError('lost ack'))}):
            with self.assertRaisesRegex(RuntimeError,'unresolved'): MOD['run_submit'](self.args)
        db = MOD['db_open']()
        request_id = db.execute("select request_id from handoffs where state='pending'").fetchone()[0]
        db.close()
        args = ['bridge', 'reconcile', '--request-id', request_id, '--host', 'executor-local', '--run-id', 'run-1',
                '--confirm', 'matching-terminal-run-verified']
        with mock.patch.object(sys, 'argv', ['bridge', *args]), \
             mock.patch.dict(G, {'gateway':mock.Mock(return_value={'id':'run-1','result':None})}):
            with self.assertRaisesRegex(ValueError,'not terminal'): MOD['main']()
        with mock.patch.object(sys, 'argv', ['bridge', *args]), \
             mock.patch.dict(G, {'gateway':mock.Mock(return_value={'id':'run-1','result':{'status':'done'}})}), \
             contextlib.redirect_stdout(io.StringIO()):
            MOD['main']()
        db = MOD['db_open']()
        self.assertEqual(db.execute('select state,run_id from handoffs').fetchone(),('accepted','run-1'))
        db.close()

if __name__ == '__main__': unittest.main()
