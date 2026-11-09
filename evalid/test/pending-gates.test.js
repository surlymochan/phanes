import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { afterEach, describe, test } from 'node:test';

import { ackPendingGate } from '../src/core/pending-gates.js';

const projects = [];

function makeProject() {
  const projectRoot = mkdtempSync(resolve(tmpdir(), 'pending-gates-'));
  projects.push(projectRoot);
  return projectRoot;
}

function writeGate(filePath) {
  mkdirSync(dirname(filePath), { recursive: true });
  const contents = `${JSON.stringify({ phase: 'review', status: 'pending' }, null, 2)}\n`;
  writeFileSync(filePath, contents);
  return contents;
}

afterEach(() => {
  for (const projectRoot of projects.splice(0)) {
    rmSync(projectRoot, { recursive: true, force: true });
  }
});

describe('ackPendingGate', () => {
  test('rejects traversal without changing the file outside pending-gates', () => {
    const projectRoot = makeProject();
    const outsideGate = resolve(projectRoot, '.as-xflow', 'outside.json');
    const original = writeGate(outsideGate);

    assert.throws(() => ackPendingGate(projectRoot, '../outside'));
    assert.equal(readFileSync(outsideGate, 'utf8'), original);
  });

  test('rejects absolute paths without changing the target file', () => {
    const projectRoot = makeProject();
    const absoluteGate = resolve(projectRoot, 'absolute.json');
    const original = writeGate(absoluteGate);

    assert.throws(() => ackPendingGate(projectRoot, absoluteGate.slice(0, -5)));
    assert.equal(readFileSync(absoluteGate, 'utf8'), original);
  });

  for (const phase of ['nested/gate', 'nested\\gate']) {
    test(`rejects phase separator input ${JSON.stringify(phase)}`, () => {
      const projectRoot = makeProject();
      const nestedGate = resolve(projectRoot, '.as-xflow', 'pending-gates', 'nested', 'gate.json');
      const original = writeGate(nestedGate);

      assert.throws(() => ackPendingGate(projectRoot, phase));
      assert.equal(readFileSync(nestedGate, 'utf8'), original);
    });
  }

  test('rejects traversal without creating the pending-gates directory', () => {
    const projectRoot = makeProject();

    assert.throws(() => ackPendingGate(projectRoot, '../outside'), /Invalid pending gate phase/);
    assert.equal(existsSync(resolve(projectRoot, '.as-xflow', 'pending-gates')), false);
  });

  test('rejects an invalid phase without touching the pending-gates directory', () => {
    const projectRoot = makeProject();
    const gateFile = resolve(projectRoot, '.as-xflow', 'pending-gates', 'review.json');
    writeGate(gateFile);
    const before = readdirSync(dirname(gateFile)).sort();

    assert.throws(() => ackPendingGate(projectRoot, '..'), /Invalid pending gate phase/);
    assert.throws(() => ackPendingGate(projectRoot, ''), /non-empty string/);
    assert.deepEqual(readdirSync(dirname(gateFile)).sort(), before);
    assert.equal(JSON.parse(readFileSync(gateFile, 'utf8')).status, 'pending');
  });

  test('acknowledges a normal gate', () => {
    const projectRoot = makeProject();
    const gateFile = resolve(projectRoot, '.as-xflow', 'pending-gates', 'review.json');
    writeGate(gateFile);

    const approved = ackPendingGate(projectRoot, 'review', { approved_by: 'test-user' });

    assert.equal(approved.status, 'approved');
    assert.equal(approved.approved_by, 'test-user');
    assert.equal(approved.gate_file, gateFile);
    assert.equal(JSON.parse(readFileSync(gateFile, 'utf8')).status, 'approved');
  });
});
