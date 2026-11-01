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
// Optional request log. A lane turn reads the last entry of this file to learn whether the
// provider is still being asked for work; without it only the child event stream counts.
const requestLog = process.env.OPENSWARM_BROKER_REQUEST_LOG;
const noteRequest = () => {
  if (!requestLog) return;
  try { appendFileSync(requestLog, JSON.stringify({ts: Date.now() / 1000}) + '\n'); } catch { /* the log is advisory */ }
};

const defaultQuotaPattern = 'Token Plan|用量上限|usage limit|\\(2056\\)';
const getQuotaRe = () => new RegExp(process.env.PI_BROKER_QUOTA_RE || defaultQuotaPattern);
const getQuotaCooldownMs = () => {
  const s = Number(process.env.PI_BROKER_QUOTA_COOLDOWN_S);
  return (Number.isFinite(s) && s > 0 ? s : 300) * 1000;
};
let breakerState = 'closed';
let breakerOpenUntil = 0;
let lastQuotaBody = null;
let lastQuotaContentType = 'application/json';
let lastQuotaMsg = '';
let probeInFlight = false;
const getErrorInfo = (bodyStr) => {
  let parsed;
  try { parsed = JSON.parse(bodyStr); } catch {}
  if (!parsed || typeof parsed !== 'object') return { type: null, message: '' };
  const type = (parsed.error && typeof parsed.error === 'object' && parsed.error.type) || parsed.type || null;
  const message = (parsed.error && typeof parsed.error === 'object' && typeof parsed.error.message === 'string')
    ? parsed.error.message
    : (typeof parsed.message === 'string' ? parsed.message : '');
  return { type, message };
};

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
    const now = Date.now();
    let isProbe = false;
    if (breakerState === 'open') {
      if (now < breakerOpenUntil) {
        release();
        row.status = 429;
        row.breaker = 'open';
        row.err = lastQuotaMsg || 'Token Plan quota exhausted';
        row.resp_bytes = Buffer.byteLength(lastQuotaBody || '');
        if (!res.headersSent) res.writeHead(429, {'content-type': lastQuotaContentType || 'application/json'});
        res.end(lastQuotaBody || JSON.stringify({type: 'rate_limit_error', message: row.err}));
        done();
        return;
      }
      if (!probeInFlight) {
        probeInFlight = true;
        breakerState = 'half-open';
        isProbe = true;
        row.breaker = 'half-open';
      } else {
        release();
        row.status = 429;
        row.breaker = 'open';
        row.err = lastQuotaMsg || 'Token Plan quota exhausted';
        row.resp_bytes = Buffer.byteLength(lastQuotaBody || '');
        if (!res.headersSent) res.writeHead(429, {'content-type': lastQuotaContentType || 'application/json'});
        res.end(lastQuotaBody || JSON.stringify({type: 'rate_limit_error', message: row.err}));
        done();
        return;
      }
    } else if (breakerState === 'half-open') {
      release();
      row.status = 429;
      row.breaker = 'open';
      row.err = lastQuotaMsg || 'Token Plan quota exhausted';
      row.resp_bytes = Buffer.byteLength(lastQuotaBody || '');
      if (!res.headersSent) res.writeHead(429, {'content-type': lastQuotaContentType || 'application/json'});
      res.end(lastQuotaBody || JSON.stringify({type: 'rate_limit_error', message: row.err}));
      done();
      return;
    }
    noteRequest();
    // Forward the parsed value, not the original bytes: duplicate JSON keys must not
    // let the upstream select a different model from the one checked above.
    const payload = Buffer.from(JSON.stringify(parsed));
    const outgoing = https.request({hostname: 'api.minimaxi.com', port: 443, path: '/anthropic' + req.url,
      method: 'POST', timeout: 180000, headers: {'content-type': 'application/json', 'content-length': payload.length,
        'anthropic-version': '2023-06-01', ...(beta === undefined ? {} : {'anthropic-beta': beta}),
        'x-api-key': key, 'authorization': 'Bearer ' + key}}, upstream => {
      if (upstream.statusCode < 200 || upstream.statusCode >= 300) {
        if (upstream.statusCode === 429) {
          const errParts = [];
          upstream.on('data', chunk => errParts.push(chunk));
          upstream.on('error', () => { release(); res.destroy(); });
          upstream.on('end', () => {
            release();
            const errBody = Buffer.concat(errParts).toString('utf8');
            const ctype = upstream.headers['content-type'] || 'application/json';
            const { type: errType, message: errMsg } = getErrorInfo(errBody);
            const quotaRe = getQuotaRe();
            const isQuota = errType === 'rate_limit_error' && (quotaRe.test(errMsg) || quotaRe.test(errBody));
            if (isQuota) {
              breakerState = 'open';
              breakerOpenUntil = Date.now() + getQuotaCooldownMs();
              lastQuotaBody = errBody;
              lastQuotaContentType = ctype;
              lastQuotaMsg = errMsg;
              probeInFlight = false;
              row.breaker = isProbe ? 'half-open' : 'open';
            } else if (breakerState === 'half-open') {
              breakerState = 'closed';
              probeInFlight = false;
              lastQuotaBody = null;
            }
            row.status = 429;
            row.err = errMsg || 'rate_limit_error';
            row.resp_bytes = Buffer.byteLength(errBody);
            if (!res.headersSent) res.writeHead(429, {'content-type': ctype});
            res.end(errBody);
            done();
          });
          return;
        }
        if (breakerState === 'half-open') {
          breakerState = 'open';
          breakerOpenUntil = Date.now() + getQuotaCooldownMs();
          probeInFlight = false;
        }
        upstream.resume(); release(); return fail(upstream.statusCode || 502, 'provider request failed');
      }
      if (breakerState === 'half-open') {
        breakerState = 'closed';
        probeInFlight = false;
        lastQuotaBody = null;
      }
      row.status = upstream.statusCode; row.ttfb_ms = Date.now() - t0;
      let tail = '';
      upstream.on('data', chunk => {
        row.resp_bytes += chunk.length;
        tail = (tail + chunk.toString('utf8')).slice(-4096);
        let m; const re = /"(input_tokens|output_tokens)"\s*:\s*(\d+)/g;
        while ((m = re.exec(tail))) { const k = m[1] === 'input_tokens' ? 'in_tok' : 'out_tok'; row[k] = Math.max(row[k] || 0, Number(m[2])); }
        // Only a real SSE error event counts (line-start `event: error` or an error `data:` frame);
        // model text is JSON-escaped inside deltas, so it cannot forge either form.
        if (!row.stream_err && /(?:^|\n)event:\s*error\b|(?:^|\n)data:\s*\{"type"\s*:\s*"error"|(?:^|\n)data:\s*\{[^\n]*"http_code"\s*:\s*"?[45]\d\d/.test(tail)) {
          const t = /"error"\s*:\s*\{[^}]*?"type"\s*:\s*"([a-z_]+)"/.exec(tail); const h = /"http_code"\s*:\s*"?([45]\d\d)/.exec(tail);
          const map = {rate_limit_error: 429, overloaded_error: 529, api_error: 500, invalid_request_error: 400, authentication_error: 401, permission_error: 403, not_found_error: 404, request_too_large: 413};
          row.http_status = row.status; row.status = h ? Number(h[1]) : (map[t && t[1]] || 500);
          row.stream_err = (t && t[1]) || 'sse_error';
          const mm = /"message"\s*:\s*"([^"]{0,160})/.exec(tail); if (mm) row.err = mm[1];
          if (row.status === 429 && row.stream_err === 'rate_limit_error') {
            const quotaRe = getQuotaRe();
            if (row.err && quotaRe.test(row.err)) {
              breakerState = 'open';
              breakerOpenUntil = Date.now() + getQuotaCooldownMs();
              lastQuotaBody = JSON.stringify({type: 'rate_limit_error', message: row.err});
              lastQuotaContentType = 'application/json';
              lastQuotaMsg = row.err;
              probeInFlight = false;
              row.breaker = isProbe ? 'half-open' : 'open';
            }
          }
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
    outgoing.on('timeout', () => {
      if (isProbe) { breakerState = 'open'; breakerOpenUntil = Date.now() + getQuotaCooldownMs(); probeInFlight = false; }
      row.err = 'upstream timeout'; outgoing.destroy();
    });
    outgoing.on('error', () => {
      if (isProbe) { breakerState = 'open'; breakerOpenUntil = Date.now() + getQuotaCooldownMs(); probeInFlight = false; }
      release(); fail(502, 'provider unavailable');
    });
    res.on('close', () => {
      if (isProbe && !res.writableEnded) { breakerState = 'open'; breakerOpenUntil = Date.now() + getQuotaCooldownMs(); probeInFlight = false; }
      if (!res.writableEnded) outgoing.destroy();
      release();
    });
    outgoing.end(payload);
  });
});
server.listen(socket, () => chmodSync(socket, 0o600));
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => server.close(() => { try { unlinkSync(socket); } catch {} process.exit(0); }));
