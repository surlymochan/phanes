import { describe, test } from 'node:test';
import assert from 'node:assert/strict';

import { buildDemandProfile } from '../src/core/intake-recommendation.js';

describe('demand size validation', () => {
  test('rejects repo counts that are not positive integers with a stable error', () => {
    for (const repo_count of [-1, 1.5, 'NaN']) {
      assert.throws(
        () => buildDemandProfile({ repo_count }),
        { message: 'repo_count must be a positive integer' },
      );
    }
  });

  test('rejects estimated file counts that are not non-negative integers with a stable error', () => {
    for (const estimated_file_count of [-1, 1.5, 'NaN']) {
      assert.throws(
        () => buildDemandProfile({ estimated_file_count }),
        { message: 'estimated_file_count must be a non-negative integer' },
      );
    }
  });

  test('keeps complexity thresholds explicit at file count boundaries', () => {
    const expectedComplexity = new Map([
      [1, 'low'],
      [3, 'low'],
      [4, 'medium'],
      [8, 'medium'],
      [9, 'high'],
      [20, 'high'],
      [21, 'critical'],
    ]);

    for (const [estimated_file_count, complexity] of expectedComplexity) {
      assert.equal(
        buildDemandProfile({ estimated_file_count }).complexity,
        complexity,
        `estimated_file_count ${estimated_file_count}`,
      );
    }
  });
});
