import { test } from 'node:test';
import assert from 'node:assert/strict';

import { validateTeamPolicy } from '../src/core/policy-overlay.js';

for (const [label, min_reviewers] of [
  ['fractional number', 1.5],
  ['fractional numeric string', '1.5'],
  ['empty string', ''],
  ['whitespace string', '   '],
  ['NaN', NaN],
  ['negative number', -1],
]) {
  test(`team policy rejects ${label} reviewer count`, () => {
    const validation = validateTeamPolicy({ review: { min_reviewers } });

    assert.equal(validation.ok, false, `expected ${JSON.stringify(min_reviewers)} to be rejected`);
    assert.ok(
      validation.errors.some((error) => /review\.min_reviewers.*non-negative integer/.test(error)),
      `expected a clear reviewer count validation error for ${JSON.stringify(min_reviewers)}`,
    );
  });
}

test('team policy accepts zero and positive integer reviewer counts', () => {
  for (const min_reviewers of [0, 1, 2, '2']) {
    const validation = validateTeamPolicy({ review: { min_reviewers } });

    assert.equal(validation.ok, true, `expected ${JSON.stringify(min_reviewers)} to be accepted`);
    assert.deepEqual(validation.errors, []);
  }
});

test('team policy still accepts fractional coverage and smoke pass rates', () => {
  const validation = validateTeamPolicy({
    coding: { min_changed_line_coverage: 87.5 },
    review: { min_reviewers: 2 },
    deploy: { required_smoke_pass_rate: 99.5 },
  });

  assert.equal(validation.ok, true);
  assert.deepEqual(validation.errors, []);
});
