#!/usr/bin/env node
// A private, single-model Anthropic proxy. The Pi credential stays in this process.
import { createServer } from 'node:http';
import https from 'node:https';
import { appendFileSync, chmodSync, lstatSync, unlinkSync } from 'node:fs';
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
// The upload timeout bounds a slow request body only. It is cleared once the body
// is complete, so a slow inference may take longer than this value. The default
// is the production value; the override exists so the test suite can exercise the
// same path without waiting for a fixed multi-second delay.
const uploadTimeout = Number(process.env.OPENSWARM_BROKER_UPLOAD_TIMEOUT_MS);
const uploadTimeoutMs = Number.isFinite(uploadTimeout) && uploadTimeout > 0 ? uploadTimeout : 20000;
// Four resident Pi lanes share this broker. Track every in-flight request so
// one completion cannot admit a fifth while peers are still streaming. The
// fleet coordinator remains responsible for the cross-host/global limit.
const maxInFlight = 4;
let inFlight = 0;
// Per-request audit log (JSON lines, no credentials or prompt text): time, lane, sizes, tokens,
// status and latency. Lane is taken from the worktree path in the prompt (".../lanes/<L>/wt").
const logPath = process.env.PI_BROKER_LOG || (socket + '.requests.jsonl');
let seq = 0;
const audit = (row) => { try { appendFileSync(logPath, JSON.stringify(row) + '\n'); } catch {} };
const laneOf = (text) => { const m = /\/lanes\/([A-Za-z0-9_-]{1,16})\/wt\b/.exec(text); return m ? m[1] : '?'; };

const server = createServer((req, res) => {
  const t0 = Date.now(); const id = ++seq;
  const row = {ts: new Date(t0).toISOString(), id, lane: '?', req_bytes: 0, resp_bytes: 0, status: 0,
               in_tok: null, out_tok: null, in_flight: inFlight + 1};
  let logged = false;
  const done = (extra) => { if (logged) return; logged = true; Object.assign(row, extra || {}); row.ms = Date.now() - t0; audit(row); };
  res.on('close', () => done({client_closed: !res.writableEnded}));
  const fail = (status, message) => { row.status = status; row.err = message; if (!res.headersSent) res.writeHead(status, {'content-type': 'text/plain'}); res.end(message); done(); };
  if (inFlight >= maxInFlight) return fail(503, 'broker temporarily unavailable');
  if (req.method !== 'POST' || !['/v1/messages', '/v1/messages?beta=true'].includes(req.url)) return fail(403, 'request refused');
  const beta = req.headers['anthropic-beta'];
  if (beta !== undefined && (typeof beta !== 'string' || !/^[a-z0-9, -]{1,512}$/i.test(beta))) return fail(400, 'invalid beta header');
  const length = Number(req.headers['content-length']);
  if (!Number.isSafeInteger(length) || length < 1 || length > maxBytes || req.headers['transfer-encoding']) return fail(400, 'invalid size');
  inFlight += 1;
  req.setTimeout(uploadTimeoutMs, () => req.destroy());
  let settled = false;
  const release = () => {
    if (settled) return;
    settled = true;
    inFlight -= 1;
  };
  let size = 0; const parts = [];
  req.on('data', chunk => { size += chunk.length; if (size <= maxBytes) parts.push(chunk); else req.destroy(); });
  req.on('error', release);
  req.on('aborted', release);
  req.on('end', () => {
    req.setTimeout(0);
    let parsed;
    const bodyText = Buffer.concat(parts).toString('utf8');
    row.req_bytes = size; row.lane = laneOf(bodyText);
    try { parsed = JSON.parse(bodyText); } catch { release(); return fail(400, 'invalid JSON'); }
    if (size !== length || !parsed || parsed.model !== model || !Array.isArray(parsed.messages)) {
      release(); return fail(403, 'model or request refused');
    }
    // Forward the parsed value, not the original bytes: duplicate JSON keys must not
    // let the upstream select a different model from the one checked above.
    const payload = Buffer.from(JSON.stringify(parsed));
    const outgoing = https.request({hostname: 'api.minimaxi.com', port: 443, path: '/anthropic' + req.url,
      method: 'POST', timeout: 180000, headers: {'content-type': 'application/json', 'content-length': payload.length,
        'anthropic-version': '2023-06-01', ...(beta === undefined ? {} : {'anthropic-beta': beta}),
        'x-api-key': key, 'authorization': 'Bearer ' + key}}, upstream => {
      if (upstream.statusCode < 200 || upstream.statusCode >= 300) {
        upstream.resume(); release(); return fail(upstream.statusCode || 502, 'provider request failed');
      }
      row.status = upstream.statusCode; row.ttfb_ms = Date.now() - t0;
      let tail = '';
      upstream.on('data', chunk => {
        row.resp_bytes += chunk.length;
        tail = (tail + chunk.toString('utf8')).slice(-4096);
        let m; const re = /"(input_tokens|output_tokens)"\s*:\s*(\d+)/g;
        while ((m = re.exec(tail))) { const k = m[1] === 'input_tokens' ? 'in_tok' : 'out_tok'; row[k] = Math.max(row[k] || 0, Number(m[2])); }
        if (!row.stream_err && /"type"\s*:\s*"error"|event:\s*error|"http_code"\s*:\s*"?[45]\d\d/.test(tail)) {
          const t = /"error"\s*:\s*\{[^}]*?"type"\s*:\s*"([a-z_]+)"/.exec(tail); const h = /"http_code"\s*:\s*"?([45]\d\d)/.exec(tail);
          const map = {rate_limit_error: 429, overloaded_error: 529, api_error: 500, invalid_request_error: 400, authentication_error: 401, permission_error: 403, not_found_error: 404, request_too_large: 413};
          row.http_status = row.status; row.status = h ? Number(h[1]) : (map[t && t[1]] || 500);
          row.stream_err = (t && t[1]) || 'sse_error';
          const mm = /"message"\s*:\s*"([^"]{0,160})/.exec(tail); if (mm) row.err = mm[1];
        }
      });
      upstream.on('end', () => done());
      res.writeHead(upstream.statusCode, {'content-type': upstream.headers['content-type'] || 'application/json',
                                         'cache-control': 'no-store'});
      upstream.on('error', () => { release(); res.destroy(); });
      upstream.on('end', release);
      upstream.on('close', () => { release(); if (!upstream.complete) res.destroy(); });
      upstream.pipe(res);
    });
    outgoing.on('timeout', () => { row.err = 'upstream timeout'; outgoing.destroy(); });
    outgoing.on('error', () => { release(); fail(502, 'provider unavailable'); });
    res.on('close', () => { if (!res.writableEnded) outgoing.destroy(); release(); });
    outgoing.end(payload);
  });
});
server.listen(socket, () => chmodSync(socket, 0o600));
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => server.close(() => { try { unlinkSync(socket); } catch {} process.exit(0); }));
