#!/usr/bin/env node
// A private, single-model Anthropic proxy. The Pi credential stays in this process.
import { createServer } from 'node:http';
import https from 'node:https';
import { chmodSync, lstatSync, unlinkSync } from 'node:fs';
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
// Four resident Pi lanes share this broker. Track every in-flight request so
// one completion cannot admit a fifth while peers are still streaming. The
// fleet coordinator remains responsible for the cross-host/global limit.
const maxInFlight = 4;
let inFlight = 0;

const server = createServer((req, res) => {
  const fail = (status, message) => { if (!res.headersSent) res.writeHead(status, {'content-type': 'text/plain'}); res.end(message); };
  if (inFlight >= maxInFlight) return fail(503, 'broker temporarily unavailable');
  if (req.method !== 'POST' || !['/v1/messages', '/v1/messages?beta=true'].includes(req.url)) return fail(403, 'request refused');
  const beta = req.headers['anthropic-beta'];
  if (beta !== undefined && (typeof beta !== 'string' || !/^[a-z0-9, -]{1,512}$/i.test(beta))) return fail(400, 'invalid beta header');
  const length = Number(req.headers['content-length']);
  if (!Number.isSafeInteger(length) || length < 1 || length > maxBytes || req.headers['transfer-encoding']) return fail(400, 'invalid size');
  inFlight += 1;
  req.setTimeout(20000, () => req.destroy());
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
    try { parsed = JSON.parse(Buffer.concat(parts).toString('utf8')); } catch { release(); return fail(400, 'invalid JSON'); }
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
      res.writeHead(upstream.statusCode, {'content-type': upstream.headers['content-type'] || 'application/json',
                                         'cache-control': 'no-store'});
      upstream.on('error', () => { release(); res.destroy(); });
      upstream.on('end', release);
      upstream.on('close', () => { release(); if (!upstream.complete) res.destroy(); });
      upstream.pipe(res);
    });
    outgoing.on('timeout', () => outgoing.destroy());
    outgoing.on('error', () => { release(); fail(502, 'provider unavailable'); });
    res.on('close', () => { if (!res.writableEnded) outgoing.destroy(); release(); });
    outgoing.end(payload);
  });
});
server.listen(socket, () => chmodSync(socket, 0o600));
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => server.close(() => { try { unlinkSync(socket); } catch {} process.exit(0); }));
