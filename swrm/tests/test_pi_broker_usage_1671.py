"""Reproduction of #1671: the broker must log the token counters of the provider.

The MiniMax burn reading of the fleet is the *real* hourly token consumption of
the provider, so the numbers have to be the ones MiniMax reported for a request
-- the figures an operator reconciles with the MiniMax console.  The broker is
the collection side of that reading: it writes one JSON line per request next
to its socket (``<socket>.requests.jsonl``), the shape
``tools/broker-log-summary.py`` already reads
(``{"ts","id","lane","status","err","ms","in_tok","out_tok"}``).

Two ways of getting the counters wrong are reproduced here, because both
silently distort the hourly burn and the alert that goes with it:

* counters scraped out of the whole answer text are not the usage of the
  request.  The ``input`` of a ``tool_use`` block is content and may carry
  numbers called ``input_tokens``/``output_tokens``; taking the largest of them
  inflates the hour by orders of magnitude (99999 instead of 120), and a normal
  tool call is enough to trigger it.
* only the last 4096 bytes of the answer were scanned, so the usage of a
  longer answer was lost: a streamed answer carries its input tokens in the
  first event, and the hour was under-counted instead.

On the batch base every test of this module fails: the broker of the base
writes no request log at all.
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

BROKER = pathlib.Path(__file__).resolve().parents[1] / 'bin' / 'pi-minimax-broker.mjs'
WATCHDOG = pathlib.Path(__file__).resolve().parents[2] / 'tools' / 'mm-burn-rate.py'
SCRATCH = pathlib.Path(__file__).resolve().parents[2] / '.swrm-tmp'

# The provider answer of a normal tool call: the content carries numbers that
# look like usage, the usage object carries the real ones.
PROVIDER_ANSWER = {
    'id': 'msg_1', 'type': 'message', 'role': 'assistant',
    'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'shell',
                 'input': {'command': 'ls', 'input_tokens': 99999, 'output_tokens': 88888}}],
    'stop_reason': 'tool_use',
    'usage': {'input_tokens': 120, 'output_tokens': 34},
}

# The provider answer of a streamed request: the input tokens are reported in
# the first event, the output tokens in the last one.
STREAMED_ANSWER = ''.join([
    'event: message_start\ndata: %s\n\n' % json.dumps(
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 120, 'output_tokens': 1}}}),
    'event: content_block_delta\ndata: %s\n\n' % json.dumps(
        {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'x' * 6000}}),
    'event: message_delta\ndata: %s\n\n' % json.dumps(
        {'type': 'message_delta', 'usage': {'output_tokens': 34}}),
])

# The provider is never reached: the answer is handed to the broker as is.
STUB = '''
const https = require('node:https');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const body = Buffer.from(process.env.FAKE_UPSTREAM_BODY || '', 'base64');
https.request = (_options, callback) => {
  const req = new EventEmitter();
  req.destroy = () => {};
  req.end = () => {
    const upstream = new PassThrough();
    upstream.statusCode = 200;
    upstream.headers = {'content-type': 'application/json'};
    upstream.complete = true;
    callback(upstream);
    const chunkSize = Number(process.env.FAKE_UPSTREAM_CHUNK_SIZE) || body.length;
    let offset = 0;
    const send = () => {
      if (offset >= body.length) { upstream.end(); return; }
      upstream.write(body.subarray(offset, offset + chunkSize));
      offset += chunkSize;
      setImmediate(send);
    };
    send();
  };
  return req;
};
'''


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=5)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def token_counters(row):
    """The input and the output token counters of a request log row."""
    incoming = outgoing = None
    for key, value in row.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        name = key.lower()
        if 'tok' not in name:
            continue
        if 'out' in name:
            outgoing = int(value)
        elif 'in' in name:
            incoming = int(value)
    return incoming, outgoing


def hourly_tokens(payload):
    """The hourly token reading of a payload, whatever the field is named."""
    for key, value in sorted(payload.items()):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        name = key.lower()
        if 'hour' in name and 'token' in name:
            return value
    return None


@unittest.skipUnless(shutil.which('node'), 'requires node')
class PiBrokerUsageParserTests(unittest.TestCase):
    def test_usage_parsing_is_independent_of_chunk_boundaries(self):
        """Exercise the production collector without needing a Unix socket."""
        data_stream = ''.join(line + '\n' for line in STREAMED_ANSWER.splitlines()
                              if not line.startswith('event:'))
        answers = [STREAMED_ANSWER, data_stream,
                   ' \r\n' + STREAMED_ANSWER.replace('\n', '\r\n'),
                   ' \n' + json.dumps(PROVIDER_ANSWER)]
        script = '''
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const assert = require('node:assert/strict');
const source = readFileSync(process.argv[1], 'utf8');
const collector = runInNewContext(source.slice(source.indexOf('const maxUsageBytes ='),
  source.indexOf('// Per-request audit log')) + '\\nusageCollector;');
for (const answer of JSON.parse(readFileSync(0, 'utf8'))) {
  const body = Buffer.from(answer);
  for (let size = 1; size <= 7; size++) {
    const usage = collector();
    for (let offset = 0; offset < body.length; offset += size)
      usage.push(body.subarray(offset, offset + size));
    usage.end();
    assert.equal(usage.result().in, 120, `input tokens with chunk size ${size}`);
    assert.equal(usage.result().out, 34, `output tokens with chunk size ${size}`);
  }
}
'''
        result = subprocess.run(['node', '-e', script, str(BROKER)],
                                input=json.dumps(answers), capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(shutil.which('node'), 'requires node')
@unittest.skipIf(os.name == "nt",
                 "pi broker binds a '/'-rooted unix-domain socket by design; no Windows transport")
class PiBrokerUsageTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.module = self.root / 'auth.mjs'
        self.module.write_text(
            'export class AuthStorage { static create() { return { getApiKey: async () => "fake-key" }; } }',
            encoding='utf-8')
        self.preload = self.root / 'fake.cjs'
        self.preload.write_text(STUB, encoding='utf-8')

    # -- helpers ---------------------------------------------------------
    def _env(self, **extra):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('OPENSWARM_', 'XSWARM_', 'MM_', 'PI_BROKER_'))}
        env['HOME'] = str(self.root)
        env['NODE_OPTIONS'] = '--require=' + str(self.preload)
        env.update(extra)
        return env

    def _start_broker(self, answer, log=None, chunk_size=0):
        """Start the broker with a stubbed provider and return (socket, log)."""
        log = log or (self.root / 'requests.jsonl')
        path = log.with_suffix('.sock')
        env = self._env(PI_BROKER_LOG=str(log),
                        FAKE_UPSTREAM_CHUNK_SIZE=str(chunk_size),
                        FAKE_UPSTREAM_BODY=base64.b64encode(answer).decode('ascii'))
        proc = subprocess.Popen(['node', str(BROKER), str(path), str(self.module), 'allowed-model'],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop, proc)
        deadline = time.monotonic() + 10
        while not path.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), 'the broker did not create its socket')
        return path, log

    @staticmethod
    def _stop(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - the broker is stubborn
                proc.kill()

    def _ask(self, path):
        """Send one request through the broker and read the answer."""
        client = UnixHTTP(str(path))
        client.request('POST', '/v1/messages',
                       json.dumps({'model': 'allowed-model', 'messages': []}),
                       {'content-type': 'application/json'})
        response = client.getresponse()
        payload = response.read()
        client.close()
        self.assertEqual(response.status, 200, payload)
        return payload

    def _rows(self, log, timeout=5):
        """The rows the broker wrote into its request log."""
        deadline = time.monotonic() + timeout
        rows = []
        while time.monotonic() < deadline:
            try:
                text = log.read_text(encoding='utf-8')
            except OSError:
                text = ''
            rows = []
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

    def _row(self, log, timeout=5):
        rows = self._rows(log, timeout)
        answered = [row for row in rows if row.get('status') == 200]
        return (answered or rows)[-1]

    # -- the collection side ---------------------------------------------
    def test_the_logged_counters_are_the_usage_of_the_provider(self):
        """The counters of the log are the usage of the provider, not content.

        The ``input`` of a tool call is content of the answer: its numbers are
        not the usage of the request and must not end up in the burn reading.
        """
        path, log = self._start_broker(json.dumps(PROVIDER_ANSWER).encode('utf-8'))
        self._ask(path)
        row = self._row(log)
        incoming, outgoing = token_counters(row)
        self.assertEqual(incoming, 120,
                         'the logged input tokens must be the usage of the provider: %r' % row)
        self.assertEqual(outgoing, 34,
                         'the logged output tokens must be the usage of the provider: %r' % row)

    def test_the_usage_of_a_long_answer_is_not_lost(self):
        """The usage of an answer that is bigger than a scan window is kept.

        A streamed answer carries the input tokens of the request in its first
        event; a scan of the last bytes of the answer loses them and the hour
        is under-counted.
        """
        path, log = self._start_broker(STREAMED_ANSWER.encode('utf-8'))
        self._ask(path)
        row = self._row(log)
        incoming, outgoing = token_counters(row)
        self.assertEqual(incoming, 120,
                         'the input tokens of the provider must survive a long answer: %r' % row)
        self.assertEqual(outgoing, 34,
                         'the output tokens of the provider must survive a long answer: %r' % row)

    def test_sse_usage_survives_network_chunk_boundaries(self):
        """Partial event/data prefixes must not select the JSON parser."""
        streams = {
            'event': STREAMED_ANSWER,
            'data': ''.join(line + '\n' for line in STREAMED_ANSWER.splitlines()
                            if not line.startswith('event:')),
        }
        for name, stream in streams.items():
            for size in range(1, 7):
                with self.subTest(prefix=name, chunk_size=size):
                    answer = stream.encode('utf-8')
                    path, log = self._start_broker(
                        answer, self.root / ('%s-%s.jsonl' % (name, size)), chunk_size=size)
                    self.assertEqual(self._ask(path), answer)
                    self.assertEqual(token_counters(self._row(log)), (120, 34))

    def test_json_usage_survives_network_chunk_boundaries(self):
        answer = json.dumps(PROVIDER_ANSWER).encode('utf-8')
        path, log = self._start_broker(answer, chunk_size=1)
        self.assertEqual(self._ask(path), answer)
        self.assertEqual(token_counters(self._row(log)), (120, 34))

    def test_the_request_of_the_broker_reaches_the_burn_reading(self):
        """The real request log of the broker is what the burn reading measures.

        This is the chain of #1671: the broker logs the usage of the provider,
        the burn watchdog reads that log and reports the hour as the token
        volume of the provider -- not a dispatch count proxy.
        """
        path, log = self._start_broker(json.dumps(PROVIDER_ANSWER).encode('utf-8'))
        self._ask(path)
        row = self._row(log)
        incoming, outgoing = token_counters(row)
        self.assertEqual((incoming, outgoing), (120, 34),
                         'the request log must carry the usage of the provider: %r' % row)
        history = self.root / 'history.jsonl'
        history.write_text('', encoding='utf-8')
        status = self.root / 'status.json'
        env = self._env()
        reading = subprocess.run(
            [sys.executable, str(WATCHDOG), '--history', str(history), '--broker-log', str(log),
             '--status-file', str(status), '--json'],
            capture_output=True, text=True, timeout=30, env=env, cwd=str(self.root))
        self.assertIn(reading.returncode, (0, 1),
                      'the burn watchdog failed: %s' % reading.stderr)
        payload = json.loads(reading.stdout)
        self.assertEqual(hourly_tokens(payload), 154,
                         'the burn reading must be the token volume of the provider: %r' % payload)
        snapshot = json.loads(status.read_text(encoding='utf-8'))
        self.assertEqual(hourly_tokens(snapshot), 154,
                         'the burn snapshot must carry the token volume of the provider: %r' % snapshot)


if __name__ == '__main__':
    unittest.main()
