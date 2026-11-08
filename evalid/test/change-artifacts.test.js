import { describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { join, resolve, sep } from 'node:path';
import { tmpdir } from 'node:os';

import { resolveChangeDir, resolveChangePath } from '../src/core/change-artifacts.js';
import { writeWorkflowRecommendation } from '../src/core/intake-recommendation.js';

const UNSAFE_CHANGE_IDS = [
  '../escape',
  '../../escape',
  '..',
  'nested/child',
  'nested\\child',
  '/tmp/absolute-escape',
];

function makeProjectRoot() {
  const root = mkdtempSync(join(tmpdir(), 'openflow-change-'));
  mkdirSync(resolve(root, 'specs', 'changes'), { recursive: true });
  return root;
}

function cleanupProjectRoot(root) {
  rmSync(root, { recursive: true, force: true });
}

describe('change artifact paths', () => {
  test('rejects change ids that escape the specs changes directory', () => {
    const projectRoot = makeProjectRoot();
    try {
      for (const changeId of UNSAFE_CHANGE_IDS) {
        assert.throws(
          () => resolveChangeDir(projectRoot, changeId),
          `resolveChangeDir should reject change_id: ${changeId}`,
        );
        assert.throws(
          () => resolveChangePath(projectRoot, changeId, 'workflow_recommendation.json'),
          `resolveChangePath should reject change_id: ${changeId}`,
        );
      }
    } finally {
      cleanupProjectRoot(projectRoot);
    }
  });

  test('resolves safe change ids inside the specs changes directory', () => {
    const projectRoot = makeProjectRoot();
    try {
      const changesRoot = resolve(projectRoot, 'specs', 'changes');
      assert.equal(resolveChangeDir(projectRoot, 'chg-123'), resolve(changesRoot, 'chg-123'));
      assert.equal(
        resolveChangePath(projectRoot, 'chg-123', 'workflow_recommendation.json'),
        resolve(changesRoot, 'chg-123', 'workflow_recommendation.json'),
      );
      assert.ok(
        resolveChangePath(projectRoot, 'chg-123', 'workflow_recommendation.json').startsWith(`${changesRoot}${sep}`),
      );
    } finally {
      cleanupProjectRoot(projectRoot);
    }
  });

  test('still requires a change id', () => {
    const projectRoot = makeProjectRoot();
    try {
      assert.throws(() => resolveChangeDir(projectRoot, ''));
      assert.throws(() => resolveChangeDir(projectRoot, null));
    } finally {
      cleanupProjectRoot(projectRoot);
    }
  });

  test('writes the workflow recommendation inside the change directory', () => {
    const projectRoot = makeProjectRoot();
    try {
      const result = writeWorkflowRecommendation({
        projectRoot,
        changeId: 'chg-safe',
        input: { title: 'safe change' },
      });

      const expectedFile = resolve(projectRoot, 'specs', 'changes', 'chg-safe', 'workflow_recommendation.json');
      assert.equal(result.ok, true);
      assert.equal(result.workflow_recommendation_file, expectedFile);
      assert.equal(existsSync(expectedFile), true);
      assert.equal(typeof JSON.parse(readFileSync(expectedFile, 'utf8')).recommended_track, 'string');
    } finally {
      cleanupProjectRoot(projectRoot);
    }
  });

  test('does not write recommendation artifacts outside the specs changes directory', () => {
    const projectRoot = makeProjectRoot();
    try {
      assert.throws(() => writeWorkflowRecommendation({
        projectRoot,
        changeId: '../escape',
        input: { title: 'escape' },
      }));

      assert.equal(existsSync(resolve(projectRoot, 'specs', 'escape')), false);
      assert.equal(existsSync(resolve(projectRoot, 'specs', 'escape', 'workflow_recommendation.json')), false);
      assert.equal(existsSync(resolve(projectRoot, 'workflow_recommendation.json')), false);
    } finally {
      cleanupProjectRoot(projectRoot);
    }
  });
});
