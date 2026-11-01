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

    def test_token_plan_quota_exhausted_trips_breaker_and_cooldown_recovers(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            counter = pathlib.Path(root, 'upstream_calls')
            mode_file = pathlib.Path(root, 'mode')
            mode_file.write_text('quota')
            log = pathlib.Path(root, 'requests.jsonl')
            quota_err = json.dumps({'type': 'rate_limit_error', 'message': '已达到 Token Plan 用量上限…(2056)'})
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const fs = require('node:fs');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
https.request = (_options, callback) => {
  const count = Number(fs.existsSync(process.env.COUNTER) ? fs.readFileSync(process.env.COUNTER, 'utf8') : 0) + 1;
  fs.writeFileSync(process.env.COUNTER, String(count));
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => {
    const mode = fs.existsSync(process.env.MODE) ? fs.readFileSync(process.env.MODE, 'utf8').trim() : 'quota';
    const upstream = new PassThrough(); upstream.headers = {'content-type': 'application/json'}; upstream.complete = true;
    if (mode === 'quota') {
      upstream.statusCode = 429;
      callback(upstream);
      upstream.end(JSON.stringify({type: 'rate_limit_error', message: '已达到 Token Plan 用量上限…(2056)'}));
    } else {
      upstream.statusCode = 200;
      callback(upstream);
      upstream.end(JSON.stringify({id: 'msg_1', type: 'message'}));
    }
  };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload),
                       COUNTER=str(counter), MODE=str(mode_file), PI_BROKER_LOG=str(log),
                       PI_BROKER_QUOTA_COOLDOWN_S='0.3')
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(path), 'broker did not start')

                c1 = UnixHTTP(path)
                c1.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}),
                           {'content-type': 'application/json'})
                r1 = c1.getresponse()
                self.assertEqual(r1.status, 429)
                self.assertEqual(json.loads(r1.read().decode('utf8')),
                                 {'type': 'rate_limit_error', 'message': '已达到 Token Plan 用量上限…(2056)'})
                c1.close()
                self.assertEqual(counter.read_text(), '1')

                for _ in range(2):
                    c = UnixHTTP(path)
                    c.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}),
                              {'content-type': 'application/json'})
                    r = c.getresponse()
                    self.assertEqual(r.status, 429)
                    self.assertEqual(json.loads(r.read().decode('utf8')),
                                     {'type': 'rate_limit_error', 'message': '已达到 Token Plan 用量上限…(2056)'})
                    c.close()
                self.assertEqual(counter.read_text(), '1')

                deadline = time.monotonic() + 3
                while (not log.exists() or len(log.read_text().splitlines()) < 3) and time.monotonic() < deadline:
                    time.sleep(0.02)
                rows = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(rows[1]['status'], 429)
                self.assertEqual(rows[1]['breaker'], 'open')
                self.assertIn('Token Plan', rows[1]['err'])
                self.assertEqual(rows[2]['breaker'], 'open')

                mode_file.write_text('ok')
                time.sleep(0.35)

                c4 = UnixHTTP(path)
                c4.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}),
                           {'content-type': 'application/json'})
                r4 = c4.getresponse()
                self.assertEqual(r4.status, 200)
                r4.read()
                c4.close()
                self.assertEqual(counter.read_text(), '2')

                c5 = UnixHTTP(path)
                c5.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}),
                           {'content-type': 'application/json'})
                r5 = c5.getresponse()
                self.assertEqual(r5.status, 200)
                r5.read()
                c5.close()
                self.assertEqual(counter.read_text(), '3')
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stderr.close()

    def test_half_open_allows_only_single_probe_concurrently(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            counter = pathlib.Path(root, 'upstream_calls')
            mode_file = pathlib.Path(root, 'mode')
            mode_file.write_text('quota')
            probe_started = pathlib.Path(root, 'probe_started')
            probe_release = pathlib.Path(root, 'probe_release')
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const fs = require('node:fs');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
https.request = (_options, callback) => {
  const count = Number(fs.existsSync(process.env.COUNTER) ? fs.readFileSync(process.env.COUNTER, 'utf8') : 0) + 1;
  fs.writeFileSync(process.env.COUNTER, String(count));
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => {
    const mode = fs.existsSync(process.env.MODE) ? fs.readFileSync(process.env.MODE, 'utf8').trim() : 'quota';
    const upstream = new PassThrough(); upstream.headers = {'content-type': 'application/json'}; upstream.complete = true;
    if (mode === 'quota') {
      upstream.statusCode = 429;
      callback(upstream);
      upstream.end(JSON.stringify({type: 'rate_limit_error', message: '已达到 Token Plan 用量上限…(2056)'}));
    } else if (mode === 'slow_probe') {
      fs.writeFileSync(process.env.PROBE_STARTED, '1');
      const waitRelease = () => {
        if (fs.existsSync(process.env.PROBE_RELEASE)) {
          upstream.statusCode = 200;
          callback(upstream);
          upstream.end(JSON.stringify({id: 'probe_ok'}));
        } else {
          setTimeout(waitRelease, 10);
        }
      };
      waitRelease();
    }
  };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload),
                       COUNTER=str(counter), MODE=str(mode_file),
                       PROBE_STARTED=str(probe_started), PROBE_RELEASE=str(probe_release),
                       PI_BROKER_QUOTA_COOLDOWN_S='0.3')
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(path), 'broker did not start')

                c1 = UnixHTTP(path)
                c1.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}))
                r1 = c1.getresponse()
                self.assertEqual(r1.status, 429)
                r1.read()
                c1.close()
                self.assertEqual(counter.read_text(), '1')

                mode_file.write_text('slow_probe')
                time.sleep(0.35)

                probe_res = []
                def send_probe():
                    cp = UnixHTTP(path)
                    cp.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}))
                    rp = cp.getresponse()
                    probe_res.append(rp.status)
                    rp.read()
                    cp.close()

                t = threading.Thread(target=send_probe)
                t.start()

                deadline = time.monotonic() + 3
                while not probe_started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(probe_started.exists())
                self.assertEqual(counter.read_text(), '2')

                c2 = UnixHTTP(path)
                c2.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}))
                r2 = c2.getresponse()
                self.assertEqual(r2.status, 429)
                r2.read()
                c2.close()
                self.assertEqual(counter.read_text(), '2')

                probe_release.touch()
                t.join(timeout=5)
                self.assertFalse(t.is_alive())
                self.assertEqual(probe_res, [200])
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stderr.close()

    def test_normal_rate_limit_does_not_trip_breaker(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            counter = pathlib.Path(root, 'upstream_calls')
            log = pathlib.Path(root, 'requests.jsonl')
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const fs = require('node:fs');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
https.request = (_options, callback) => {
  const count = Number(fs.existsSync(process.env.COUNTER) ? fs.readFileSync(process.env.COUNTER, 'utf8') : 0) + 1;
  fs.writeFileSync(process.env.COUNTER, String(count));
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => {
    const upstream = new PassThrough(); upstream.statusCode = 429; upstream.headers = {'content-type': 'application/json'}; upstream.complete = true;
    callback(upstream);
    upstream.end(JSON.stringify({type: 'rate_limit_error', message: 'Rate limit reached: 60 RPM'}));
  };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload),
                       COUNTER=str(counter), PI_BROKER_LOG=str(log),
                       PI_BROKER_QUOTA_COOLDOWN_S='300')
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(path), 'broker did not start')

                c1 = UnixHTTP(path)
                c1.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}))
                r1 = c1.getresponse()
                self.assertEqual(r1.status, 429)
                r1.read()
                c1.close()
                self.assertEqual(counter.read_text(), '1')

                c2 = UnixHTTP(path)
                c2.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}))
                r2 = c2.getresponse()
                self.assertEqual(r2.status, 429)
                r2.read()
                c2.close()
                self.assertEqual(counter.read_text(), '2')

                deadline = time.monotonic() + 3
                while (not log.exists() or len(log.read_text().splitlines()) < 2) and time.monotonic() < deadline:
                    time.sleep(0.02)
                rows = [json.loads(line) for line in log.read_text().splitlines()]
                for row in rows:
                    self.assertNotIn('breaker', row)
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stderr.close()

    def test_custom_quota_regex_override(self):
        with tempfile.TemporaryDirectory() as root:
            module = pathlib.Path(root, 'auth.mjs')
            module.write_text('export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }')
            counter = pathlib.Path(root, 'upstream_calls')
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const fs = require('node:fs');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
https.request = (_options, callback) => {
  const count = Number(fs.existsSync(process.env.COUNTER) ? fs.readFileSync(process.env.COUNTER, 'utf8') : 0) + 1;
  fs.writeFileSync(process.env.COUNTER, String(count));
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => {
    const upstream = new PassThrough(); upstream.statusCode = 429; upstream.headers = {'content-type': 'application/json'}; upstream.complete = true;
    callback(upstream);
    upstream.end(JSON.stringify({error: {type: 'rate_limit_error', message: 'tier_cap_exceeded'}}));
  };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload),
                       COUNTER=str(counter), PI_BROKER_QUOTA_RE='tier_cap_exceeded',
                       PI_BROKER_QUOTA_COOLDOWN_S='60')
            proc = subprocess.Popen(['node', str(BROKER), path, str(module), 'allowed-model'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(path), 'broker did not start')

                c1 = UnixHTTP(path)
                c1.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}))
                r1 = c1.getresponse()
                self.assertEqual(r1.status, 429)
                r1.read()
                c1.close()
                self.assertEqual(counter.read_text(), '1')

                c2 = UnixHTTP(path)
                c2.request('POST', '/v1/messages', json.dumps({'model': 'allowed-model', 'messages': []}))
                r2 = c2.getresponse()
                self.assertEqual(r2.status, 429)
                r2.read()
                c2.close()
                self.assertEqual(counter.read_text(), '1')
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stderr.close()


if __name__ == '__main__':
    unittest.main()
