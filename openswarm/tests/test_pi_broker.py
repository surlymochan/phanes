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
            preload = pathlib.Path(root, 'fake.cjs')
            preload.write_text('''
const https = require('node:https');
const fs = require('node:fs');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
https.request = (_options, callback) => {
  const req = new EventEmitter(); req.destroy = () => {};
  req.end = () => {
    const count = Number(fs.existsSync(process.env.STARTED) ? fs.readFileSync(process.env.STARTED, 'utf8') : 0) + 1;
    fs.writeFileSync(process.env.STARTED, String(count));
    const upstream = new PassThrough(); upstream.statusCode = 200; upstream.headers = {}; upstream.complete = true;
    callback(upstream); setTimeout(() => upstream.end('ok-' + count), 300);
  };
  return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload), STARTED=str(marker))
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
                while (not marker.exists() or marker.read_text() != '4') and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(marker.read_text(), '4')
                saturated = UnixHTTP(path)
                saturated.request('POST', '/v1/messages', json.dumps({'model':'allowed-model','messages':[]}))
                self.assertEqual(saturated.getresponse().status, 503)
                saturated.close()
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
    callback(upstream); setTimeout(() => upstream.end('ok-' + count), 500);
  }; return req;
};
''')
            path = os.path.join(root, 'broker.sock')
            env = dict(os.environ, NODE_OPTIONS='--require=' + str(preload), STARTED=str(marker))
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
            stub = pathlib.Path(root, 'stub.mjs')
            stub.write_text("import https from 'node:https'; import {Writable, Readable} from 'node:stream';\n"
                            "https.request = (_options, callback) => { const w = new Writable({write(c,e,done){done()}}); "
                            "w.on('finish', () => setTimeout(() => { const r = Readable.from(['{}']); "
                            "r.statusCode=200; r.headers={'content-type':'application/json'}; callback(r); }, 21000)); return w; };\n")
            path = os.path.join(root, 'broker.sock')
            proc = subprocess.Popen(['node', '--import', str(stub), str(BROKER), path, str(module), 'allowed-model'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


if __name__ == '__main__':
    unittest.main()
