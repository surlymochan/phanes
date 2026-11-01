"""Verify that the credential broker rejects off-target requests before networking."""
import http.client
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager

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
class PiBrokerTests(unittest.TestCase):
    def test_beta_request_preserves_upstream_opt_in(self):
        with tempfile.TemporaryDirectory() as root:
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
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(path))
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

    def test_request_log_records_lane_tokens_and_stream_error(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
https.request = (options, callback) => {
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => {
    const upstream = new PassThrough(); upstream.statusCode = 200; upstream.headers = {'content-type': 'text/event-stream'}; upstream.complete = true;
    callback(upstream);
    upstream.write('event: message_start\\ndata: {"type":"message_start","message":{"usage":{"input_tokens":42,"output_tokens":1}}}\\n\\n');
    upstream.end('event: error\\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\\n\\n');
  };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            log = pathlib.Path(root, 'requests.jsonl')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload), PI_BROKER_LOG=str(log))
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                client = UnixHTTP(path)
                body = json.dumps({'model': 'allowed-model', 'messages': [
                    {'role': 'user', 'content': 'work in /home/x/.claude/openswarm/lanes/M7/wt now'}]})
                client.request('POST', '/v1/messages', body)
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                client.close()
                deadline = time.monotonic() + 3
                while not log.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                row = json.loads(log.read_text().splitlines()[0])
                self.assertEqual(row['lane'], 'M7')
                self.assertEqual(row['req_bytes'], len(body))
                self.assertEqual(row['in_tok'], 42)
                self.assertEqual(row['http_status'], 200)
                self.assertEqual(row['status'], 529)
                self.assertEqual(row['stream_err'], 'overloaded_error')
                self.assertEqual(row['err'], 'Overloaded')
                self.assertNotIn('fake-key', log.read_text())
                self.assertNotIn('work in', log.read_text())
            finally:
                proc.terminate()
                proc.wait(timeout=5)

    def test_model_text_mentioning_errors_is_not_a_stream_error(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
https.request = (options, callback) => {
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => {
    const upstream = new PassThrough(); upstream.statusCode = 200; upstream.headers = {'content-type': 'text/event-stream'}; upstream.complete = true;
    callback(upstream);
    upstream.write('event: message_start\\ndata: {"type":"message_start","message":{"usage":{"input_tokens":42,"output_tokens":1}}}\\n\\n');
    upstream.end('event: content_block_delta\\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"see event: error and \\\\"type\\\\":\\\\"error\\\\" docs"}}\\n\\n');
  };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            log = pathlib.Path(root, 'requests.jsonl')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload), PI_BROKER_LOG=str(log))
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                client = UnixHTTP(path)
                body = json.dumps({'model': 'allowed-model', 'messages': [
                    {'role': 'user', 'content': 'work in /home/x/.claude/openswarm/lanes/M7/wt now'}]})
                client.request('POST', '/v1/messages', body)
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                client.close()
                deadline = time.monotonic() + 3
                while not log.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                row = json.loads(log.read_text().splitlines()[0])
                self.assertEqual(row['lane'], 'M7')
                self.assertEqual(row['req_bytes'], len(body))
                self.assertEqual(row['in_tok'], 42)
                self.assertEqual(row['status'], 200)
                self.assertNotIn('stream_err', row)
                self.assertNotIn('fake-key', log.read_text())
                self.assertNotIn('work in', log.read_text())
            finally:
                proc.terminate()
                proc.wait(timeout=5)

    def test_refused_request_is_logged_next_to_the_socket(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            path = os.path.join(root, 'broker.sock')
            env = {k: v for k, v in os.environ.items() if k != 'PI_BROKER_LOG'}
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                client = UnixHTTP(path)
                client.request('GET', '/other')
                self.assertEqual(client.getresponse().status, 403)
                client.close()
                log = pathlib.Path(path + '.requests.jsonl')
                deadline = time.monotonic() + 3
                while not log.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                row = json.loads(log.read_text().splitlines()[0])
                self.assertEqual((row['status'], row['err']), (403, 'request refused'))
            finally:
                proc.terminate()
                proc.wait(timeout=5)

    def test_aborted_upstream_releases_in_flight_slot(self):
        with tempfile.TemporaryDirectory() as root:
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
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not os.path.exists(path):
                    status = proc.poll()
                    detail = proc.stderr.read().decode(errors='replace') if status is not None else 'still running after 5 seconds'
                    self.fail('broker did not create its socket (exit=%s): %s' % (status, detail))
                client = UnixHTTP(path)
                client.request('POST', '/v1/messages?beta=true', json.dumps({'model': 'allowed-model', 'messages': []}))
                response = client.getresponse()
                busy_client = UnixHTTP(path)
                busy_client.request('POST', '/other', '{}')
                # Capacity is now four. An otherwise invalid request must not
                # pretend the single active slot made the broker unavailable.
                self.assertEqual(busy_client.getresponse().status, 403)
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

    def test_four_requests_stream_concurrently_and_release_independently(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            marker = pathlib.Path(root, 'started')
            release = pathlib.Path(root, 'release')
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const fs = require('node:fs');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
https.request = (_options, callback) => {
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => {
    fs.appendFileSync(process.env.STARTED, '1\\n');
    const upstream = new PassThrough(); upstream.statusCode = 200; upstream.headers = {}; upstream.complete = true;
    callback(upstream);
    const finish = () => {
      if (fs.existsSync(process.env.RELEASE)) upstream.end('ok');
      else setTimeout(finish, 5);
    };
    finish();
  };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload), STARTED=str(marker), RELEASE=str(release))
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(path), 'broker did not start')
                replies = []
                def request():
                    client = UnixHTTP(path)
                    client.request('POST', '/v1/messages', json.dumps({'model':'allowed-model','messages':[]}))
                    response = client.getresponse()
                    replies.append((response.status, response.read()))
                    client.close()
                workers = [threading.Thread(target=request) for _ in range(4)]
                for worker in workers: worker.start()
                while (not marker.exists() or len(marker.read_text().splitlines()) != 4) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(marker.read_text().splitlines()), 4)
                saturated = UnixHTTP(path)
                saturated.request('POST', '/v1/messages', json.dumps({'model':'allowed-model','messages':[]}))
                self.assertEqual(saturated.getresponse().status, 503)
                saturated.close()
                release.touch()
                for worker in workers: worker.join(timeout=5)
                self.assertTrue(all(not worker.is_alive() for worker in workers))
                self.assertEqual(sorted(status for status, _body in replies), [200, 200, 200, 200])
                client = UnixHTTP(path)
                client.request('POST', '/other', '{}')
                self.assertEqual(client.getresponse().status, 403)
                client.close()
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stderr.close()

    def test_cancelled_request_releases_exactly_one_of_four_slots(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            marker = pathlib.Path(root, 'started')
            release = pathlib.Path(root, 'release')
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https'); const fs = require('node:fs');
const {EventEmitter} = require('node:events'); const {PassThrough} = require('node:stream');
https.request = (_options, callback) => {
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => {
    const count = Number(fs.existsSync(process.env.STARTED) ? fs.readFileSync(process.env.STARTED, 'utf8') : 0) + 1;
    fs.writeFileSync(process.env.STARTED, String(count));
    const upstream = new PassThrough(); upstream.statusCode = 200; upstream.headers = {}; upstream.complete = true;
    callback(upstream);
    upstream.write('started-');
    const finish = () => {
      if (fs.existsSync(process.env.RELEASE)) upstream.end('ok-' + count);
      else setTimeout(finish, 5);
    };
    finish();
  }; return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload), STARTED=str(marker), RELEASE=str(release))
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            active, replacement = [], None
            def start_valid():
                body = json.dumps({'model':'allowed-model','messages':[]}).encode()
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(3); client.connect(path)
                client.sendall(b'POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
                               + b'Content-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
                return client
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(path), 'broker did not start')
                active = [start_valid() for _ in range(4)]
                while (not marker.exists() or marker.read_text() != '4') and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(marker.read_text(), '4')
                blocked = UnixHTTP(path)
                blocked.request('POST', '/v1/messages', json.dumps({'model':'allowed-model','messages':[]}))
                self.assertEqual(blocked.getresponse().status, 503); blocked.close()
                active[0].close()
                time.sleep(0.08)
                replacement = start_valid()
                while marker.read_text() != '5' and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(marker.read_text(), '5')
                blocked = UnixHTTP(path)
                blocked.request('POST', '/v1/messages', json.dumps({'model':'allowed-model','messages':[]}))
                self.assertEqual(blocked.getresponse().status, 503); blocked.close()
                self.assertTrue(all(b'200' in client.recv(4096) for client in active[1:]))
                self.assertIn(b'200', replacement.recv(4096))
                release.touch()
                for client in active[1:]: client.close()
                replacement.close()
            finally:
                for client in active:
                    try: client.close()
                    except OSError: pass
                if replacement is not None:
                    try: replacement.close()
                    except OSError: pass
                proc.terminate(); proc.wait(timeout=5); proc.stderr.close()

    def test_more_than_64_requests_keep_broker_running(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            counter = pathlib.Path(root, 'counter')
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const fs = require('node:fs');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
https.request = (_options, callback) => {
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => {
    const count = Number(fs.existsSync(process.env.COUNTER) ? fs.readFileSync(process.env.COUNTER, 'utf8') : 0) + 1;
    fs.writeFileSync(process.env.COUNTER, String(count));
    const upstream = new PassThrough(); upstream.statusCode = 200; upstream.headers = {}; upstream.complete = true;
    callback(upstream); upstream.end('ok-' + count);
  };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload), COUNTER=str(counter))
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 8
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(path), 'broker did not start')
                for _ in range(68):
                    client = UnixHTTP(path)
                    client.request('POST', '/v1/messages', json.dumps({'model':'allowed-model','messages':[]}))
                    self.assertEqual(client.getresponse().status, 200)
                    client.close()
                self.assertIsNone(proc.poll(), 'normal request volume must not rotate the broker')
                client = UnixHTTP(path)
                client.request('POST', '/v1/messages', json.dumps({'model':'allowed-model','messages':[]}))
                self.assertEqual(client.getresponse().status, 200)
                client.close()
                self.assertEqual(counter.read_text(), '69')
            finally:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=5)
                proc.stderr.close()

    def test_rejects_other_paths_and_models(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            path = os.path.join(root, 'broker.sock')
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(path), 'broker did not start')
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

    def test_slow_inference_outlives_upload_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            # The upload timeout is shortened instead of waiting out the production
            # 20s value: the inference below still outlives it, which is what the
            # broker has to survive.
            upload_timeout_ms = 1000
            inference_delay_ms = upload_timeout_ms + 1500
            stub = pathlib.Path(root, 'stub.mjs')
            stub.write_text("import https from 'node:https'; import {Writable, Readable} from 'node:stream';\n"
                            "https.request = (_options, callback) => { const w = new Writable({write(c,e,done){done()}}); "
                            "w.on('finish', () => setTimeout(() => { const r = Readable.from(['{}']); "
                            "r.statusCode=200; r.headers={'content-type':'application/json'}; callback(r); }, "
                            + str(inference_delay_ms) + ")); return w; };\n")
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, OPENSWARM_BROKER_UPLOAD_TIMEOUT_MS=str(upload_timeout_ms))
            proc = subprocess.Popen(['node', '--import', str(stub), str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(path), 'broker did not start')
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


@unittest.skipUnless(shutil.which('node'), 'requires node')
class PiBrokerUpstreamErrorTests(unittest.TestCase):
    @contextmanager
    def broker(self, status, body, headers=None, streaming=False):
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            module = root / 'auth.mjs'
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            config = root / 'response.json'
            config.write_text(json.dumps({'status': status, 'body': body.decode('utf-8'),
                                          'headers': headers or {}, 'streaming': streaming}))
            preload = root / 'fake.cjs'
            preload.write_text('''
const https = require('node:https');
const fs = require('node:fs');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const fixture = JSON.parse(fs.readFileSync(process.env.ERROR_FIXTURE, 'utf8'));
https.request = (_options, callback) => {
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => {
    const upstream = new PassThrough();
    upstream.statusCode = fixture.status; upstream.headers = fixture.headers;
    upstream.complete = !fixture.streaming;
    upstream.on('close', () => fs.appendFileSync(process.env.UPSTREAM_CLOSED, 'closed\\n'));
    callback(upstream);
    if (!fixture.streaming) upstream.end(Buffer.from(fixture.body));
    else {
      const chunk = Buffer.from(fixture.body);
      const send = () => {
        if (upstream.destroyed) return;
        upstream.write(chunk);
        setImmediate(send);
      };
      send();
    }
  };
  return req;
};
''')
            path = str(root / 'broker.sock')
            log = root / 'requests.jsonl'
            closed = root / 'upstream-closed'
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload), PI_BROKER_LOG=str(log),
                       ERROR_FIXTURE=str(config), UPSTREAM_CLOSED=str(closed))
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(path), 'broker did not create its socket')
                yield path, log, closed, proc
            finally:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                proc.stderr.close()

    def request(self, path):
        client = UnixHTTP(path)
        try:
            client.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}),
                           {'authorization': 'request-only-value', 'x-client-sensitive': 'request-only-value'})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def rows(self, log, count=1):
        deadline = time.monotonic() + 3
        rows = []
        while time.monotonic() < deadline:
            if log.exists():
                rows = [json.loads(line) for line in log.read_text().splitlines()]
                if len(rows) >= count:
                    return rows
            time.sleep(0.01)
        self.fail('expected %s completed request log rows, got %s' % (count, len(rows)))

    def test_token_plan_error_preserves_status_body_and_logs_provider_details(self):
        message = '已达到 Token Plan 用量上限 (2056)'
        body = json.dumps({'error': {'type': 'rate_limit_error', 'message': message},
                           'base_resp': {'status_code': 2056}}, ensure_ascii=False).encode()
        headers = {'retry-after': '120', 'x-ratelimit-reset': '1800000123',
                   'x-ratelimit-reset-tokens': '5h', 'authorization': 'fake-key', 'x-debug-key': 'fake-key'}
        with self.broker(429, body, headers) as (path, log, _closed, _proc):
            status, returned_headers, returned_body = self.request(path)
            self.assertEqual(status, 429)
            self.assertEqual(returned_headers['content-type'], 'application/json')
            self.assertEqual(returned_body, body)
            row = self.rows(log)[0]
            self.assertEqual(row['status'], 429)
            self.assertEqual(row['upstream_type'], 'rate_limit_error')
            self.assertEqual(row['provider_code'], 2056)
            self.assertEqual(row['err'], message)
            self.assertEqual(row['retry_after'], {'retry-after': '120', 'x-ratelimit-reset': '1800000123',
                                                   'x-ratelimit-reset-tokens': '5h'})
            for forbidden in ('fake-key', 'request-only-value', 'x-debug-key'):
                self.assertNotIn(forbidden, json.dumps(returned_headers))
                self.assertNotIn(forbidden, returned_body.decode())
                self.assertNotIn(forbidden, log.read_text())

    def test_top_level_error_and_unicode_log_message_limit(self):
        message = '配额😀已用尽' * 100
        body = json.dumps({'type': 'rate_limit_error', 'message': message,
                           'base_resp': {'status_code': 2056}}, ensure_ascii=False).encode()
        with self.broker(429, body) as (path, log, _closed, _proc):
            self.assertEqual(self.request(path)[2], body)
            row = self.rows(log)[0]
            self.assertEqual(row['upstream_type'], 'rate_limit_error')
            self.assertEqual(row['provider_code'], 2056)
            self.assertEqual(row['err'], message[:160])

    def test_html_error_is_logged_as_non_json_and_keeps_upstream_status(self):
        body = b'<html>bad gateway</html>'
        with self.broker(502, body) as (path, log, _closed, _proc):
            status, headers, returned = self.request(path)
            self.assertEqual(status, 502)
            self.assertEqual(headers['content-type'], 'application/json')
            self.assertEqual(returned, body)
            self.assertEqual(self.rows(log)[0]['err'], 'non_json_body')

    def test_exact_4kb_json_error_is_not_truncated(self):
        prefix = b'{"error":{"type":"api_error","message":"'
        suffix = b'"}}'
        body = prefix + b'x' * (4096 - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(body), 4096)
        with self.broker(500, body) as (path, log, _closed, _proc):
            status, _headers, returned = self.request(path)
            self.assertEqual(status, 500)
            self.assertEqual(returned, body)
            row = self.rows(log)[0]
            self.assertEqual(row['upstream_type'], 'api_error')
            self.assertEqual(row['err'], 'x' * 160)

    def test_unending_oversize_body_is_capped_and_releases_slots(self):
        # The fake provider never emits end; the broker must stop consuming at its cap.
        chunk = b'x' * 1024
        with self.broker(429, chunk, streaming=True) as (path, log, closed, proc):
            for _ in range(6):
                status, headers, body = self.request(path)
                self.assertEqual(status, 429)
                self.assertEqual(headers['content-type'], 'application/json')
                self.assertEqual(body, chunk * 4)
            rows = self.rows(log, 6)
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(row['err'] == 'non_json_body' for row in rows))
            deadline = time.monotonic() + 3
            while (not closed.exists() or len(closed.read_text().splitlines()) < 6) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(closed.read_text().splitlines()), 6)
            self.assertIsNone(proc.poll(), 'oversize responses must not terminate the broker')


if __name__ == '__main__':
    unittest.main()
