#!/usr/bin/env node
// A private, single-model Anthropic proxy. The Pi credential stays in this process.
import { createServer } from 'node:http';
import https from 'node:https';
import { appendFileSync, chmodSync, lstatSync, readFileSync, readdirSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, basename } from 'node:path';
import { pathToFileURL } from 'node:url';

const [socket, authModule, model] = process.argv.slice(2);
if (!socket || !socket.startsWith('/') || !authModule || !model) throw new Error('socket, auth module and model are required');
try { lstatSync(socket); throw new Error('socket path already exists'); } catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
const { AuthStorage } = await import(pathToFileURL(authModule).href);
let key;
try { key = await AuthStorage.create().getApiKey('minimax-cn'); }
catch { throw new Error('MiniMax credential unavailable'); }
if (typeof key !== 'string' || !key || /[\r\n]/.test(key)) throw new Error('MiniMax credential unavailable');
const maxBytes = 8 * 1024 * 1024;
// The bound of the answer the broker keeps for its usage object. A bigger answer
// leaves the counters unknown: a missing usage is unknown, never zero.
const maxUsageBytes = 8 * 1024 * 1024;
// The usage of the provider answer is the real burn of a request (Refs #1671).
// It is read from the usage object of the answer only: the content of an answer
// (the input of a tool call) may carry numbers with the same names and is not
// usage, so a scan of the answer text would inflate the hour by orders of
// magnitude (a normal tool call is enough to trigger it).
const usageCounters = (usage) => {
  if (!usage || typeof usage !== 'object') return null;
  const incoming = Number(usage.input_tokens);
  const outgoing = Number(usage.output_tokens);
  return {in: Number.isFinite(incoming) ? incoming : null,
          out: Number.isFinite(outgoing) ? outgoing : null};
};
// A JSON answer carries the usage at its top level; a streamed answer carries it
// in the message of its first event and in the usage of its delta events.
const usageOfPayload = (payload) => {
  if (!payload || typeof payload !== 'object') return null;
  const direct = usageCounters(payload.usage);
  if (direct) return direct;
  const message = payload.message;
  if (message && typeof message === 'object') return usageCounters(message.usage);
  return null;
};
// The usage of a streamed answer: every event carries its own usage object.
const usageOfEvents = (text) => {
  let incoming = null, outgoing = null;
  for (const line of text.split(/\r?\n/)) {
    if (!line.startsWith('data:')) continue;
    const data = line.slice(5).trim();
    if (!data || data === '[DONE]') continue;
    let payload;
    try { payload = JSON.parse(data); } catch { continue; }
    const usage = usageOfPayload(payload);
    if (!usage) continue;
    // The counters of the provider are cumulative: the highest one of the answer
    // is the total it reported.
    if (usage.in !== null) incoming = incoming === null ? usage.in : Math.max(incoming, usage.in);
    if (usage.out !== null) outgoing = outgoing === null ? usage.out : Math.max(outgoing, usage.out);
  }
  return {in: incoming, out: outgoing};
};
// The usage is collected while the answer streams: a streamed answer is read
// event by event (its first event carries the input tokens of the request, so a
// scan window would lose them), a JSON answer is parsed when it is complete.
const usageCollector = () => {
  let mode = null;    // 'sse' or 'json'
  let head = '';
  let pending = '';   // the incomplete event of a streamed answer
  let body = '';      // the JSON answer
  let incoming = null, outgoing = null;
  const take = (usage) => {
    if (!usage) return;
    if (usage.in !== null) incoming = incoming === null ? usage.in : Math.max(incoming, usage.in);
    if (usage.out !== null) outgoing = outgoing === null ? usage.out : Math.max(outgoing, usage.out);
  };
  const feedEvents = (text) => {
    pending += text;
    let match;
    while ((match = /\r?\n\r?\n/.exec(pending)) !== null) {
      take(usageOfEvents(pending.slice(0, match.index)));
      pending = pending.slice(match.index + match[0].length);
    }
  };
  return {
    push(chunk) {
      const text = chunk.toString('utf8');
      if (mode === null) {
        head += text;
        const trimmed = head.trimStart();
        if (!trimmed) return;  // only whitespace so far
        // Network chunks may split the SSE field name (e.g. "ev" + "ent:").
        // Keep the prefix until the format is known, then consume it once.
        if (['event:', 'data:'].some(prefix => prefix.startsWith(trimmed))) return;
        if (trimmed.startsWith('event:') || trimmed.startsWith('data:')) {
          mode = 'sse';
          feedEvents(trimmed);
        } else {
          mode = 'json';
          body = head;
        }
        return;
      }
      if (mode === 'sse') { feedEvents(text); return; }
      if (body.length <= maxUsageBytes) body += text;
    },
    end() {
      if (mode === 'sse') { take(usageOfEvents(pending)); pending = ''; return; }
      if (mode !== 'json') return;
      let payload;
      try { payload = JSON.parse(body); } catch { return; }
      take(usageOfPayload(payload));
    },
    result() { return {in: incoming, out: outgoing}; },
  };
};
// Per-request audit log (JSON lines, no credentials and no prompt text): time, lane, status,
// latency and the token counters the provider reported. It is the collection side of the
// MiniMax burn reading (Refs #1671): the fleet side measures the real hourly token
// consumption of this log instead of a dispatch count proxy. The log lives next to the
// socket ("<socket>.requests.jsonl") unless PI_BROKER_LOG names another file.
const logPath = process.env.PI_BROKER_LOG || (socket + '.requests.jsonl');
// The byte limit includes the next complete UTF-8 JSONL record. Zero disables rotation,
// not logging; invalid, negative and empty settings use the 20 MiB default.
const configuredLogLimit = process.env.PI_BROKER_LOG_MAX_BYTES;
const parsedLogLimit = configuredLogLimit?.trim() ? Number(configuredLogLimit) : NaN;
const maxLogBytes = Number.isSafeInteger(parsedLogLimit) && parsedLogLimit >= 0 ? parsedLogLimit : 20 * 1024 * 1024;
// Several generations are kept: a broker that rotates more than once inside an hour must
// not drop the earlier half of that hour (Refs #1671).
const configuredRotations = process.env.PI_BROKER_LOG_ROTATIONS;
const parsedRotations = configuredRotations?.trim() ? Number(configuredRotations) : NaN;
const logRotations = Number.isSafeInteger(parsedRotations) && parsedRotations > 0 ? parsedRotations : 5;
let seq = 0;
const heartbeatPath = `${logPath}.heartbeat`;
// Keep audit failures across restarts until all affected reporting windows expire.
let auditErrorAt = null;
let coverageStart = new Date().toISOString(), logIdentity = null;
try {
  const prior = JSON.parse(readFileSync(heartbeatPath, 'utf8'));
  auditErrorAt = prior.audit_error_at || null;
  const info = statSync(logPath);
  if (info.dev === prior.log_dev && info.ino === prior.log_ino && info.size >= prior.log_size
      && Number.isFinite(Date.parse(prior.coverage_start))) coverageStart = prior.coverage_start;
} catch {}
// A live process alone cannot prove a full hour. Track which log the coverage
// belongs to, so a deleted/replaced/truncated log starts a new coverage period.
const checkLog = () => {
  let info = null;
  try { info = statSync(logPath); } catch {}
  if (!info || (logIdentity && (info.dev !== logIdentity.dev || info.ino !== logIdentity.ino
                               || info.size < logIdentity.size))) coverageStart = new Date().toISOString();
  logIdentity = info;
};

const auditWarning = (operation, error) => {
  auditErrorAt = new Date().toISOString();
  heartbeat();
  // Diagnostics carry neither the row nor the credential.
  console.error(`broker audit log ${operation} failed (${error.code || 'unknown'})`);
};
const rotateLog = () => {
  // A byte/count limit must never evict records from the six-hour reporting
  // window. Keep additional generations during bursts, then prune old files.
  const generations = readdirSync(dirname(logPath))
    .filter(name => name.startsWith(basename(logPath) + '.') && /^\d+$/.test(name.slice(basename(logPath).length + 1)))
    .map(name => Number(name.slice(basename(logPath).length + 1)))
    .sort((a, b) => b - a);
  for (const index of generations) {
    const from = `${logPath}.${index}`;
    if (index >= logRotations && Date.now() - statSync(from).mtimeMs > 6 * 3600000) {
      unlinkSync(from);
    } else {
      renameSync(from, `${logPath}.${index + 1}`);
    }
  }
  renameSync(logPath, `${logPath}.1`);
};
const audit = (row) => {
  let line;
  try { line = JSON.stringify(row) + '\n'; }
  catch (error) { auditWarning('serialization', error); return; }
  checkLog();
  let rotated = false;
  // Synchronous stat/rename/append keep the records serialized within this broker.
  if (maxLogBytes > 0) {
    let size = 0;
    try { size = statSync(logPath).size; }
    catch (error) { if (error.code !== 'ENOENT') auditWarning('stat', error); }
    if (size > 0 && size + Buffer.byteLength(line, 'utf8') > maxLogBytes) {
      try { rotateLog(); rotated = true; } catch (error) { auditWarning('rotation', error); }
    }
  }
  // A rotation failure must not drop this record: still try the current log path.
  try {
    appendFileSync(logPath, line, 'utf8');
    // Our own rotations preserve coverage; publish the new identity immediately.
    logIdentity = statSync(logPath);
    if (rotated) heartbeat();
  }
  catch (error) { auditWarning('append', error); }
};
// The lane is taken from the worktree path in the prompt (".../lanes/<L>/wt").
const laneOf = (text) => { const m = /\/lanes\/([A-Za-z0-9_-]{1,16})\/wt\b/.exec(text); return m ? m[1] : '?'; };
// The liveness file of the collector: the broker refreshes it every 30s, so the burn
// watchdog can tell a fleet that burned nothing in the hour (the collector is alive,
// the hour is quiet) from a collector that stopped (Refs #1671). It holds no data of
// the requests, only liveness and the beginning of retained audit coverage.
const heartbeat = () => {
  checkLog();
  try { writeFileSync(heartbeatPath, JSON.stringify({ts: new Date().toISOString(), audit_error_at: auditErrorAt,
    coverage_start: coverageStart, log_dev: logIdentity?.dev, log_ino: logIdentity?.ino,
    log_size: logIdentity?.size}) + '\n', 'utf8'); }
  catch (error) { console.error(`broker audit heartbeat failed (${error.code || 'unknown'})`); }
};
// A quiet collector has an empty log. A missing log means the audit data is lost,
// even if the heartbeat is fresh; create it once without truncating prior records.
try { appendFileSync(logPath, '', 'utf8'); }
catch (error) { auditWarning('initialization', error); }
checkLog();
const heartbeatTimer = setInterval(heartbeat, 30000);
heartbeatTimer.unref();
let busy = false, accepted = 0;
const server = createServer((req, res) => {
  const t0 = Date.now();
  const row = {ts: new Date(t0).toISOString(), id: ++seq, lane: '?', status: 0, err: '', ms: 0,
               resp_bytes: 0, in_tok: null, out_tok: null};
  let logged = false;
  const done = (extra) => { if (logged) return; logged = true; Object.assign(row, extra || {}); row.ms = Date.now() - t0; audit(row); };
  const fail = (status, message, forwarded = false) => {
    row.status = status; row.err = message;
    // A request that never reached the provider burned nothing: its counters are
    // explicit zeros, so the hour of the fleet is a real zero and not a broken
    // collection. A forwarded request that failed may still have been billed, so
    // its usage stays unknown (Refs #1671).
    if (!forwarded) { row.in_tok = 0; row.out_tok = 0; }
    if (!res.headersSent) res.writeHead(status, {'content-type': 'text/plain'});
    res.end(message); done();
  };
  if (busy || accepted >= 64) return fail(503, 'broker temporarily unavailable');
  if (req.method !== 'POST' || !['/v1/messages', '/v1/messages?beta=true'].includes(req.url)) return fail(403, 'request refused');
  const beta = req.headers['anthropic-beta'];
  if (beta !== undefined && (typeof beta !== 'string' || !/^[a-z0-9, -]{1,512}$/i.test(beta))) return fail(400, 'invalid beta header');
  const length = Number(req.headers['content-length']);
  if (!Number.isSafeInteger(length) || length < 1 || length > maxBytes || req.headers['transfer-encoding']) return fail(400, 'invalid size');
  busy = true;
  req.setTimeout(20000, () => req.destroy());
  let settled = false;
  const release = () => { if (!settled) { settled = true; busy = false; } };
  let size = 0; const parts = [];
  req.on('data', chunk => { size += chunk.length; if (size <= maxBytes) parts.push(chunk); else req.destroy(); });
  req.on('error', release);
  req.on('aborted', release);
  req.on('end', () => {
    req.setTimeout(0);
    const bodyText = Buffer.concat(parts).toString('utf8');
    row.lane = laneOf(bodyText);
    let parsed;
    try { parsed = JSON.parse(bodyText); } catch { release(); return fail(400, 'invalid JSON'); }
    if (size !== length || !parsed || parsed.model !== model || !Array.isArray(parsed.messages)) {
      release(); return fail(403, 'model or request refused');
    }
    accepted += 1;
    if (accepted >= 64) {
      // A disconnected client may never emit finish. Stop on either terminal
      // event so the supervised broker cannot remain permanently at its cap.
      let stopping = false;
      const stopAtLimit = () => {
        if (stopping) return;
        stopping = true;
        server.close(() => { try { unlinkSync(socket); } catch {} process.exit(0); });
        server.closeAllConnections?.();
      };
      res.once('finish', stopAtLimit);
      res.once('close', stopAtLimit);
    }
    // Forward the parsed value, not the original bytes: duplicate JSON keys must not
    // let the upstream select a different model from the one checked above.
    const payload = Buffer.from(JSON.stringify(parsed));
    const outgoing = https.request({hostname: 'api.minimaxi.com', port: 443, path: '/anthropic' + req.url,
      method: 'POST', timeout: 180000, headers: {'content-type': 'application/json', 'content-length': payload.length,
        'anthropic-version': '2023-06-01', ...(beta === undefined ? {} : {'anthropic-beta': beta}),
        'x-api-key': key, 'authorization': 'Bearer ' + key}}, upstream => {
      if (upstream.statusCode < 200 || upstream.statusCode >= 300) {
        upstream.resume(); release(); return fail(upstream.statusCode || 502, 'provider request failed', true);
      }
      res.writeHead(upstream.statusCode, {'content-type': upstream.headers['content-type'] || 'application/json',
                                         'cache-control': 'no-store'});
      // The usage counters of the provider answer are the real burn of the request.
      // The answer is a JSON body or an SSE stream; both carry the usage fields,
      // and only inside their usage object.
      const usage = usageCollector();
      upstream.on('data', chunk => { row.resp_bytes += chunk.length; usage.push(chunk); });
      upstream.on('error', () => { release(); res.destroy(); });
      upstream.on('end', () => {
        usage.end();
        const counters = usage.result();
        row.in_tok = counters.in;
        row.out_tok = counters.out;
        row.status = upstream.statusCode;
        done();
        release();
      });
      upstream.on('close', () => { release(); if (!upstream.complete) res.destroy(); });
      upstream.pipe(res);
    });
    outgoing.on('timeout', () => outgoing.destroy());
    outgoing.on('error', () => { release(); fail(502, 'provider unavailable', true); });
    res.on('close', () => { done({client_closed: !res.writableEnded}); if (!res.writableEnded) outgoing.destroy(); release(); });
    outgoing.end(payload);
  });
});
server.listen(socket, () => { chmodSync(socket, 0o600); heartbeat(); });
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => server.close(() => { try { unlinkSync(socket); } catch {} process.exit(0); }));
