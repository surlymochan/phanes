import { test } from 'node:test';
import assert from 'node:assert/strict';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { gzipSync, gunzipSync } from 'node:zlib';

function withScanner(name, run) {
  const root = mkdtempSync(join(tmpdir(), 'evalid-privacy-'));
  try {
    mkdirSync(join(root, 'scripts'));
    const script = join(root, 'scripts', name);
    copyFileSync(resolve('scripts', name), script);
    run(root, (...args) => spawnSync('python3', [script, ...args], { encoding: 'utf8' }));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test('home path scanner checks and redacts gzip payloads without changing clean archives', () => {
  withScanner('redact-home-paths.py', (root, scan) => {
    const privatePaths = ['Users', 'home'].map((directory) => `/${directory}/tester/work/file`);
    const payload = `${privatePaths.join('\n')}\n`;
    const archive = join(root, 'solution.diff.gz');
    const compressed = gzipSync(payload);
    writeFileSync(archive, compressed);
    writeFileSync(join(root, 'plain.txt'), payload);
    const dirty = scan('--check');
    assert.equal(dirty.status, 1, dirty.stderr);
    assert.match(dirty.stdout, /Found 4 home paths in 2 files/);
    assert.deepEqual(readFileSync(archive), compressed);
    assert.equal(scan().status, 0);
    assert.equal(gunzipSync(readFileSync(archive)).toString(), '~/work/file\n~/work/file\n');
    const clean = readFileSync(archive);
    assert.equal(scan('--check').status, 0);
    assert.equal(scan().status, 0);
    assert.deepEqual(readFileSync(archive), clean);
  });
});

test('IP scanner preserves allowed loopbacks and detects non-loopback IPs inside gzip', () => {
  withScanner('redact-ip-addresses.py', (root, scan) => {
    const allowed = '127.0.0.1 localhost ::1 192.0.2.10 2001:db8::10';
    const archive = join(root, 'endpoints.txt.gz');
    const publicIPv4 = [8, 8, 8, 8].join('.');
    const publicIPv6 = ['2606', '4700', '4700', '', '1111'].join(':');
    writeFileSync(archive, gzipSync(`${allowed}\n${publicIPv4} ${publicIPv6}`));
    const dirty = scan('--check');
    assert.equal(dirty.status, 1, dirty.stderr);
    assert.match(dirty.stdout, /Found 2 IP literals in 1 files/);
    assert.equal(scan().status, 0);
    assert.ok(gunzipSync(readFileSync(archive)).toString().startsWith(`${allowed}\n`));
    assert.equal(scan('--check').status, 0);
  });
});
