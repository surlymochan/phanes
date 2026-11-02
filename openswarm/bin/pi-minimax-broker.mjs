#!/usr/bin/env node
// A private, single-model Anthropic proxy. The Pi credential stays in this process.
import { createServer } from 'node:http';
import https from 'node:https';
import { appendFileSync, chmodSync, lstatSync, renameSync, statSync, unlinkSync } from 'node:fs';
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
const maxErrorBytes = 4096;
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
// Byte limit includes the next complete UTF-8 JSONL record. Zero disables rotation,
// not logging; invalid, negative and empty settings use the 20 MiB default.
const configuredLogLimit = process.env.PI_BROKER_LOG_MAX_BYTES;
const parsedLogLimit = configuredLogLimit?.trim() ? Number(configuredLogLimit) : NaN;
const maxLogBytes = Number.isSafeInteger(parsedLogLimit) && parsedLogLimit >= 0
  ? parsedLogLimit : 20 * 1024 * 1024;
let seq = 0;
const auditWarning = (operation, error) => {
  // Do not include the row, request, headers, or credential in diagnostics.
  console.error(`broker audit log ${operation} failed (${error.code || 'unknown'})`);
};
const audit = (row) => {
  let line;
  try { line = JSON.stringify(row) + '\n'; }
  catch (error) { auditWarning('serialization', error); return; }
  // Synchronous stat/rename/append keep records serialized within this broker.
  if (maxLogBytes > 0) {
    let size = 0;
    try { size = statSync(logPath).size; }
    catch (error) { if (error.code !== 'ENOENT') auditWarning('stat', error); }
    if (size > 0 && size + Buffer.byteLength(line, 'utf8') > maxLogBytes) {
      try { renameSync(logPath, logPath + '.1'); }
      catch (error) { auditWarning('rotation', error); }
    }
  }
  // Rotation failure must not drop this record: still try the current log path.
  try { appendFileSync(logPath, line, 'utf8'); }
  catch (error) { auditWarning('append', error); }
};
const laneOf = (text) => { const m = /\/lanes\/([A-Za-z0-9_-]{1,16})\/wt\b/.exec(text); return m ? m[1] : '?'; };
// Optional request log. A lane turn reads the last entry of this file to learn whether the
// provider is still being asked for work; without it only the child event stream counts.
const requestLog = process.env.OPENSWARM_BROKER_REQUEST_LOG;
const noteRequest = () => {
  if (!requestLog) return;
  try { appendFileSync(requestLog, JSON.stringify({ts: Date.now() / 1000}) + '\n'); } catch { /* the log is advisory */ }
};

const defaultQuotaPattern = 'Token Plan|用量上限|usage limit|\\(2056\\)';
const getQuotaRe = () => {
  const env = process.env.PI_BROKER_QUOTA_RE;
  if (!env) return new RegExp(defaultQuotaPattern);
  try {
    return new RegExp(env);
  } catch {
    return new RegExp(defaultQuotaPattern);
  }
};
const getQuotaCooldownMs = () => {
  const s = Number(process.env.PI_BROKER_QUOTA_COOLDOWN_S);
  return (Number.isFinite(s) && s > 0 ? s : 300) * 1000;
};
let breakerState = 'closed';
let breakerOpenUntil = 0;
let lastQuotaBody = null;
let lastQuotaContentType = 'application/json';
let lastQuotaMsg = '';
let lastQuotaType = 'rate_limit_error';
let lastQuotaCode = null;
let probeInFlight = false;

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
        row.upstream_type = lastQuotaType;
        if (lastQuotaCode !== null) row.provider_code = lastQuotaCode;
        row.err = lastQuotaMsg || 'Token Plan quota exhausted';
        row.resp_bytes = Buffer.byteLength(lastQuotaBody || '');
        if (!res.headersSent) res.writeHead(429, {'content-type': lastQuotaContentType || 'application/json', 'cache-control': 'no-store'});
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
        row.upstream_type = lastQuotaType;
        if (lastQuotaCode !== null) row.provider_code = lastQuotaCode;
        row.err = lastQuotaMsg || 'Token Plan quota exhausted';
        row.resp_bytes = Buffer.byteLength(lastQuotaBody || '');
        if (!res.headersSent) res.writeHead(429, {'content-type': lastQuotaContentType || 'application/json', 'cache-control': 'no-store'});
        res.end(lastQuotaBody || JSON.stringify({type: 'rate_limit_error', message: row.err}));
        done();
        return;
      }
    } else if (breakerState === 'half-open') {
      release();
      row.status = 429;
      row.breaker = 'open';
      row.upstream_type = lastQuotaType;
      if (lastQuotaCode !== null) row.provider_code = lastQuotaCode;
      row.err = lastQuotaMsg || 'Token Plan quota exhausted';
      row.resp_bytes = Buffer.byteLength(lastQuotaBody || '');
      if (!res.headersSent) res.writeHead(429, {'content-type': lastQuotaContentType || 'application/json', 'cache-control': 'no-store'});
      res.end(lastQuotaBody || JSON.stringify({type: 'rate_limit_error', message: row.err}));
      done();
      return;
    }
    noteRequest();
    // Forward the parsed value, not the original bytes: duplicate JSON keys must not
    // let the upstream select a different model from the one checked above.
    const payload = Buffer.from(JSON.stringify(parsed));
    let providerErrorResponded = false;
    let upstreamResponded = false;
    const outgoing = https.request({hostname: 'api.minimaxi.com', port: 443, path: '/anthropic' + req.url,
      method: 'POST', timeout: 180000, headers: {'content-type': 'application/json', 'content-length': payload.length,
        'anthropic-version': '2023-06-01', ...(beta === undefined ? {} : {'anthropic-beta': beta}),
        'x-api-key': key, 'authorization': 'Bearer ' + key}}, upstream => {
      upstreamResponded = true;
      if (upstream.statusCode < 200 || upstream.statusCode >= 300) {
        row.status = upstream.statusCode || 502; row.ttfb_ms = Date.now() - t0;
        const retry = {};
        for (const [name, value] of Object.entries(upstream.headers || {})) {
          const lower = name.toLowerCase();
          if (lower === 'retry-after' || lower.startsWith('x-ratelimit-reset')) retry[lower] = value;
        }
        if (Object.keys(retry).length) row.retry_after = retry;
        // Copy only the bounded prefix, so a large chunk cannot stay retained through
        // a Buffer slice. A provider that never ends is stopped as soon as the cap fills.
        const errorBody = Buffer.alloc(maxErrorBytes);
        let errorBytes = 0;
        const finishError = () => {
          if (providerErrorResponded) return;
          providerErrorResponded = true;
          const body = errorBody.subarray(0, errorBytes);
          row.resp_bytes = errorBytes;
          let rawMsg = '';
          try {
            const parsedError = JSON.parse(body.toString('utf8'));
            const error = parsedError?.error && typeof parsedError.error === 'object'
              ? parsedError.error : parsedError;
            if (typeof error?.type === 'string') row.upstream_type = error.type;
            if (typeof error?.message === 'string') rawMsg = error.message;
            row.err = rawMsg ? Array.from(rawMsg).slice(0, 160).join('') : 'provider request failed';
            const code = parsedError?.base_resp?.status_code ?? error?.base_resp?.status_code;
            if (typeof code === 'string' || (typeof code === 'number' && Number.isFinite(code))) row.provider_code = code;
          } catch { row.err = 'non_json_body'; }
          const quotaRe = getQuotaRe();
          const bodyText = body.toString('utf8');
          const isQuota = row.status === 429 &&
            (row.upstream_type === 'rate_limit_error' || !row.upstream_type) &&
            (quotaRe.test(rawMsg) || quotaRe.test(bodyText) || quotaRe.test(row.err));
          if (isQuota) {
            breakerState = 'open';
            breakerOpenUntil = Date.now() + getQuotaCooldownMs();
            lastQuotaBody = body.length > 0 ? body : Buffer.from(JSON.stringify({type: 'rate_limit_error', message: row.err}));
            lastQuotaContentType = upstream.headers['content-type'] || 'application/json';
            lastQuotaMsg = row.err;
            lastQuotaType = row.upstream_type || 'rate_limit_error';
            lastQuotaCode = row.provider_code ?? null;
            probeInFlight = false;
            row.breaker = isProbe ? 'half-open' : 'open';
          } else if (isProbe) {
            breakerState = 'open';
            breakerOpenUntil = Date.now() + getQuotaCooldownMs();
            probeInFlight = false;
            row.breaker = 'half-open';
          }
          release();
          if (!res.destroyed && !res.writableEnded) {
            res.writeHead(row.status, {'content-type': upstream.headers['content-type'] || 'application/json', 'cache-control': 'no-store'});
            res.end(body);
          }
          done();
        };
        upstream.on('data', chunk => {
          if (providerErrorResponded) return;
          const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
          const take = Math.min(bytes.length, maxErrorBytes - errorBytes);
          bytes.copy(errorBody, errorBytes, 0, take);
          errorBytes += take;
          if (errorBytes === maxErrorBytes) {
            finishError();
            upstream.destroy();
          }
        });
        upstream.on('end', finishError);
        upstream.on('error', finishError);
        upstream.on('aborted', finishError);
        upstream.on('close', finishError);
        return;
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
              lastQuotaBody = Buffer.from(JSON.stringify({type: 'rate_limit_error', message: row.err}));
              lastQuotaContentType = 'application/json';
              lastQuotaMsg = row.err;
              lastQuotaType = 'rate_limit_error';
              lastQuotaCode = null;
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
      release(); if (!providerErrorResponded) fail(502, 'provider unavailable');
    });
    res.on('close', () => {
      if (isProbe && !upstreamResponded) {
        breakerState = 'open'; breakerOpenUntil = Date.now() + getQuotaCooldownMs(); probeInFlight = false;
      }
      if (!res.writableEnded) outgoing.destroy();
      release();
    });
    outgoing.end(payload);
  });
});
server.listen(socket, () => chmodSync(socket, 0o600));
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => server.close(() => { try { unlinkSync(socket); } catch {} process.exit(0); }));
