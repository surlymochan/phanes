import { describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import Ajv from 'ajv';
import { hashPayload } from '../src/core/workflow-integrity.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..');
const FIXTURE_ROOT = resolve(__dirname, 'fixtures', 'roles-schema');

function loadSchema(name) {
  return JSON.parse(readFileSync(resolve(REPO_ROOT, 'schemas', name), 'utf8'));
}

const ajv = new Ajv({ allErrors: true, strict: true });
const validateRole = ajv.compile(loadSchema('evalid-role.schema.json'));
const validateArtifact = ajv.compile(loadSchema('evalid-artifact.schema.json'));
const validateHoldoutReceipt = ajv.compile({
  $ref: 'https://openflow/schemas/evalid-artifact.schema.json#/definitions/holdout_receipt',
});

function readFixture(relativePath) {
  return JSON.parse(readFileSync(resolve(FIXTURE_ROOT, relativePath), 'utf8'));
}

function errorSummary(validate) {
  return (validate.errors || [])
    .map((error) => `${error.keyword} ${error.instancePath || '/'} ${error.message}`)
    .join('\n');
}

function assertValid(validate, data, label) {
  const valid = validate(data);
  assert.equal(valid, true, `${label} should be valid:\n${errorSummary(validate)}`);
}

function assertRejected(validate, data, label, expected) {
  const valid = validate(data);
  assert.equal(valid, false, `${label} should be rejected:\n${errorSummary(validate)}`);
  const errors = validate.errors || [];
  const matched = errors.some((error) => {
    if (error.keyword !== expected.keyword) return false;
    if (expected.property && error.params?.missingProperty !== expected.property
      && error.params?.additionalProperty !== expected.property) return false;
    if (expected.path && !error.instancePath.includes(expected.path)) return false;
    return true;
  });
  assert.equal(
    matched,
    true,
    `${label} should report ${expected.keyword}${expected.property ? ` ${expected.property}` : ''}:\n${errorSummary(validate)}`,
  );
}

describe('evalid role declarations', () => {
  test('the role card header accepts the three roles', () => {
    for (const role of ['spec', 'tdd', 'review']) {
      assertValid(validateRole, readFixture(`valid/role-card-${role}.json`), `role card ${role}`);
    }
  });

  test('a role card may declare a model, the controller owns the conflict check', () => {
    const card = readFixture('valid/role-card-tdd.json');
    assertValid(validateRole, card, 'role card without a model override');
    card.model_override = { backend: 'codex', model: 'gpt-6.1-sol', effort: 'high' };
    assertValid(validateRole, card, 'role card with a model override');
    card.model_override = { backend: 'codex', model: 'gpt-6.1-sol', effort: 'ultra' };
    assertRejected(
      validateRole,
      card,
      'role card with an unknown effort',
      { keyword: 'enum', path: 'effort' },
    );
  });

  test('a role card without a parent record is refused', () => {
    assertRejected(
      validateRole,
      readFixture('invalid/role-card-missing-parent.json'),
      'role card without input evidence',
      { keyword: 'minItems', path: 'input_evidence' },
    );
    assertRejected(
      validateRole,
      readFixture('invalid/role-card-duplicate-parent.json'),
      'role card with duplicated input evidence',
      { keyword: 'uniqueItems', path: 'input_evidence' },
    );
  });

  test('an unknown version or an unknown role is refused', () => {
    assertRejected(
      validateRole,
      readFixture('invalid/role-card-unknown-version.json'),
      'role card with an unknown version',
      { keyword: 'const', path: 'evalid_version' },
    );
    assertRejected(
      validateRole,
      readFixture('invalid/role-card-unknown-role.json'),
      'role card with an unknown role',
      { keyword: 'enum', path: 'role' },
    );
  });

  test('a role card cannot pick its own stage', () => {
    assertRejected(
      validateRole,
      readFixture('invalid/role-card-with-stage.json'),
      'role card that declares a stage',
      { keyword: 'additionalProperties', property: 'stage' },
    );
  });

  test('a lane declaration carries the role only', () => {
    assertValid(validateRole, readFixture('valid/lane-declaration.json'), 'lane declaration');
    assertRejected(
      validateRole,
      readFixture('invalid/lane-declaration-unknown-role.json'),
      'lane declaration with an unknown role',
      { keyword: 'enum', path: 'role' },
    );
    assertRejected(
      validateRole,
      readFixture('invalid/lane-declaration-with-model.json'),
      'lane declaration that also pins a model',
      { keyword: 'additionalProperties', property: 'model' },
    );
  });

  test('the controller stage request pins the role to its stages', () => {
    assertValid(
      validateRole,
      readFixture('valid/role-request-tdd-red.json'),
      'tdd red request',
    );
    assertValid(
      validateRole,
      readFixture('valid/role-request-review-delivery.json'),
      'review delivery request',
    );
    assertRejected(
      validateRole,
      readFixture('invalid/role-request-stage-conflict.json'),
      'review request that asks for the red stage',
      { keyword: 'enum', path: 'stage' },
    );
    assertRejected(
      validateRole,
      readFixture('invalid/role-request-missing-inputs.json'),
      'request without inputs',
      { keyword: 'required', property: 'inputs' },
    );
    assertRejected(
      validateRole,
      readFixture('invalid/role-request-unknown-field.json'),
      'request with an unknown field',
      { keyword: 'additionalProperties', property: 'model' },
    );
  });
});

describe('evalid public artifact envelope', () => {
  const packages = [
    ['valid/spec-envelope.json', 'evalid-spec', 'spec'],
    ['valid/tdd-red-envelope.json', 'evalid-tdd', 'red'],
    ['valid/tdd-green-envelope.json', 'evalid-tdd', 'green'],
    ['valid/review-spec-check-envelope.json', 'evalid-review', 'spec-check'],
    ['valid/review-delivery-envelope.json', 'evalid-review', 'delivery'],
  ];

  test('the spec, tdd and review packages validate', () => {
    for (const [fixture, role, stage] of packages) {
      const data = readFixture(fixture);
      assertValid(validateArtifact, data, fixture);
      assert.equal(data.role, role);
      assert.equal(data.stage, stage);
    }
  });

  test('evidence_id binds the canonical envelope without the field itself', () => {
    for (const [fixture] of packages) {
      const data = readFixture(fixture);
      const { evidence_id: evidenceId, ...rest } = data;
      assert.equal(evidenceId, `sha256:${hashPayload(rest)}`, `${fixture} evidence_id`);
    }
  });

  test('the spec payload keeps the red-green default when the mode is omitted', () => {
    const data = readFixture('valid/spec-envelope.json');
    delete data.payload.tdd_mode;
    const { evidence_id: evidenceId, ...rest } = data;
    data.evidence_id = `sha256:${hashPayload(rest)}`;
    assertValid(validateArtifact, data, 'spec package without an explicit tdd_mode');
  });

  test('a package without a parent record is refused', () => {
    assertRejected(
      validateArtifact,
      readFixture('invalid/spec-envelope-missing-parents.json'),
      'package without parents',
      { keyword: 'required', property: 'parents' },
    );
  });

  test('unknown fields are refused at every level', () => {
    assertRejected(
      validateArtifact,
      readFixture('invalid/spec-envelope-unknown-field.json'),
      'package with an unknown envelope field',
      { keyword: 'additionalProperties', property: 'notes' },
    );
    assertRejected(
      validateArtifact,
      readFixture('invalid/spec-envelope-unknown-payload-field.json'),
      'package with an unknown payload field',
      { keyword: 'additionalProperties', property: 'notes' },
    );
  });

  test('a role cannot run a stage it does not own', () => {
    assertRejected(
      validateArtifact,
      readFixture('invalid/tdd-envelope-role-stage-conflict.json'),
      'tdd package that claims the delivery stage',
      { keyword: 'enum', path: 'stage' },
    );
    assertRejected(
      validateArtifact,
      readFixture('invalid/tdd-envelope-kind-role-conflict.json'),
      'tdd package that claims the spec role',
      { keyword: 'const', path: 'role' },
    );
  });

  test('a malformed digest is refused', () => {
    assertRejected(
      validateArtifact,
      readFixture('invalid/spec-envelope-bad-hash-shape.json'),
      'package with a malformed parent digest',
      { keyword: 'pattern', path: 'parents' },
    );
    assertRejected(
      validateArtifact,
      readFixture('invalid/spec-envelope-bad-evidence-id.json'),
      'package with a malformed evidence id',
      { keyword: 'pattern', path: 'evidence_id' },
    );
  });

  test('a public package never carries hidden holdout material', () => {
    assertRejected(
      validateArtifact,
      readFixture('invalid/spec-envelope-hidden-field.json'),
      'package with a hidden test field',
      { keyword: 'additionalProperties', property: 'hidden_tests' },
    );
    assertRejected(
      validateArtifact,
      readFixture('invalid/spec-envelope-hidden-payload-field.json'),
      'package with a hidden holdout suite',
      { keyword: 'additionalProperties', property: 'holdout_suite' },
    );
  });

  test('a PASS review cannot carry a BLOCKER finding', () => {
    assertRejected(
      validateArtifact,
      readFixture('invalid/review-envelope-pass-with-blocker.json'),
      'PASS review with a BLOCKER finding',
      { keyword: 'not', path: 'severity' },
    );
  });

  test('an observable assertion needs a selector for the json and file channels', () => {
    assertRejected(
      validateArtifact,
      readFixture('invalid/spec-envelope-json-assertion-without-selector.json'),
      'json assertion without a selector',
      { keyword: 'required', property: 'selector' },
    );
  });

  test('a command runs inside the repository', () => {
    assertRejected(
      validateArtifact,
      readFixture('invalid/spec-envelope-command-with-absolute-cwd.json'),
      'command with an absolute working directory',
      { keyword: 'pattern', path: 'cwd' },
    );
  });
});

describe('control plane holdout receipt', () => {
  test('the receipt validates on its own definition and never as a public package', () => {
    const receipt = readFixture('valid/holdout-receipt.json');
    assertValid(validateHoldoutReceipt, receipt, 'holdout receipt');
    assertRejected(
      validateArtifact,
      receipt,
      'holdout receipt used as a public package',
      { keyword: 'required', property: 'kind' },
    );
  });
});
