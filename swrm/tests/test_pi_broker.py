"""Verify that the credential broker rejects off-target requests before networking.

The broker is also the collection side of the MiniMax burn reading (Refs #1671):
its request log carries the token counters the provider reported per request,
so the fleet side can measure the real hourly token consumption instead of a
dispatch count proxy.
"""
import base64
import http.client
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from _broker_wait import short_tmp_root, wait_for_unix_socket

BROKER = pathlib.Path(__file__).resolve().parents[1] / 'bin' / 'pi-minimax-broker.mjs'


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=3)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


@unittest.skipUnless(shutil.which('node'), 'requires node')
@unittest.skipIf(os.name == "nt",
                 "pi broker binds a '/'-rooted unix-domain socket by design; no Windows transport")
class PiBrokerTests(unittest.TestCase):
    def _stderr_file(self, path):
        # unittest's enterContext needs Python 3.11; CI also runs 3.9.
        handle = open(path + '.stderr', 'wb')
        self.addCleanup(handle.close)
        return handle

    # -- helpers for the request log of the broker (Refs #1671) -----------
    @staticmethod
    def _stop(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - the broker is stubborn
                proc.kill()

    def _broker_with_answer(self, answer, root):
        """Start the broker with a stubbed provider that answers with ``answer``."""
        module = pathlib.Path(root, 'auth.mjs')
        module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
        preload = pathlib.Path(root, 'fake.cjs')
        preload.write_text('''
const https = require('node:https');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const body = Buffer.from(process.env.FAKE_ANSWER || '', 'base64');
https.request = (_options, callback) => {
  const req = new EventEmitter();
  req.destroy = () => {};
  req.end = () => {
    const upstream = new PassThrough();
    upstream.statusCode = 200; upstream.headers = {}; upstream.complete = true;
    callback(upstream); upstream.end(body);
  };
  return req;
};
''')
        path = os.path.join(root, 'broker.sock')
        env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload),
                   FAKE_ANSWER=base64.b64encode(answer).decode('ascii'))
        proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                env=env, stdout=subprocess.DEVNULL, stderr=self._stderr_file(path))
        self.addCleanup(self._stop, proc)
        wait_for_unix_socket(path, proc=proc, errors=path + '.stderr')
        return path

    def _ask(self, path, payload=None):
        """Send one request through the broker and read the answer."""
        client = UnixHTTP(path)
        client.request('POST', '/v1/messages',
                       json.dumps(payload or {'model': 'allowed-model', 'messages': []}),
                       {'content-type': 'application/json'})
        response = client.getresponse()
        response.read()
        client.close()
        return response.status

    def _logged_rows(self, path, timeout=5):
        """The rows the broker wrote into its request log."""
        log = pathlib.Path(path + '.requests.jsonl')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = []
            try:
                text = log.read_text()
            except OSError:
                text = ''
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
            if rows:
                return rows
            time.sleep(0.05)
        self.fail('the broker wrote no request log: %s' % log)

    def test_the_logged_counters_are_the_usage_of_the_provider_not_its_content(self):
        """The counters of the log are the usage of the provider, not content.

        The input of a tool call is content of the answer: its numbers are not
        the usage of the request and must never end up in the burn reading
        (Refs #1671).
        """
        answer = json.dumps({'id': 'msg_1', 'type': 'message', 'role': 'assistant',
                             'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'shell',
                                          'input': {'command': 'ls', 'input_tokens': 99999,
                                                    'output_tokens': 88888}}],
                             'stop_reason': 'tool_use',
                             'usage': {'input_tokens': 120, 'output_tokens': 34}})
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            path = self._broker_with_answer(answer.encode('utf-8'), root)
            self.assertEqual(self._ask(path), 200)
            row = self._logged_rows(path)[-1]
            self.assertEqual(row['in_tok'], 120,
                             'the logged input tokens must be the usage of the provider: %r' % row)
            self.assertEqual(row['out_tok'], 34,
                             'the logged output tokens must be the usage of the provider: %r' % row)

    def test_the_usage_of_a_long_answer_is_not_lost(self):
        """The usage of an answer that is bigger than a scan window is kept.

        A streamed answer carries the input tokens of the request in its first
        event: a scan of the last bytes of the answer loses them and the hour is
        under-counted (Refs #1671).
        """
        answer = ''.join([
            'event: message_start\ndata: %s\n\n' % json.dumps(
                {'type': 'message_start', 'message': {'usage': {'input_tokens': 120, 'output_tokens': 1}}}),
            'event: content_block_delta\ndata: %s\n\n' % json.dumps(
                {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'x' * 6000}}),
            'event: message_delta\ndata: %s\n\n' % json.dumps(
                {'type': 'message_delta', 'usage': {'output_tokens': 34}}),
        ])
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            path = self._broker_with_answer(answer.encode('utf-8'), root)
            self.assertEqual(self._ask(path), 200)
            row = self._logged_rows(path)[-1]
            self.assertEqual(row['in_tok'], 120,
                             'the input tokens of the provider must survive a long answer: %r' % row)
            self.assertEqual(row['out_tok'], 34,
                             'the output tokens of the provider must survive a long answer: %r' % row)

    def test_a_request_refused_before_the_provider_is_an_explicit_zero(self):
        """A request the broker refuses never reached the provider.

        Its counters are explicit zeros: the hour of the fleet is a real zero
        burn and not a broken collection (Refs #1671).
        """
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            path = self._broker_with_answer(b'{}', root)
            client = UnixHTTP(path)
            client.request('GET', '/refused', '', {})
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            client.close()
            row = self._logged_rows(path)[-1]
            self.assertEqual(row['in_tok'], 0,
                             'a refused request burned nothing: %r' % row)
            self.assertEqual(row['out_tok'], 0,
                             'a refused request burned nothing: %r' % row)

    def test_quiet_collector_initializes_log_without_truncation(self):
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            root = pathlib.Path(root)
            module = root / 'auth.mjs'
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            preload = root / 'listener.cjs'
            preload.write_text('''
const http = require('node:http');
const fs = require('node:fs');
http.createServer = () => ({
  listen(path, ready) { fs.writeFileSync(path, ''); ready(); }
});
''')
            for index, initial in enumerate((None, 'existing audit records\n', 'unwritable')):
                with self.subTest(initial=initial):
                    log = root / f'audit-{index}'
                    if initial == 'unwritable':
                        log.mkdir()
                    elif initial is not None:
                        log.write_text(initial)
                    env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload), PI_BROKER_LOG=str(log))
                    proc = subprocess.run(['node', str(BROKER), str(root / f'broker-{index}.sock'), str(module), 'allowed-model'],
                                          env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    health = json.loads(pathlib.Path(str(log) + '.heartbeat').read_text())
                    if initial == 'unwritable':
                        self.assertIsNotNone(health['audit_error_at'])
                        self.assertIn('initialization failed', proc.stderr)
                    else:
                        self.assertEqual(log.read_text(), initial or '')
                        self.assertIsNone(health['audit_error_at'])
                        self.assertLess(abs((datetime.now(timezone.utc) - datetime.fromisoformat(
                            health['coverage_start'].replace('Z', '+00:00'))).total_seconds()), 10)
                        self.assertEqual(health['log_ino'], log.stat().st_ino)

    def test_coverage_survives_restart_but_resets_after_log_loss(self):
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            root = pathlib.Path(root)
            module = root / 'auth.mjs'
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            preload = root / 'listener.cjs'
            preload.write_text('''
const http = require('node:http');
const fs = require('node:fs');
http.createServer = () => ({ listen(path, ready) { fs.writeFileSync(path, ''); ready(); } });
''')
            log = root / 'audit.jsonl'
            heartbeat = pathlib.Path(str(log) + '.heartbeat')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload), PI_BROKER_LOG=str(log))

            def run():
                path = root / 'broker.sock'
                path.unlink(missing_ok=True)
                proc = subprocess.run(['node', str(BROKER), str(path), str(module), 'allowed-model'],
                                      env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return json.loads(heartbeat.read_text())

            health = run()
            watchdog = BROKER.parents[2] / 'tools' / 'mm-burn-rate.py'
            reading = subprocess.run([sys.executable, str(watchdog), '--history', str(root / 'history'),
                                      '--broker-log', str(log), '--status-file', str(root / 'status.json'), '--json'],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(reading.returncode, 0, reading.stderr)
            payload = json.loads(reading.stdout)
            self.assertEqual(payload['token_state'], 'incomplete')
            self.assertIsNone(payload['hourly_tokens'])
            self.assertFalse(payload['token_alert'])
            self.assertTrue(all(bucket['tokens'] is None for bucket in payload['hourly_buckets']))
            old_start = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
            for loss in ('deleted', 'replaced', 'truncated'):
                with self.subTest(loss=loss):
                    log.write_text('prior audit records\n')
                    health.update(coverage_start=old_start, log_dev=log.stat().st_dev,
                                  log_ino=log.stat().st_ino, log_size=log.stat().st_size)
                    heartbeat.write_text(json.dumps(health))
                    self.assertEqual(run()['coverage_start'], old_start)
                    if loss == 'deleted':
                        log.unlink()
                    elif loss == 'replaced':
                        log.rename(root / 'old-audit')
                        log.write_text('replacement\n')
                    else:
                        log.write_text('')
                    health = run()
                    self.assertNotEqual(health['coverage_start'], old_start)
                    self.assertEqual(health['log_ino'], log.stat().st_ino)

    def test_audit_retains_window_and_reports_write_failure_without_sockets(self):
        """Exercise the actual audit writer with only the HTTP listener stubbed."""
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            root = pathlib.Path(root)
            module = root / 'auth.mjs'
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            preload = root / 'listener.cjs'
            preload.write_text('''
const http = require('node:http');
const fs = require('node:fs');
let failAudit = false;
const append = fs.appendFileSync;
fs.appendFileSync = (...args) => {
  if (failAudit) { const error = new Error('disk unavailable'); error.code = 'EIO'; throw error; }
  return append(...args);
};
http.createServer = handler => ({
  listen(path, ready) {
    fs.writeFileSync(path, '');
    ready();
    for (let i = 0; i < 9; i++) {
      handler({method: 'GET', url: '/refused', headers: {}},
              {headersSent: false, writeHead() {}, end() {}});
    }
    // Fail an append after rotations succeeded. The next heartbeat must carry it.
    failAudit = true;
    handler({method: 'GET', url: '/refused', headers: {}},
            {headersSent: false, writeHead() {}, end() {}});
  }
});
''')
            path = root / 'broker.sock'
            log = root / 'requests.jsonl'
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload),
                       PI_BROKER_LOG=str(log), PI_BROKER_LOG_MAX_BYTES='1',
                       PI_BROKER_LOG_ROTATIONS='2')
            proc = subprocess.run(['node', str(BROKER), str(path), str(module), 'allowed-model'],
                                  env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rotations = list(root.glob('requests.jsonl.[0-9]*'))
            self.assertEqual(len(rotations), 9)
            self.assertEqual(sum(len(p.read_text().splitlines()) for p in rotations), 9)
            health = json.loads(pathlib.Path(str(log) + '.heartbeat').read_text())
            self.assertIsNotNone(health['audit_error_at'])
            self.assertIn('append failed', proc.stderr)

    def test_beta_request_preserves_upstream_opt_in(self):
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            capture = pathlib.Path(root, 'request.json')
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const fs = require('node:fs');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
https.request = (options, callback) => {
  fs.writeFileSync(process.env.REQUEST_CAPTURE, JSON.stringify({path: options.path, beta: options.headers['anthropic-beta']}));
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => { const upstream = new PassThrough(); upstream.statusCode = 200; upstream.headers = {}; upstream.complete = true; callback(upstream); upstream.end('ok'); };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload), REQUEST_CAPTURE=str(capture))
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=self._stderr_file(path))
            try:
                wait_for_unix_socket(path, proc=proc, errors=path + '.stderr')
                client = UnixHTTP(path)
                client.request('POST', '/v1/messages?beta=true', json.dumps({'model': 'allowed-model', 'messages': []}),
                               {'anthropic-beta': 'interleaved-thinking-2025-05-14'})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                client.close()
                self.assertEqual(json.loads(capture.read_text()), {'path': '/anthropic/v1/messages?beta=true',
                    'beta': 'interleaved-thinking-2025-05-14'})
            finally:
                proc.terminate()
                proc.wait(timeout=5)

    def test_aborted_upstream_releases_busy_slot(self):
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
https.request = (_options, callback) => {
  const req = new EventEmitter();
  req.destroy = () => {};
  req.end = () => {
    const upstream = new PassThrough();
    upstream.statusCode = 200; upstream.headers = {}; upstream.complete = false;
    callback(upstream); upstream.write('partial');
    setTimeout(() => upstream.destroy(), 300);
  };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload))
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                wait_for_unix_socket(path, proc=proc)
                client = UnixHTTP(path)
                client.request('POST', '/v1/messages?beta=true', json.dumps({'model': 'allowed-model', 'messages': []}))
                response = client.getresponse()
                busy_client = UnixHTTP(path)
                busy_client.request('POST', '/other', '{}')
                self.assertEqual(busy_client.getresponse().status, 503)
                busy_client.close()
                try:
                    response.read()
                except (http.client.IncompleteRead, ConnectionError):
                    pass
                client.close()
                time.sleep(0.1)
                client = UnixHTTP(path)
                client.request('POST', '/other', '{}')
                self.assertEqual(client.getresponse().status, 403)
                client.close()
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stderr.close()

    def test_rejects_other_paths_and_models(self):
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            path = os.path.join(root, 'broker.sock')
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    stdout=subprocess.DEVNULL, stderr=self._stderr_file(path))
            try:
                wait_for_unix_socket(path, proc=proc, errors=path + '.stderr')
                for url, body in [('/other', {'model': 'allowed-model', 'messages': []}),
                                  ('/v1/messages?beta=false', {'model': 'allowed-model', 'messages': []}),
                                  ('/v1/messages?beta=true', {'model': 'wrong-model', 'messages': []}),
                                  ('/v1/messages', {'model': 'wrong-model', 'messages': []})]:
                    client = UnixHTTP(path)
                    client.request('POST', url, json.dumps(body), {'content-type': 'application/json'})
                    response = client.getresponse()
                    self.assertEqual(response.status, 403)
                    self.assertNotIn(b'fake-key', response.read())
                    client.close()
            finally:
                proc.terminate()
                proc.wait(timeout=5)

    def test_request_log_carries_the_token_counters_of_the_provider(self):
        """Every request must leave its token counters in the request log.

        The log lives next to the broker socket (``<socket>.requests.jsonl``),
        so the burn watchdog finds it without any configuration.
        """
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const body = JSON.stringify({id: 'msg_1', type: 'message', role: 'assistant', content: [],
                              usage: {input_tokens: 120, output_tokens: 34}});
https.request = (_options, callback) => {
  const req = new EventEmitter();
  req.destroy = () => {};
  req.end = () => {
    const upstream = new PassThrough();
    upstream.statusCode = 200; upstream.headers = {}; upstream.complete = true;
    callback(upstream); upstream.end(body);
  };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload))
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=self._stderr_file(path))
            try:
                wait_for_unix_socket(path, proc=proc, errors=path + '.stderr')
                client = UnixHTTP(path)
                client.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}),
                               {'content-type': 'application/json'})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                client.close()
                log = pathlib.Path(path + '.requests.jsonl')
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not (log.exists() and log.read_text().strip()):
                    time.sleep(0.05)
                self.assertTrue(log.exists(),
                                'the broker must write its request log next to the socket: %s' % log)
                rows = [line for line in log.read_text().splitlines() if line.strip()]
                self.assertTrue(rows, 'the request log must carry the request')
                row = json.loads(rows[0])
                self.assertIsInstance(row, dict)
                stamps = [value for key, value in row.items()
                          if ('ts' in key or 'time' in key) and isinstance(value, (str, int, float))]
                self.assertTrue(stamps, 'the request log row must carry a timestamp: %r' % row)
                counters = {key: value for key, value in row.items()
                            if isinstance(value, (int, float)) and not isinstance(value, bool)
                            and key not in ('id', 'seq', 'status', 'ms', 'ttfb_ms', 'in_flight',
                                            'req_bytes', 'resp_bytes')}
                self.assertIn(120, counters.values(),
                              'the request log row must carry the reported input tokens: %r' % row)
                self.assertIn(34, counters.values(),
                              'the request log row must carry the reported output tokens: %r' % row)
            finally:
                proc.terminate()
                proc.wait(timeout=5)

    def test_the_broker_writes_a_heartbeat_next_to_its_request_log(self):
        """The collector must stay verifiable while the fleet is quiet.

        The heartbeat tells the burn watchdog that a quiet hour is the zero burn
        of a live collector and not a collector that stopped (Refs #1671).
        """
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            path = os.path.join(root, 'broker.sock')
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    stdout=subprocess.DEVNULL, stderr=self._stderr_file(path))
            try:
                wait_for_unix_socket(path, proc=proc, errors=path + '.stderr')
                heartbeat = pathlib.Path(path + '.requests.jsonl.heartbeat')
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not (heartbeat.exists() and heartbeat.read_text().strip()):
                    time.sleep(0.05)
                self.assertTrue(heartbeat.exists(),
                                'the broker must write its heartbeat next to the request log: %s' % heartbeat)
            finally:
                proc.terminate()
                proc.wait(timeout=5)

    def test_slow_inference_outlives_upload_timeout(self):
        with tempfile.TemporaryDirectory(prefix='pb', dir=short_tmp_root()) as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            stub = pathlib.Path(root, 'stub.mjs')
            stub.write_text("import https from 'node:https'; import {Writable, Readable} from 'node:stream';\n"
                            "https.request = (_options, callback) => { const w = new Writable({write(c,e,done){done()}}); "
                            "w.on('finish', () => setTimeout(() => { const r = Readable.from(['{}']); "
                            "r.statusCode=200; r.headers={'content-type':'application/json'}; callback(r); }, 21000)); return w; };\n")
            path = os.path.join(root, 'broker.sock')
            proc = subprocess.Popen(['node', '--import', str(stub), str(BROKER), path, str(module), 'allowed-model'],
                                    stdout=subprocess.DEVNULL, stderr=self._stderr_file(path))
            try:
                wait_for_unix_socket(path, proc=proc, errors=path + '.stderr')
                client = UnixHTTP(path)
                client.timeout = 30
                client.request('POST', '/v1/messages', json.dumps({'model':'allowed-model','messages':[]}),
                               {'content-type':'application/json'})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b'{}')
                client.close()
            finally:
                proc.terminate()
                proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
