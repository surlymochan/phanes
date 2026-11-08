import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { validateDelivery } from '../src/roles/review.js';

const fixture = JSON.parse(await readFile(new URL('./fixtures/roles-review/delivery.json', import.meta.url), 'utf8'));
const run = (changes = {}) => validateDelivery({ ...fixture, ...changes });

test('accepts a complete review with verified holdout', () => assert.equal(run().verdict, 'PASS'));
test('does not let review PASS override failed, unknown, or missing holdout', () => {
  for (const holdout of [{ ...fixture.holdout, status: 'FAIL' }, { ...fixture.holdout, status: 'unknown' }, null]) assert.equal(run({ holdout }).verdict, 'FAIL');
});
test('BLOCKER rejects while FOLLOW-UP does not block', () => {
  assert.equal(run({ review: { ...fixture.review, findings: [{ severity: 'BLOCKER', reason: 'x' }] } }).verdict, 'FAIL');
  assert.equal(run({ review: { ...fixture.review, findings: [{ severity: 'FOLLOW-UP', reason: 'x' }] } }).verdict, 'PASS');
});
test('rejects stale head and incomplete commit chain', () => {
  assert.equal(run({ headCommit: '3333333333333333333333333333333333333333' }).verdict, 'FAIL');
  assert.equal(run({ commits: [] }).verdict, 'FAIL');
});
test('unpublished requirements escalate to NEEDS-BOSS, never author BLOCKER', () => {
  const result = run({ holdout: { ...fixture.holdout, status: 'FAIL', unpublished_requirement: true } });
  assert.equal(result.verdict, 'NEEDS-BOSS');
  assert.equal(result.findings.some(f => f.code === 'blocker'), false);
});

test('holdout binds head, Spec, receipt and frozen exemption', () => {
  for (const patch of [{ subject_head: fixture.baseCommit }, { spec_digest: fixture.holdout.digest }, { digest: fixture.approvedSpec.digest }]) {
    assert.equal(run({ holdout: { ...fixture.holdout, ...patch } }).verdict, 'FAIL');
  }
  const holdout = { ...fixture.holdout, status: 'NOT_APPLICABLE', required: false };
  assert.equal(run({ holdout }).verdict, 'FAIL');
  assert.equal(run({ holdout, intake: { holdout_required: false } }).verdict, 'PASS');
});
