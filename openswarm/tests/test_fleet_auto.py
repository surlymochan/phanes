"""Automatic dispatch must preserve model and read-only boundaries."""
import pathlib
import runpy
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

MOD = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'bin' / 'openswarm-auto'))
preference = MOD['preference']
G = MOD['main'].__globals__

class FleetAutoTests(unittest.TestCase):
    def test_remote_discovery_timeout_keeps_local_lane_available(self):
        args = ['openswarm-auto', '--type', 'test', '--difficulty', '2', '--base', 'abcdef0', '--prompt', 'task', '--dry-run']
        lanes = [{'name': 'K1', 'state': 'idle', 'backend': 'cursor', 'model': 'composer'}]
        def remote(_request):
            raise subprocess.TimeoutExpired('ssh', 35)
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes': lambda: lanes, 'remote': remote}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            MOD['main']()
        self.assertEqual(json.loads(out.getvalue())['lane'], 'K1')

    def test_task_profiles_cover_all_five_backends(self):
        expected = [('mechanical', 1, 'pi'), ('test', 2, 'cursor'), ('impl', 2, 'pi'),
                    ('impl', 3, 'pi'), ('impl', 4, 'agy'), ('migration', 3, 'agy')]
        for kind, difficulty, backend in expected:
            self.assertEqual(preference(kind, difficulty)[0], backend)

    def test_complex_copilot_never_falls_back_to_haiku(self):
        for kind in ('impl', 'test', 'migration'):
            for difficulty in (4, 5):
                self.assertLess(preference(kind, difficulty).index('agy'), preference(kind, difficulty).index('copilot'))
        self.assertLess(preference('impl', 2).index('pi'), preference('impl', 2).index('copilot'))

    def test_discovers_all_idle_lanes_for_expanded_backend_capacity(self):
        lanes = [
            {'name': 'M4', 'state': 'idle', 'backend': 'pi', 'model': 'MiniMax'},
            {'name': 'A2', 'state': 'idle', 'backend': 'agy', 'model': 'Gemini'},
            {'name': 'K2', 'state': 'idle', 'backend': 'cursor', 'model': 'composer'},
            {'name': 'M2', 'state': 'idle', 'backend': 'pi', 'model': 'MiniMax'},
            {'name': 'M1', 'state': 'busy', 'backend': 'pi', 'model': 'MiniMax'},
            {'name': 'A1', 'state': 'idle', 'backend': 'agy', 'model': 'Gemini'},
            {'name': 'K1', 'state': 'idle', 'backend': 'cursor', 'model': 'composer'},
            {'name': 'M3', 'state': 'idle', 'backend': 'pi', 'model': 'MiniMax'},
        ]
        ordered = G['available_in_preference'](lanes, ['pi', 'agy', 'cursor'])
        self.assertEqual([row['name'] for row in ordered], ['M2', 'M3', 'M4', 'A1', 'A2', 'K1', 'K2'])

    def test_duplicate_lane_names_remain_distinct_by_dispatch_origin(self):
        local = [{'name': 'A1', 'state': 'idle', 'backend': 'agy', 'model': 'local-gemini'}]
        def remote(request):
            if request['op'] == 'lanes':
                return [{'name': 'A1', 'state': 'idle', 'backend': 'agy', 'model': 'remote-gemini'}]
            self.assertEqual(request['lane'], 'A1')
            return {'id': 'remote-a1', 'state': 'queued'}
        args = ['openswarm-auto', '--type', 'migration', '--difficulty', '4', '--base', 'abcdef0', '--prompt', 'task']
        local_busy = mock.Mock(returncode=1, stdout=json.dumps({'ok': False, 'error': 'lane A1 is busy (busy)'}))
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes': lambda: local, 'remote': remote}), \
                mock.patch.object(G['subprocess'], 'run', return_value=local_busy), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            MOD['main']()
        result = json.loads(out.getvalue())
        self.assertEqual((result['id'], result['host'], result['lane']), ('remote-a1', 'gocc', 'A1'))

    def test_explicit_remote_gateway_host_is_used_for_discovery_and_dispatch(self):
        extra = {'host': 'govpn', 'ssh_alias': 'openswarm-govpn-gateway'}
        calls = []
        def remote(request):
            self.assertEqual(request, {'op': 'lanes'})
            return []
        def remote_from(source, request):
            calls.append((source, request))
            if request == {'op': 'lanes'}:
                return [{'name': 'K2', 'state': 'idle', 'backend': 'cursor', 'model': 'composer-2.5[fast=false]'}]
            return {'id': 'govpn-k2', 'state': 'queued'}
        args = ['openswarm-auto', '--type', 'test', '--difficulty', '2', '--base', 'abcdef0', '--prompt', 'task']
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {
                'local_lanes': lambda: [], 'remote_sources': lambda: [G['DEFAULT_REMOTE_SOURCE'], extra],
                'remote': remote, 'remote_from': remote_from}), contextlib.redirect_stdout(io.StringIO()) as out:
            MOD['main']()
        self.assertEqual(json.loads(out.getvalue())['host'], 'govpn')
        self.assertEqual(calls, [(extra, {'op': 'lanes'}),
                                 (extra, mock.ANY)])
        self.assertEqual(calls[1][1]['lane'], 'K2')

    def test_gocc_relay_source_envelopes_discovery_and_assignment(self):
        source = {'host': 'govpn', 'ssh_alias': 'openswarm-pi-broker', 'via': 'gocc'}
        calls = []
        def run(command, input=None, **_kwargs):
            calls.append((command, json.loads(input)))
            request = calls[-1][1]['request']
            response = ([] if request['op'] == 'lanes' else {'id': 'govpn-k1', 'state': 'queued'})
            return subprocess.CompletedProcess(command, 0, json.dumps({'ok': True, 'result': response}), '')
        with mock.patch.object(G['subprocess'], 'run', side_effect=run):
            self.assertEqual(G['remote_from'](source, {'op':'lanes'}), [])
            self.assertEqual(G['remote_from'](source, {'op':'assign','lane':'K1'}),
                             {'id':'govpn-k1','state':'queued'})
        self.assertEqual([item[0][-1] for item in calls], ['openswarm-pi-broker'] * 2)
        self.assertEqual(calls[0][1], {'op':'relay','target':'govpn','request':{'op':'lanes'}})
        self.assertEqual(calls[1][1], {'op':'relay','target':'govpn',
                                       'request':{'op':'assign','lane':'K1'}})

    def test_remote_source_configuration_rejects_untrusted_alias_syntax(self):
        with mock.patch.dict(os.environ, {'OPENSWARM_AUTO_REMOTE_SOURCES': '[{"host":"gocc","ssh_alias":"bad alias"}]'}):
            with self.assertRaisesRegex(RuntimeError, 'invalid automatic routing'):
                G['remote_sources']()

    def test_extra_remote_sources_preserve_default_gocc_and_reject_ambiguity(self):
        extra = '[{"host":"govpn","ssh_alias":"openswarm-govpn-gateway"}]'
        with mock.patch.dict(os.environ, {'OPENSWARM_AUTO_REMOTE_SOURCES': extra}):
            self.assertEqual(G['remote_sources'](), [G['DEFAULT_REMOTE_SOURCE'],
                                                     {'host':'govpn','ssh_alias':'openswarm-govpn-gateway'}])
        for config in (
            '[{"host":"gocc","ssh_alias":"other-gateway"}]',
            '[{"host":"gocc","ssh_alias":"openswarm-pi-broker","via":"gocc"}]',
            '[{"host":"gores1","ssh_alias":"openswarm-gores-gateway"}]',
            '[{"host":"govpn","ssh_alias":"a"},{"host":"govpn","ssh_alias":"b"}]',
        ):
            with mock.patch.dict(os.environ, {'OPENSWARM_AUTO_REMOTE_SOURCES': config}):
                with self.assertRaisesRegex(RuntimeError, 'conflicting automatic routing'):
                    G['remote_sources']()

    def test_relay_source_only_allows_gocc_as_transit(self):
        raw = '[{"host":"govpn","ssh_alias":"openswarm-pi-broker","via":"gocc"}]'
        with mock.patch.dict(os.environ, {'OPENSWARM_AUTO_REMOTE_SOURCES': raw}):
            self.assertEqual(G['remote_sources']()[-1],
                             {'host':'govpn','ssh_alias':'openswarm-pi-broker','via':'gocc'})
        raw = '[{"host":"govpn","ssh_alias":"relay","via":"other"}]'
        with mock.patch.dict(os.environ, {'OPENSWARM_AUTO_REMOTE_SOURCES': raw}):
            with self.assertRaisesRegex(RuntimeError, 'invalid automatic routing'):
                G['remote_sources']()

    def test_same_lane_name_on_remote_hosts_keeps_gateway_origin(self):
        govpn = {'host': 'govpn', 'ssh_alias': 'openswarm-govpn-gateway'}
        calls = []
        def remote(request):
            if request['op'] == 'lanes':
                return [{'name':'K1', 'state':'idle', 'backend':'cursor', 'model':'composer'}]
            raise G['LaneBusy']('remote lane is busy')
        def remote_from(source, request):
            calls.append((source, request))
            if request['op'] == 'lanes':
                return [{'name':'K1', 'state':'idle', 'backend':'cursor', 'model':'composer'}]
            return {'id':'govpn-k1','state':'queued'}
        args = ['openswarm-auto', '--type', 'test', '--difficulty', '2', '--base', 'abcdef0', '--prompt', 'task']
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {
                'local_lanes': lambda: [], 'remote_sources': lambda: [G['DEFAULT_REMOTE_SOURCE'], govpn],
                'remote': remote, 'remote_from': remote_from}), contextlib.redirect_stdout(io.StringIO()) as out:
            MOD['main']()
        self.assertEqual(json.loads(out.getvalue())['host'], 'govpn')
        self.assertEqual(calls[-1], (govpn, mock.ANY))
        self.assertEqual(calls[-1][1]['lane'], 'K1')

    def test_review_uses_available_remote_lane_and_marks_read_only(self):
        def remote(req):
            if req['op'] == 'lanes':
                return [{'name':'GH1','state':'idle','backend':'copilot','model':'haiku'}]
            self.assertTrue(req['read_only'])
            return {'id':'review-1','state':'queued'}
        args = ['openswarm-auto','--type','review','--difficulty','2','--base','abcdef0',
                '--prompt','review','--verify','true']
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes':lambda: [], 'remote':remote}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            MOD['main']()
        result = json.loads(out.getvalue())
        self.assertEqual((result['lane'], result['host'], result['read_only']), ('GH1','gocc',True))

    def test_review_uses_available_local_lane_and_marks_read_only(self):
        args = ['openswarm-auto','--type','review','--difficulty','2','--base','abcdef0','--prompt','review']
        lanes = [{'name':'K1','state':'idle','backend':'cursor','model':'composer'}]
        completed = mock.Mock(returncode=0, stdout=json.dumps({'ok':True,'id':'review-2','state':'queued'}))
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes':lambda: lanes, 'remote':lambda req: []}), \
                mock.patch.object(G['subprocess'], 'run', return_value=completed) as run, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            MOD['main']()
        self.assertIn('--read-only', run.call_args.args[0])
        self.assertEqual(json.loads(out.getvalue())['host'], 'gores1')

    def test_review_with_only_pi_uses_isolated_one_shot(self):
        args = ['openswarm-auto','--type','review','--difficulty','2','--base','abcdef0','--prompt','review']
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
            raise RuntimeError('gocc dispatch gateway unavailable')
        local = [{'name': 'K1', 'state': 'idle', 'backend': 'cursor', 'model': 'composer'}]
        args = ['openswarm-auto', '--type', 'migration', '--difficulty', '3', '--base', 'abcdef0', '--prompt', 'task']
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes': lambda: local, 'remote': remote}), \
                mock.patch.object(G['subprocess'], 'run') as run, self.assertRaisesRegex(RuntimeError, 'unavailable'):
            MOD['main']()
        self.assertEqual(calls, ['lanes', 'assign'])
        run.assert_not_called()

    def test_lost_local_ack_does_not_try_another_lane(self):
        lanes = [{'name': 'K1', 'state': 'idle', 'backend': 'cursor', 'model': 'composer'},
                 {'name': 'M1', 'state': 'idle', 'backend': 'pi', 'model': 'minimax'}]
        args = ['openswarm-auto', '--type', 'test', '--difficulty', '2', '--base', 'abcdef0', '--prompt', 'task']
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
        args = ['openswarm-auto', '--type', 'migration', '--difficulty', '3', '--base', 'abcdef0', '--prompt', 'task']
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
        args = ['openswarm-auto','--type','test','--difficulty','2','--base','abcdef0','--prompt','task']
        reply = mock.Mock(returncode=1, stdout=json.dumps({'ok':False, 'error':'lane closed control socket'}))
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes':lambda: local, 'remote':lambda req: []}), \
             mock.patch.object(G['subprocess'], 'run', return_value=reply) as run, \
             self.assertRaisesRegex(RuntimeError, 'acknowledgement unavailable'):
            MOD['main']()
        self.assertEqual(run.call_count, 1)

    def test_real_lane_socket_close_keeps_dispatch_uncertain(self):
        lane = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'bin' / 'openswarm'))
        with tempfile.TemporaryDirectory(prefix='ack-', dir='/tmp') as directory:
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
        args = ['openswarm-auto','--type','test','--difficulty','2','--base','abcdef0','--prompt','task']
        result = mock.Mock(returncode=1, stdout=json.dumps(reply))
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes':lambda: local, 'remote':lambda req: []}), \
             mock.patch.object(G['subprocess'], 'run', return_value=result) as run, \
             self.assertRaisesRegex(RuntimeError, 'acknowledgement unavailable'):
            MOD['main']()
        self.assertEqual(run.call_count, 1)

    def test_local_explicit_busy_falls_back(self):
        local = [{'name':'K1','state':'idle','backend':'cursor','model':'composer'},
                 {'name':'M1','state':'idle','backend':'pi','model':'minimax'}]
        args = ['openswarm-auto','--type','test','--difficulty','2','--base','abcdef0','--prompt','task']
        busy = mock.Mock(returncode=1, stdout=json.dumps({'ok':False, 'error':'lane K1 is busy (busy)'}))
        accepted = mock.Mock(returncode=0, stdout=json.dumps({'ok':True, 'id':'next-run', 'state':'queued'}))
        with mock.patch.object(sys, 'argv', args), mock.patch.dict(G, {'local_lanes':lambda: local, 'remote':lambda req: []}), \
             mock.patch.object(G['subprocess'], 'run', side_effect=[busy, accepted]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            MOD['main']()
        self.assertEqual(json.loads(output.getvalue())['lane'], 'M1')

if __name__ == '__main__': unittest.main()
