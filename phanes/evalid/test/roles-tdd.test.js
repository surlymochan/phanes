import Ajv from 'ajv';
import { validateDelivery } from '../src/roles/review.js';
import { freezeApprovedSpec } from '../src/roles/spec.js';
import { spec as publicSpec, intake, approvedReview } from './fixtures/roles-spec/valid.js';
import { describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import {
  TDD_STATES,
  buildTestManifest,
  checkManifestDrift,
  commandProblem,
  countAssertions,
  isConfigPath,
  isFixturePath,
  isProductionPath,
  isTestPath,
  reduceTddStage,
  runTddStage,
  testRunCounts,
} from '../src/roles/tdd.js';

const REPO_ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)));
const FIXTURE_ROOT = resolve(REPO_ROOT, 'test', 'fixtures', 'roles-tdd');

const CHANGE_ID = 'chg-roles-tdd';
const TEST_COMMAND = 'node --test test/feature.test.js';

const SPEC = {
  spec_digest: `sha256:${'a'.repeat(64)}`,
  scenarios: [
    {
      id: 'S1',
      requirement_ref: 'REQ-1',
      given: 'a feature module',
      when: 'the module adds two numbers',
      then: 'the sum is returned',
      command_id: 'C1',
    },
  ],
  scope: {
    allowed_files: ['src/feature.js', 'test/feature.test.js'],
    excluded_files: [],
  },
  tdd_mode: 'red-green',
};

const SCENARIO_TESTS = { S1: ['test/feature.test.js#adds'] };

const RED_TEST_SOURCE = [
  "import test from 'node:test';",
  "import assert from 'node:assert/strict';",
  "import { add } from '../src/feature.js';",
  '',
  "test('adds', () => {",
  '  assert.equal(add(2, 2), 4);',
  '});',
  '',
].join('\n');

const STUB_SOURCE = [
  'export function add(a, b) {',
  '  return 0;',
  '}',
  '',
].join('\n');

const FIXED_SOURCE = [
  'export function add(a, b) {',
  '  return a + b;',
  '}',
  '',
].join('\n');

function fixture(...parts) {
  return JSON.parse(readFileSync(resolve(FIXTURE_ROOT, ...parts), 'utf8'));
}

function codes(result) {
  return result.findings.map((item) => item.code);
}

function git(projectRoot, args) {
  const result = spawnSync('git', args, {
    cwd: projectRoot,
    encoding: 'utf8',
    timeout: 20000,
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return (result.stdout || '').trim();
}

function makeProject() {
  const projectRoot = mkdtempSync(join(tmpdir(), 'evalid-roles-tdd-'));
  git(projectRoot, ['init', '--quiet']);
  git(projectRoot, ['config', 'user.email', 'roles@example.test']);
  git(projectRoot, ['config', 'user.name', 'roles test']);
  writeFileSync(resolve(projectRoot, 'package.json'), '{"type":"module"}\n');
  mkdirSync(resolve(projectRoot, 'src'), { recursive: true });
  writeFileSync(resolve(projectRoot, 'src', 'feature.js'), STUB_SOURCE);
  git(projectRoot, ['add', '.']);
  git(projectRoot, ['commit', '--quiet', '-m', 'baseline']);
  return projectRoot;
}

function writeRedTest(projectRoot) {
  mkdirSync(resolve(projectRoot, 'test'), { recursive: true });
  writeFileSync(resolve(projectRoot, 'test', 'feature.test.js'), RED_TEST_SOURCE);
}

function runRed(projectRoot, baseCommit) {
  return runTddStage({
    projectRoot,
    changeId: CHANGE_ID,
    phase: 'red',
    testCommand: TEST_COMMAND,
    spec: SPEC,
    scenarioTests: SCENARIO_TESTS,
    baseCommit,
  });
}

function runGreen(projectRoot, baseCommit, redEvidence) {
  return runTddStage({
    projectRoot,
    changeId: CHANGE_ID,
    phase: 'green',
    testCommand: TEST_COMMAND,
    redEvidence,
    spec: SPEC,
    scenarioTests: SCENARIO_TESTS,
    baseCommit,
    diffRef: baseCommit,
  });
}

describe('TDD role reducer', () => {
  test('an assertion red with a replay receipt, a snapshot and a scenario mapping is accepted', () => {
    const input = fixture('red', 'accepted.json');
    const result = reduceTddStage(input);
    assert.equal(result.ok, true, JSON.stringify(result.findings));
    assert.equal(result.state, TDD_STATES.RED_ACCEPTED);
    assert.deepEqual(result.findings.filter((item) => item.severity !== 'warn'), []);
    assert.equal(result.payload.phase, 'red');
    assert.equal(result.payload.spec_digest, input.spec.spec_digest);
    assert.deepEqual(result.payload.scenario_tests, SCENARIO_TESTS);
    assert.equal(Object.keys(result.payload.test_manifest).length, 2);
  });

  test('a red recorded on a tree that already carries the implementation is refused', () => {
    const result = reduceTddStage(fixture('red', 'posthoc-production.json'));
    assert.equal(result.ok, false);
    assert.equal(result.state, TDD_STATES.RED_REJECTED);
    assert.ok(codes(result).includes('red_production_changed'));
    assert.deepEqual(
      result.findings.find((item) => item.code === 'red_production_changed').paths,
      ['src/feature.js'],
    );
  });

  test('a red that fails on an import error is refused', () => {
    const result = reduceTddStage(fixture('red', 'not-assertion.json'));
    assert.equal(result.ok, false);
    assert.equal(result.state, TDD_STATES.RED_REJECTED);
    assert.ok(codes(result).includes('red_failure_not_assertion'));
  });

  test('a red that ran no test at all is refused', () => {
    const result = reduceTddStage(fixture('red', 'empty-tests.json'));
    assert.equal(result.ok, false);
    assert.equal(result.state, TDD_STATES.RED_REJECTED);
    assert.ok(codes(result).includes('tests_empty'));
    assert.ok(codes(result).includes('no_assertions'));
  });

  test('a red without a sealed snapshot cannot be replayed', () => {
    const result = reduceTddStage(fixture('red', 'missing-snapshot.json'));
    assert.equal(result.ok, false);
    assert.equal(result.state, TDD_STATES.INFRA);
    assert.ok(codes(result).includes('snapshot_missing'));
  });

  test('a red without a scenario mapping is refused', () => {
    const result = reduceTddStage(fixture('red', 'unmapped-scenario.json'));
    assert.equal(result.ok, false);
    assert.equal(result.state, TDD_STATES.RED_REJECTED);
    assert.ok(codes(result).includes('scenario_unmapped'));
  });

  test('the same frozen tests turning green is accepted', () => {
    const result = reduceTddStage(fixture('green', 'accepted.json'));
    assert.equal(result.ok, true, JSON.stringify(result.findings));
    assert.equal(result.state, TDD_STATES.GREEN_ACCEPTED);
    assert.equal(result.payload.phase, 'green');
  });

  test('a green that edits the frozen test is refused', () => {
    const result = reduceTddStage(fixture('green', 'test-changed.json'));
    assert.equal(result.ok, false);
    assert.equal(result.state, TDD_STATES.GREEN_REJECTED);
    assert.ok(codes(result).includes('green_test_changed'));
  });

  test('a green that edits the frozen runner configuration is refused', () => {
    const result = reduceTddStage(fixture('green', 'config-changed.json'));
    assert.equal(result.ok, false);
    assert.equal(result.state, TDD_STATES.GREEN_REJECTED);
    assert.ok(codes(result).includes('green_config_changed'));
  });

  test('a green without an accepted red is refused', () => {
    const input = fixture('green', 'accepted.json');
    input.red.receipt.ok = false;
    const result = reduceTddStage(input);
    assert.equal(result.ok, false);
    assert.equal(result.state, TDD_STATES.GREEN_REJECTED);
    assert.ok(codes(result).includes('green_red_not_accepted'));
    assert.ok(codes(result).includes('red_replay_unverified'));
  });

  test('a stage without a known phase is refused', () => {
    const result = reduceTddStage({ phase: 'blue' });
    assert.equal(result.ok, false);
    assert.deepEqual(codes(result), ['invalid_phase']);
  });

  test('a green whose commit does not contain the red commit is refused', () => {
    const input = fixture('green', 'accepted.json');
    input.contains_red = false;
    const result = reduceTddStage(input);
    assert.equal(result.ok, false);
    assert.ok(codes(result).includes('green_missing_red_commit'));
  });

  test('a test that moved between the proof and the manifest is refused', () => {
    const input = fixture('red', 'accepted.json');
    const drifted = checkManifestDrift({
      manifest: input.red.manifest,
      proof: input.red.proof,
    });
    assert.deepEqual(drifted, []);

    input.red.manifest['test/feature.test.js'] = 'f'.repeat(64);
    const findings = checkManifestDrift({
      manifest: input.red.manifest,
      proof: input.red.proof,
    });
    assert.equal(findings.length, 1);
    assert.equal(findings[0].code, 'test_manifest_drift');
    assert.equal(findings[0].path, 'test/feature.test.js');
  });

  test('a scenario mapped to a test outside the manifest is refused', () => {
    const input = fixture('red', 'accepted.json');
    input.scenario_tests = { S1: 'test/other.test.js#adds' };
    const result = reduceTddStage(input);
    assert.equal(result.ok, false);
    assert.ok(codes(result).includes('scenario_test_missing'));
  });
});

describe('TDD role evidence helpers', () => {
  test('test, fixture, configuration and production files are told apart', () => {
    assert.equal(isTestPath('test/feature.test.js'), true);
    assert.equal(isTestPath('tests/test_feature.py'), true);
    assert.equal(isTestPath('src/feature.js'), false);
    assert.equal(isTestPath('test/fixtures/sample/app.js'), false);
    assert.equal(isFixturePath('test/fixtures/sample/app.js'), true);
    assert.equal(isFixturePath('test/feature.test.js'), false);
    assert.equal(isConfigPath('package.json'), true);
    assert.equal(isConfigPath('package-lock.json'), true);
    assert.equal(isConfigPath('jest.config.js'), true);
    assert.equal(isConfigPath('src/feature.js'), false);
    assert.equal(isProductionPath('src/feature.js'), true);
    assert.equal(isProductionPath('test/feature.test.js'), false);
    assert.equal(isProductionPath('package.json'), false);
  });

  test('assertions are counted the way the quality gate counts them', () => {
    assert.equal(countAssertions('assert.equal(add(2, 2), 4);'), 1);
    assert.equal(countAssertions('expect(value).toBe(4);'), 1);
    assert.equal(countAssertions('import assert from \'node:assert/strict\';'), 0);
    assert.equal(countAssertions('test(\'empty\', () => {});'), 0);
  });

  test('test counts are read from the runner summary, never guessed', () => {
    const tap = '# tests 3\n# suites 0\n# pass 2\n# fail 1\n# cancelled 0\n# skipped 0\n# todo 0\n';
    assert.equal(testRunCounts(tap).tests, 3);
    const spec = '\u2139 tests 1\n\u2139 suites 0\n\u2139 pass 1\n\u2139 fail 0\n\u2139 cancelled 0\n\u2139 skipped 0\n\u2139 todo 0\n';
    assert.equal(testRunCounts(spec).tests, 1);
    assert.equal(testRunCounts('2 passed in 0.01s').tests, 2);
    assert.equal(testRunCounts('no tests ran').tests, 0);
    assert.equal(testRunCounts('nothing useful here'), null);
  });

  test('green must run the test red froze, or a superset of it', () => {
    assert.equal(commandProblem('node --test test/a.test.js', 'node --test test/a.test.js'), null);
    assert.equal(commandProblem('node --test test/a.test.js', 'node --test test/a.test.js test/b.test.js'), null);
    assert.equal(commandProblem('node --test test/a.test.js', 'node --test test/b.test.js'), 'red_green_command_mismatch');
    assert.equal(commandProblem('exit 1', 'node --test test/a.test.js'), 'red_command_not_a_test');
  });
});

describe('TDD role stage adapter', () => {
  test('a real red to green run is accepted', () => {
    const projectRoot = makeProject();
    try {
      const base = git(projectRoot, ['rev-parse', 'HEAD']);
      writeRedTest(projectRoot);
      git(projectRoot, ['add', 'test/feature.test.js']);
      git(projectRoot, ['commit', '--quiet', '-m', 'red tests']);

      const red = runRed(projectRoot, base);
      assert.equal(red.ok, true, JSON.stringify(red.findings));
      assert.equal(red.state, TDD_STATES.RED_ACCEPTED);
      assert.equal(red.evidence.proof.failure_kind, 'assertion');
      assert.equal(red.evidence.proof.passed, false);
      assert.equal(red.evidence.receipt.ok, true);
      assert.deepEqual(red.evidence.changed_paths, ['test/feature.test.js']);
      assert.equal(red.gates.length, 2);
      assert.ok(red.gates.every((gate) => gate.ok === true && gate.rc === 0));

      writeFileSync(resolve(projectRoot, 'src', 'feature.js'), FIXED_SOURCE);
      git(projectRoot, ['add', 'src/feature.js']);
      git(projectRoot, ['commit', '--quiet', '-m', 'green implementation']);

      const green = runGreen(projectRoot, base, red.evidence);
      assert.equal(green.ok, true, JSON.stringify(green.findings));
      assert.equal(green.state, TDD_STATES.GREEN_ACCEPTED);
      assert.equal(green.evidence.proof.passed, true);
      assert.equal(green.quality.ok, true, JSON.stringify(green.quality));
      assert.equal(green.gates.length, 3);
      assert.ok(green.gates.every((gate) => gate.ok === true && gate.rc === 0));
      assert.equal(green.payload.phase, 'green');
      const schema = JSON.parse(readFileSync(resolve(REPO_ROOT, 'schemas/evalid-artifact.schema.json'), 'utf8'));
      const validate = new Ajv({ strict: false }).compile({ definitions: schema.definitions, $ref: '#/definitions/tdd_payload' });
      assert.equal(validate(green.payload), true, JSON.stringify(validate.errors));

      assert.deepEqual(green.payload.scenario_tests, SCENARIO_TESTS);
      assert.deepEqual(
        Object.keys(green.payload.test_manifest).sort(),
        ['package.json', 'test/feature.test.js'],
      );
    } finally {
      rmSync(projectRoot, { recursive: true, force: true });
    }
  });

  test('a red recorded after the implementation existed is refused', () => {
    const projectRoot = makeProject();
    try {
      const base = git(projectRoot, ['rev-parse', 'HEAD']);
      // The implementation lands before the red: the production tree at red
      // is no longer the frozen base.
      writeFileSync(resolve(projectRoot, 'src', 'feature.js'), 'export function add(a, b) {\n  return a - b;\n}\n');
      writeRedTest(projectRoot);
      git(projectRoot, ['add', 'src/feature.js', 'test/feature.test.js']);
      git(projectRoot, ['commit', '--quiet', '-m', 'implementation and tests']);

      const red = runRed(projectRoot, base);
      assert.equal(red.ok, false);
      assert.equal(red.state, TDD_STATES.RED_REJECTED);
      assert.ok(codes(red).includes('red_production_changed'));
    } finally {
      rmSync(projectRoot, { recursive: true, force: true });
    }
  });

  test('a green that edits the frozen test is refused', () => {
    const projectRoot = makeProject();
    try {
      const base = git(projectRoot, ['rev-parse', 'HEAD']);
      writeRedTest(projectRoot);
      git(projectRoot, ['add', 'test/feature.test.js']);
      git(projectRoot, ['commit', '--quiet', '-m', 'red tests']);
      const red = runRed(projectRoot, base);
      assert.equal(red.ok, true, JSON.stringify(red.findings));

      writeFileSync(resolve(projectRoot, 'src', 'feature.js'), FIXED_SOURCE);
      writeFileSync(resolve(projectRoot, 'test', 'feature.test.js'), RED_TEST_SOURCE.replace('4', '5'));
      git(projectRoot, ['add', 'src/feature.js', 'test/feature.test.js']);
      git(projectRoot, ['commit', '--quiet', '-m', 'green with an edited test']);

      const green = runGreen(projectRoot, base, red.evidence);
      assert.equal(green.ok, false);
      assert.equal(green.state, TDD_STATES.GREEN_REJECTED);
      assert.ok(codes(green).includes('green_test_changed'));
    } finally {
      rmSync(projectRoot, { recursive: true, force: true });
    }
  });

  test('a green without a quality gate is refused', () => {
    const projectRoot = makeProject();
    try {
      const base = git(projectRoot, ['rev-parse', 'HEAD']);
      writeRedTest(projectRoot);
      git(projectRoot, ['add', 'test/feature.test.js']);
      git(projectRoot, ['commit', '--quiet', '-m', 'red tests']);
      const red = runRed(projectRoot, base);
      assert.equal(red.ok, true, JSON.stringify(red.findings));

      writeFileSync(resolve(projectRoot, 'src', 'feature.js'), FIXED_SOURCE);
      git(projectRoot, ['add', 'src/feature.js']);
      git(projectRoot, ['commit', '--quiet', '-m', 'green implementation']);

      const green = runTddStage({
        projectRoot,
        changeId: CHANGE_ID,
        phase: 'green',
        testCommand: TEST_COMMAND,
        redEvidence: red.evidence,
        spec: SPEC,
        scenarioTests: SCENARIO_TESTS,
        baseCommit: base,
      });
      assert.equal(green.ok, false);
      assert.ok(codes(green).includes('quality_gate_missing'));
    } finally {
      rmSync(projectRoot, { recursive: true, force: true });
    }
  });

  test('the test manifest holds the visible tests and the runner configuration', () => {
    const projectRoot = makeProject();
    try {
      writeRedTest(projectRoot);
      const built = buildTestManifest({ projectRoot, changeId: CHANGE_ID });
      assert.deepEqual(built.test_files, ['test/feature.test.js']);
      assert.deepEqual(built.config_files, ['package.json']);
      assert.deepEqual(built.fixture_files, []);
      assert.equal(built.assertions['test/feature.test.js'], 1);
      assert.match(built.manifest['test/feature.test.js'], /^[0-9a-f]{64}$/);
      assert.match(built.manifest['package.json'], /^[0-9a-f]{64}$/);
    } finally {
      rmSync(projectRoot, { recursive: true, force: true });
    }
  });
});

test('green refuses failed replay, quality and unknown ancestry', () => {
  for (const mutate of [
    input => { input.green.receipt.ok = false; },
    input => { input.gates.find(g => g.id === 'I6c.tdd.quality_review').ok = false; },
    input => { input.gates.find(g => g.id === 'I6b.tdd.proof_validate').rc = 1; },
    input => { delete input.contains_red; },
  ]) {
    const input = fixture('green', 'accepted.json');
    mutate(input);
    assert.equal(reduceTddStage(input).ok, false);
  }
});

test('different test selectors cannot prove red coverage', () => {
  assert.equal(commandProblem('node --test --test-name-pattern=red test/a.test.js', 'node --test --test-name-pattern=other test/a.test.js'), 'red_green_command_mismatch');
});

test('schema array scenario mappings and accepted delivery receipt agree', () => {
  const input = fixture('green', 'accepted.json');
  input.scenario_tests.S1 = [input.scenario_tests.S1];
  const result = reduceTddStage(input);
  assert.equal(result.ok, true);
  assert.equal(result.status, 'PASS');
  assert.match(result.digest, /^sha256:[a-f0-9]{64}$/);
  assert.ok(Object.values(result.payload.test_manifest).every(hash => hash.startsWith('sha256:')));
  assert.ok(result.payload.proof_refs.every(ref => ref.artifact_ref));
  assert.ok(result.payload.gate_results.every(gate => Number.isInteger(gate.exit_code)));
});

test('actual Spec freezer and TDD reducer hand off to delivery Review', () => {
  const approvedSpec = freezeApprovedSpec(publicSpec, { intake, review: approvedReview });
  const input = fixture('green', 'accepted.json');
  input.spec.spec_digest = approvedSpec.digest;
  const tdd = reduceTddStage(input);
  assert.equal(tdd.ok, true);
  const baseCommit = input.base_commit;
  const headCommit = input.green.source_commit;
  const holdout = { digest: `sha256:${'c'.repeat(64)}`, subject_head: headCommit, spec_digest: approvedSpec.digest, status: 'PASS', isolation: 'verified' };
  const review = { spec_digest: approvedSpec.digest, tdd_digest: tdd.digest, reviewed_base: baseCommit, reviewed_head: headCommit, commits: [headCommit], verdict: 'PASS', findings: [], holdout_receipt: holdout.digest };
  assert.equal(validateDelivery({ approvedSpec, tdd, holdout, review, baseCommit, headCommit, commits: [headCommit] }).verdict, 'PASS');
});
