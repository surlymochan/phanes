import test from 'node:test';
import assert from 'node:assert/strict';
import { freezeApprovedSpec, validateSpec } from '../src/roles/spec.js';
import { spec, intake, approvedReview } from './fixtures/roles-spec/valid.js';

test('rejects empty scenarios, dangling commands, and missing requirements', () => {
  assert.equal(validateSpec({ ...spec, scenarios: [] }, { intake, review: approvedReview }).ok, false);
  assert.ok(validateSpec({ ...spec, scenarios: [{ ...spec.scenarios[0], command_id: 'missing' }] }, { intake, review: approvedReview }).issues.includes('scenario_command_missing:public-api'));
  assert.ok(validateSpec({ ...spec, scenarios: [{ ...spec.scenarios[0], requirement_ref: '' }] }, { intake, review: approvedReview }).issues.includes('scenario_requirement_missing:public-api'));
});

test('requires independent review bound to the spec and freezes a traceable receipt', () => {
  assert.ok(validateSpec(spec, { intake }).issues.includes('approved_spec_review_required'));
  const frozen = freezeApprovedSpec(spec, { intake, review: approvedReview });
  assert.equal(frozen.ok, true);
  assert.equal(frozen.receipt.subject, frozen.digest);
  assert.deepEqual(frozen.spec, spec);
});

test('caller digest cannot approve mutated Spec content', () => {
  const changed = structuredClone(spec);
  changed.scenarios[0].then = [{ channel: 'exit', operator: 'eq', expected: 99 }];
  assert.equal(validateSpec(changed, { intake, review: approvedReview, specDigest: approvedReview.subject }).ok, false);
  assert.equal(freezeApprovedSpec(spec, { intake, review: approvedReview }).approved, true);
});
