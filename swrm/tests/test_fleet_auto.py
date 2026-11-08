"""Automatic dispatch must preserve model and read-only boundaries."""
import pathlib
import runpy
import contextlib
import io
import json
import os
import socket
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

MOD = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'bin' / 'swrm-auto'))
preference = MOD['preference']
G = MOD['main'].__globals__

class FleetAutoTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {'OPENSWARM_AUTO_SSH_HOST':'gateway.example.invalid'})
        env.start()
        self.addCleanup(env.stop)

    def test_remote_dispatch_requires_configured_target(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(G['subprocess'], 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'OPENSWARM_AUTO_SSH_HOST'):
                G['remote']({'op':'lanes'})
        run.assert_not_called()

    def test_remote_discovery_timeout_keeps_local_lane_available(self):
        args = ['swrm-auto', '--type', 'test', '--difficulty', '2', '--base', 'abcdef0', '--prompt', 'task', '--dry-run']
        lanes = [{'name': 'K1', 'state': 'idle', 'backend': 'cursor', 'model': 'composer'}]
        def remote(_request):
            raise subprocess.TimeoutExpired('ssh', 35)
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes': lambda: lanes, 'remote': remote}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            MOD['main']()
        self.assertEqual(json.loads(out.getvalue())['lane'], 'K1')

    def test_discovered_local_lanes_do_not_require_remote_configuration(self):
        for lane in ('A1', 'C1', 'GH1', 'GH2'):
            for dry_run in (True, False):
                with self.subTest(lane=lane, dry_run=dry_run):
                    args = ['swrm-auto', '--type', 'migration', '--difficulty', '3' if lane == 'GH1' else '4',
                            '--base', 'abcdef0', '--prompt', 'task']
                    if dry_run: args.append('--dry-run')
                    rows = [{'name': lane, 'state': 'idle', 'backend': 'test', 'model': 'test'}]
                    reply = mock.Mock(returncode=0, stdout=json.dumps({'id': 'local-test', 'state': 'queued'}))
                    with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, 'argv', args), \
                            mock.patch.dict(G, {'local_lanes': lambda: rows}), \
                            mock.patch.object(G['subprocess'], 'run', return_value=reply) as run, \
                            contextlib.redirect_stdout(io.StringIO()) as output:
                        MOD['main']()
                    self.assertEqual(json.loads(output.getvalue())['host'], 'local')
                    if dry_run: run.assert_not_called()
                    else: self.assertEqual(run.call_args.args[0][1:4], ['lane', 'assign', lane])

    def test_duplicate_remote_name_cannot_override_local_busy_state(self):
        args = ['swrm-auto', '--base', 'abcdef0', '--prompt', 'task', '--dry-run']
        local = [{'name': 'C1', 'state': 'busy'}]
        remote = [{'name': 'C1', 'state': 'idle', 'backend': 'test', 'model': 'test'}]
        with mock.patch.object(sys, 'argv', args), \
                mock.patch.dict(G, {'local_lanes': lambda: local, 'remote': lambda req: remote}), \
                self.assertRaisesRegex(RuntimeError, 'no available fleet lane'):
            MOD['main']()

    def test_remote_named_k1_is_dispatched_remotely(self):
        args = ['swrm-auto', '--type', 'test', '--base', 'abcdef0', '--prompt', 'task']
        def remote(req):
            if req['op'] == 'lanes':
                return [{'name': 'K1', 'state': 'idle', 'backend': 'cursor', 'model': 'test'}]
            return {'id': 'remote-test', 'state': 'queued'}
        with mock.patch.object(sys, 'argv', args), \
                mock.patch.dict(G, {'local_lanes': lambda: [], 'remote': remote}), \
                mock.patch.object(G['subprocess'], 'run') as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            MOD['main']()
        run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['host'], 'gateway.example.invalid')

    def test_task_profiles_cover_all_five_backends(self):
        expected = [('mechanical', 1, 'M1'), ('test', 2, 'K1'), ('impl', 2, 'GH1'),
                    ('impl', 3, 'C1'), ('impl', 4, 'GH2'), ('migration', 3, 'A1')]
        for kind, difficulty, lane in expected:
            self.assertEqual(preference(kind, difficulty)[0], lane)

    def test_complex_copilot_never_falls_back_to_haiku(self):
        for kind in ('impl', 'test', 'migration'):
            for difficulty in (4, 5):
                self.assertNotIn('GH1', preference(kind, difficulty))
        self.assertNotIn('GH2', preference('impl', 2))

    def test_review_uses_available_remote_lane_and_marks_read_only(self):
        def remote(req):
            if req['op'] == 'lanes':
                return [{'name':'GH1','state':'idle','backend':'copilot','model':'haiku'}]
            self.assertTrue(req['read_only'])
            return {'id':'review-1','state':'queued'}
        args = ['swrm-auto','--type','review','--difficulty','2','--base','abcdef0',
                '--prompt','review','--verify','true']
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes':lambda: [], 'remote':remote}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            MOD['main']()
        result = json.loads(out.getvalue())
        self.assertEqual((result['lane'], result['host'], result['read_only']), ('GH1','gateway.example.invalid',True))

    def test_review_uses_available_local_lane_and_marks_read_only(self):
        args = ['swrm-auto','--type','review','--difficulty','2','--base','abcdef0','--prompt','review']
        lanes = [{'name':'K1','state':'idle','backend':'cursor','model':'composer'}]
        completed = mock.Mock(returncode=0, stdout=json.dumps({'ok':True,'id':'review-2','state':'queued'}))
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes':lambda: lanes, 'remote':lambda req: []}), \
                mock.patch.object(G['subprocess'], 'run', return_value=completed) as run, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            MOD['main']()
        self.assertIn('--read-only', run.call_args.args[0])
        self.assertEqual(json.loads(out.getvalue())['host'], 'local')

    def test_review_with_only_pi_uses_isolated_one_shot(self):
        args = ['swrm-auto','--type','review','--difficulty','2','--base','abcdef0','--prompt','review']
        lanes = [{'name':'M1','state':'idle','backend':'pi','model':'MiniMax'}]
        completed = mock.Mock(returncode=0, stdout=json.dumps({'id':'review-3','state':'started'}))
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes':lambda: lanes, 'remote':lambda req: []}), \
                mock.patch.object(G['subprocess'], 'run', return_value=completed) as run, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            MOD['main']()
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ['run', '--type'])
        self.assertIn('--read-only', command)
        self.assertEqual(json.loads(out.getvalue())['execution_mode'], 'one-shot')

    def test_lost_remote_ack_does_not_dispatch_to_next_lane(self):
        calls = []
        def remote(req):
            calls.append(req['op'])
            if req['op'] == 'lanes':
                return [{'name': 'A1', 'state': 'idle', 'backend': 'agy', 'model': 'gemini'}]
            raise RuntimeError('remote dispatch gateway unavailable')
        local = [{'name': 'K1', 'state': 'idle', 'backend': 'cursor', 'model': 'composer'}]
        args = ['swrm-auto', '--type', 'migration', '--difficulty', '3', '--base', 'abcdef0', '--prompt', 'task']
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes': lambda: local, 'remote': remote}), \
                mock.patch.object(G['subprocess'], 'run') as run, self.assertRaisesRegex(RuntimeError, 'unavailable'):
            MOD['main']()
        self.assertEqual(calls, ['lanes', 'assign'])
        run.assert_not_called()

    def test_lost_local_ack_does_not_try_another_lane(self):
        lanes = [{'name': 'K1', 'state': 'idle', 'backend': 'cursor', 'model': 'composer'},
                 {'name': 'M1', 'state': 'idle', 'backend': 'pi', 'model': 'minimax'}]
        args = ['swrm-auto', '--type', 'test', '--difficulty', '2', '--base', 'abcdef0', '--prompt', 'task']
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes': lambda: lanes, 'remote': lambda req: []}), \
                mock.patch.object(G['subprocess'], 'run', return_value=mock.Mock(stdout='', returncode=0)) as run, \
                self.assertRaisesRegex(RuntimeError, 'acknowledgement unavailable'):
            MOD['main']()
        self.assertEqual(run.call_count, 1)

    def test_explicit_remote_busy_falls_back_to_local_lane(self):
        def remote(req):
            if req['op'] == 'lanes':
                return [{'name': 'A1', 'state': 'idle', 'backend': 'agy', 'model': 'gemini'}]
            raise G['LaneBusy']('remote lane is busy')
        local = [{'name': 'K1', 'state': 'idle', 'backend': 'cursor', 'model': 'composer'}]
        args = ['swrm-auto', '--type', 'migration', '--difficulty', '3', '--base', 'abcdef0', '--prompt', 'task']
        reply = mock.Mock(stdout=json.dumps({'ok': True, 'id': 'fallback-1', 'state': 'queued'}), returncode=0)
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes': lambda: local, 'remote': remote}), \
                mock.patch.object(G['subprocess'], 'run', return_value=reply) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            MOD['main']()
        self.assertEqual(json.loads(output.getvalue())['lane'], 'K1')
        self.assertEqual(run.call_count, 1)

    def test_gateway_distinguishes_busy_from_unknown_failure(self):
        busy = {'ok': False, 'result': {'ok': False, 'error': 'lane A1 is busy (busy)'}}
        with mock.patch.object(G['subprocess'], 'run', return_value=mock.Mock(returncode=1, stdout=json.dumps(busy))):
            with self.assertRaises(G['LaneBusy']):
                G['remote']({'op': 'assign', 'lane': 'A1'})
        unknown = {'ok': False, 'error': 'gateway timeout'}
        with mock.patch.object(G['subprocess'], 'run', return_value=mock.Mock(returncode=1, stdout=json.dumps(unknown))):
            with self.assertRaisesRegex(RuntimeError, 'gateway rejected'):
                G['remote']({'op': 'assign', 'lane': 'A1'})

    def test_local_closed_socket_does_not_dispatch_next_lane(self):
        local = [{'name':'K1','state':'idle','backend':'cursor','model':'composer'},
                 {'name':'M1','state':'idle','backend':'pi','model':'minimax'}]
        args = ['swrm-auto','--type','test','--difficulty','2','--base','abcdef0','--prompt','task']
        reply = mock.Mock(returncode=1, stdout=json.dumps({'ok':False, 'error':'lane closed control socket'}))
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes':lambda: local, 'remote':lambda req: []}), \
             mock.patch.object(G['subprocess'], 'run', return_value=reply) as run, \
             self.assertRaisesRegex(RuntimeError, 'acknowledgement unavailable'):
            MOD['main']()
        self.assertEqual(run.call_count, 1)

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "exercises a raw AF_UNIX lane socket, unavailable on this platform")
    def test_real_lane_socket_close_keeps_dispatch_uncertain(self):
        lane = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'bin' / 'swrm'))
        with tempfile.TemporaryDirectory(prefix='ack-', dir=tempfile.gettempdir()) as directory:
            sockpath = str(pathlib.Path(directory) / 'lane.sock')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(sockpath)
            listener.listen(1)
            received = []
            def accept_without_reply():
                connection, _ = listener.accept()
                with connection:
                    received.append(json.loads(connection.recv(65536)))
                listener.close()
            thread = threading.Thread(target=accept_without_reply)
            thread.start()
            with mock.patch.dict(lane['lane_call'].__globals__, {'lane_socket_path':lambda _: sockpath}):
                reply = lane['lane_call']('K1', {'op':'assign', 'run_id':'real-socket-test'})
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(received[0]['run_id'], 'real-socket-test')
        self.assertEqual(reply, {'ok':False, 'error':'lane closed control socket'})
        local = [{'name':'K1','state':'idle','backend':'cursor','model':'composer'},
                 {'name':'M1','state':'idle','backend':'pi','model':'minimax'}]
        args = ['swrm-auto','--type','test','--difficulty','2','--base','abcdef0','--prompt','task']
        result = mock.Mock(returncode=1, stdout=json.dumps(reply))
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes':lambda: local, 'remote':lambda req: []}), \
             mock.patch.object(G['subprocess'], 'run', return_value=result) as run, \
             self.assertRaisesRegex(RuntimeError, 'acknowledgement unavailable'):
            MOD['main']()
        self.assertEqual(run.call_count, 1)

    def test_local_explicit_busy_falls_back(self):
        local = [{'name':'K1','state':'idle','backend':'cursor','model':'composer'},
                 {'name':'M1','state':'idle','backend':'pi','model':'minimax'}]
        args = ['swrm-auto','--type','test','--difficulty','2','--base','abcdef0','--prompt','task']
        busy = mock.Mock(returncode=1, stdout=json.dumps({'ok':False, 'error':'lane K1 is busy (busy)'}))
        accepted = mock.Mock(returncode=0, stdout=json.dumps({'ok':True, 'id':'next-run', 'state':'queued'}))
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes':lambda: local, 'remote':lambda req: []}), \
             mock.patch.object(G['subprocess'], 'run', side_effect=[busy, accepted]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            MOD['main']()
        self.assertEqual(json.loads(output.getvalue())['lane'], 'M1')


class ConfiguredFleetAutoTests(unittest.TestCase):
    """Exercise the deployed wrapper using the documented service environment."""
    def setUp(self):
        self.root = pathlib.Path(__file__).resolve().parents[2]
        self.tmp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)
        self.calls = self.path / 'calls.jsonl'
        fake = self.path / 'fleet-cli'
        fake.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys
args = sys.argv[1:]
with open(os.environ['TEST_CALLS'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
if args == ['lane', 'status']:
    print(json.dumps([{'name':'M1','state':'idle','backend':'pi','model':'MiniMax'}]))
elif args[:2] == ['fleet', 'exec']:
    host = args[2]
    if args[4:] == ['lane', 'status']:
        print(json.dumps(json.loads(os.environ['TEST_FLEET_ROWS']).get(host, [])))
    elif args[4:6] == ['lane', 'assign']:
        if os.environ.get('TEST_LOST_ACK'):
            sys.exit(1)
        if host == os.environ.get('TEST_BUSY_HOST'):
            print(json.dumps({'ok':False,'error':'lane ' + args[6] + ' is busy (busy)'}))
            sys.exit(1)
        print(json.dumps({'id':'fleet-run','state':'queued'}))
    else:
        sys.exit(2)
else:
    sys.exit(2)
''')
        fake.chmod(0o755)
        self.env = dict(os.environ)
        for line in (self.root / 'docs/examples/hub-bridge.env.example').read_text().splitlines():
            if line and not line.startswith('#'):
                key, value = shlex.split(line)[0].split('=', 1)
                self.env[key] = value
        self.env.update(OPENSWARM_AUTO_CLI=str(fake), TEST_CALLS=str(self.calls))
        self.hosts = json.loads(self.env['OPENSWARM_AUTO_FLEET_HOSTS'])

    def dispatch(self, rows, *options):
        self.env['TEST_FLEET_ROWS'] = json.dumps(rows)
        proc = subprocess.run([str(self.root / 'swrm/bin/swrm-fleet'), 'auto',
                               '--type', 'mechanical', '--difficulty', '1',
                               '--base', 'abcdef0', '--prompt', 'task', *options],
                              env=self.env, text=True, capture_output=True, timeout=15)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([call[2] for call in calls if call[4:] == ['lane', 'status']], self.hosts)
        self.assertTrue(all(call[:2] == ['fleet', 'exec'] for call in calls), calls)
        return proc, json.loads(proc.stdout), calls

    def test_example_with_empty_remotes_never_probes_idle_local_lane(self):
        proc, result, calls = self.dispatch({}, '--dry-run')
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(result['error'], 'no available fleet lane')
        self.assertEqual(len(calls), len(self.hosts))

    def test_example_filters_wrong_backends_and_dispatches_remote_lane(self):
        rows = {'gocc': [{'name':'M1','state':'idle','backend':'pi','model':'MiniMax'}],
                'gorelay': [{'name':'C2','state':'idle','backend':'codex','model':'codex'}]}
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                self.calls.unlink(missing_ok=True)
                proc, result, calls = self.dispatch(rows, *(['--dry-run'] if dry_run else []))
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual((result['host'], result['lane']), ('gorelay', 'C2'))
                if not dry_run:
                    self.assertEqual(calls[-1][:7], ['fleet','exec','gorelay','--','lane','assign','C2'])

    def test_same_lane_name_on_two_hosts_can_fall_back_after_explicit_busy(self):
        row = [{'name':'M2','state':'idle','backend':'pi','model':'MiniMax'}]
        self.env['TEST_BUSY_HOST'] = 'gores1'
        proc, result, calls = self.dispatch({'gores1':row, 'gores2':row}, '--type', 'review')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((result['host'], result['lane'], result['read_only']), ('gores2','M2',True))
        assigns = [call for call in calls if call[4:6] == ['lane', 'assign']]
        self.assertEqual([call[2] for call in assigns], ['gores1','gores2'])
        self.assertTrue(all('--read-only' in call for call in assigns))

    def test_lost_fleet_ack_does_not_dispatch_again(self):
        row = [{'name':'M1','state':'idle','backend':'pi','model':'MiniMax'}]
        self.env['TEST_LOST_ACK'] = '1'
        proc, result, calls = self.dispatch({'gores1':row, 'gores2':row})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('acknowledgement unavailable', result['error'])
        self.assertEqual(len([call for call in calls if call[4:6] == ['lane', 'assign']]), 1)

if __name__ == '__main__': unittest.main()
