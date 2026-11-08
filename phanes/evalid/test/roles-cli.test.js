import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync, writeFileSync, mkdirSync, symlinkSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import Ajv from 'ajv';
import { runRoleRequest, storeRoleResult } from '../src/roles/index.js';
import { hashPayload } from '../src/core/workflow-integrity.js';
import { freezeApprovedSpec } from '../src/roles/spec.js';
import { runProofAtom } from '../src/roles/tdd.js';

const base = 'a'.repeat(40);
const digest = bytes => `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
const policy = 'sha256:' + 'c'.repeat(64);
const spec = {
  spec_version: 1, scope: { allowed_files: ['src/add.js', 'test/add.test.js'], excluded_files: [] },
  scenarios: [{ id: 'S1', requirement_ref: 'REQ-1', given: 'two numbers', when: 'adding them', then: [{ channel: 'exit', operator: 'eq', expected: 0 }], command_id: 'test' }],
  commands: [{ id: 'test', argv: ['node', '--test', 'test/add.test.js'], cwd: '.', expected_exit: 0, timeout_s: 30, environment_digest: policy }],
};
const review = { subject: `sha256:${hashPayload(spec)}`, verdict: 'PASS', findings: [] };
const metadata = { issue_ref: 'example/project#1', producer: { backend: 'codex', model: 'example', effort: 'high', policy_digest: policy, tool_commit: base } };
const cli = resolve(new URL('../bin/xflow.js', import.meta.url).pathname);
const schema = JSON.parse(readFileSync(new URL('../schemas/evalid-artifact.schema.json', import.meta.url)));
const validate = new Ajv({ strict: true }).compile(schema);
const jsonBytes = value => Buffer.from(`${JSON.stringify(value, null, 2)}\n`);
const seal = body => ({ ...body, evidence_id: `sha256:${hashPayload(body)}` });
function rootEvidence(overrides = {}) {
  return { result: seal({ schema_version: 1, kind: 'intake', change_id: 'sample', attempt: 0,
    base_commit: base, policy_digest: policy, payload: { requirements: ['REQ-1'], holdout_required: true }, ...overrides }), files: {} };
}
function fixture(input = { spec }, overrides = {}, upstream = null) {
  const dir = mkdtempSync(join(tmpdir(), 'evalid-role-'));
  if (!upstream && overrides.stage === 'spec-check') {
    const candidate = fixture();
    try { upstream = candidate.run(); assert.equal(upstream.rc, 0); } finally { candidate.clean(); }
  }
  const request = { schema_version: 1, role: 'evalid-spec', stage: 'spec', change_id: 'sample', attempt: 0, run_id: 'run-1', base_commit: base, head_commit: base, inputs: [], policy_digest: policy, ...overrides };
  const prepare = (data, parent = upstream || rootEvidence()) => {
    const files = { ...parent.files, 'parent.json': jsonBytes(parent.result), 'stage.json': jsonBytes({ ...metadata, ...data }) };
    request.inputs = Object.entries(files).map(([name, bytes]) => {
      writeFileSync(join(dir, name), bytes);
      return { name, sha256: digest(bytes) };
    });
  };
  prepare(input);
  if ('inputs' in overrides) request.inputs = overrides.inputs;
  const execute = (projectRoot = dir, output = 'result.json') => {
    const requestPath = join(dir, 'request.json');
    writeFileSync(requestPath, JSON.stringify(request));
    const proc = spawnSync(process.execPath, [cli, 'role', 'run', '--request', requestPath, '--output', join(dir, output)], { cwd: projectRoot, encoding: 'utf8' });
    const stdout = proc.stdout ? JSON.parse(proc.stdout) : null;
    const stored = join(dir, output);
    const result = proc.status === 0 ? JSON.parse(readFileSync(stored)) : stdout;
    if (proc.status === 0) {
      assert.deepEqual(result, stdout);
      assert.equal(readFileSync(stored).at(-1), 10, 'result ends with a newline');
    }
    const files = Object.fromEntries(readdirSync(dir).filter(name => /^(?:input|evidence|spec|review|tdd|tdd-handoff)-[0-9a-f]{64}\.json$/.test(name)).map(name => [name, readFileSync(join(dir, name))]));
    return { rc: proc.status, result, files, stderr: proc.stderr };
  };
  return { dir, request, prepare, execute, run: (projectRoot = dir) => runRoleRequest(request, { inputRoot: dir, projectRoot }), clean: () => rmSync(dir, { recursive: true, force: true }) };
}
function assertEnvelope(result) {
  assert.equal(validate(result), true, JSON.stringify(validate.errors));
  const { evidence_id, ...body } = result;
  assert.equal(evidence_id, `sha256:${hashPayload(body)}`);
}

test('Spec candidate passes without circular review and stores a schema-valid immutable envelope', () => {
  const f = fixture();
  try {
    const out = f.run();
    assert.equal(out.rc, 0, JSON.stringify(out));
    assertEnvelope(out.result);
    const path = join(f.dir, 'result.json');
    storeRoleResult(path, out.result, out.files);
    assert.equal(digest(readFileSync(join(f.dir, out.result.artifacts[0].name))), out.result.artifacts[0].sha256);
    assert.throws(() => storeRoleResult(path, out.result, out.files), /output_exists/);
    assert.deepEqual(JSON.parse(readFileSync(path)), out.result);
  } finally { f.clean(); }
});

test('strict requests reject empty parents, payload, stage conflict, empty run IDs and partial OIDs', () => {
  for (const overrides of [{ inputs: [] }, { payload: {} }, { stage: 'spec-check' }, { run_id: '' }, { base_commit: 'a'.repeat(41) }, { inputs: [policy] }]) {
    const f = fixture({ spec }, overrides);
    try { assert.equal(f.run().rc, 2); } finally { f.clean(); }
  }
});

test('spec-check binds the subject and refuses PASS with BLOCKER or FAIL', () => {
  for (const [candidate, rc] of [[review, 0], [{ ...review, subject: policy }, 1], [{ ...review, verdict: 'FAIL' }, 1], [{ ...review, findings: [{ severity: 'BLOCKER', requirement_ref: 'REQ-1', artifact_ref: 'spec.json', reason: 'missing behavior' }] }, 2]]) {
    const f = fixture({ spec, review: candidate }, { role: 'evalid-review', stage: 'spec-check' });
    try { const out = f.run(); assert.equal(out.rc, rc, JSON.stringify(out)); if (!rc) assertEnvelope(out.result); } finally { f.clean(); }
  }
});

test('input byte hashes, containment, hidden fields, and missing files fail closed', () => {
  const f = fixture();
  try {
    writeFileSync(join(f.dir, 'stage.json'), '{}');
    assert.equal(f.run().rc, 2);
    f.request.inputs.find(entry => entry.name === 'stage.json').name = 'missing.json';
    assert.equal(f.run().rc, 3);
    symlinkSync(cli, join(f.dir, 'escape.json'));
    f.request.inputs.find(entry => entry.name === 'missing.json').name = 'escape.json';
    assert.equal(f.run().rc, 2);
  } finally { f.clean(); }
  const hidden = fixture({ spec, holdout_commands: ['private'] });
  try { assert.equal(hidden.run().rc, 2); } finally { hidden.clean(); }
});

test('CLI emits the stored evidence id and machine rc 0/1/2/3 without workflows', () => {
  for (const [input, rc] of [[{ spec }, 0], [{ spec: { ...spec, scenarios: [{ ...spec.scenarios[0], command_id: 'missing' }] } }, 1], [{ spec: {} }, 2]]) {
    const f = fixture(input);
    try {
      const requestPath = join(f.dir, 'request.json');
      const output = join(f.dir, 'result.json');
      writeFileSync(requestPath, JSON.stringify(f.request));
      const out = spawnSync(process.execPath, [cli, 'role', 'run', '--request', requestPath, '--output', output], { encoding: 'utf8' });
      assert.equal(out.status, rc, out.stderr);
      assert.deepEqual(JSON.parse(out.stdout), JSON.parse(readFileSync(output)));
      if (!rc) assertEnvelope(JSON.parse(out.stdout));
      assert.equal(spawnSync(process.execPath, [cli, 'role', 'run', '--request', requestPath, '--output', output]).status, 2);
    } finally { f.clean(); }
  }
  assert.equal(spawnSync(process.execPath, [cli, 'role', 'run', '--request', 'absent.json', '--output', 'unused.json']).status, 3);
});

function git(dir, args) {
  const out = spawnSync('git', args, { cwd: dir, encoding: 'utf8' });
  assert.equal(out.status, 0, out.stderr);
  return out.stdout.trim();
}

function assertCliChain(checkTampering = true) {
  const project = mkdtempSync(join(tmpdir(), 'evalid-chain-'));
  const fixtures = [];
  const stages = [];
  const make = (stage, input, upstream, head = baseCommit) => {
    const f = fixture(input, { stage, role: stage === 'spec' ? 'evalid-spec' : ['red', 'green'].includes(stage) ? 'evalid-tdd' : 'evalid-review',
      run_id: `run-${stage}`, base_commit: baseCommit, head_commit: head }, upstream);
    fixtures.push(f);
    return f;
  };
  let baseCommit;
  try {
    git(project, ['init', '--quiet']);
    git(project, ['config', 'user.name', 'Test']);
    git(project, ['config', 'user.email', 'test@example.test']);
    mkdirSync(join(project, 'src'));
    writeFileSync(join(project, 'src/add.js'), 'export const add = () => 0;\n');
    writeFileSync(join(project, 'package.json'), '{"type":"module"}');
    git(project, ['add', 'src', 'package.json']);
    git(project, ['commit', '-qm', 'base']);
    baseCommit = git(project, ['rev-parse', 'HEAD']);
    const intake = rootEvidence({ base_commit: baseCommit });
    const candidate = make('spec', { spec }, intake).execute(project);
    assert.equal(candidate.rc, 0, JSON.stringify(candidate));
    const checked = make('spec-check', { review }, candidate).execute(project);
    assert.equal(checked.rc, 0, JSON.stringify(checked));
    stages.push(candidate, checked);
    const approved = freezeApprovedSpec(spec, { review });
    mkdirSync(join(project, 'test'));
    writeFileSync(join(project, 'test/add.test.js'), "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { add } from '../src/add.js';\ntest('adds', () => assert.equal(add(2, 2), 4));\n");
    git(project, ['add', 'test']);
    git(project, ['commit', '-qm', 'red']);
    const redHead = git(project, ['rev-parse', 'HEAD']);
    const options = { projectRoot: project, changeId: 'sample', testCommand: 'node --test test/add.test.js' };
    assert.equal(runProofAtom({ ...options, phase: 'red' }).ok, true);
    const redInput = { scenario_tests: { S1: ['test/add.test.js'] } };
    const redFixture = make('red', redInput, checked, redHead);
    const red = redFixture.execute(project);
    assert.equal(red.rc, 0, JSON.stringify(red));
    assert.equal(JSON.parse(red.files[red.result.artifacts[1].name]).evidence.proof.phase, 'red');
    stages.push(red);

    // Each intermediate envelope, its input registration, payload and handoff
    // is rejected even when the transport hash is recomputed by the caller.
    const rejectTampering = (f, data, upstream) => {
      if (!checkTampering) return;
      const original = structuredClone(upstream.result);
      const patched = structuredClone(upstream.result);
      patched.run_id = 'tampered';
      f.prepare(data, { ...upstream, result: patched });
      assert.equal(f.execute(project, `reject-envelope-${upstream.result.stage}.json`).rc, 1);
      for (const [name, bytes] of Object.entries(upstream.files)) {
        const value = JSON.parse(bytes);
        const changed = { ...value, tampered: true };
        f.prepare(data, { result: original, files: { ...upstream.files, [name]: jsonBytes(changed) } });
        assert.equal(f.execute(project, `reject-${name}`).rc, 1, name);
      }
      f.prepare(data, upstream);
    };
    rejectTampering(redFixture, redInput, checked);
    // Spec is also rejected by its immediate consumer.
    const checkFixture = make('spec-check', { review }, candidate);
    rejectTampering(checkFixture, { review }, candidate);

    writeFileSync(join(project, 'src/add.js'), 'export const add = (a, b) => a + b;\n');
    git(project, ['add', 'src']);
    git(project, ['commit', '-qm', 'green']);
    const greenHead = git(project, ['rev-parse', 'HEAD']);
    assert.equal(runProofAtom({ ...options, phase: 'green' }).ok, true);
    const greenInput = { scenario_tests: { S1: ['test/add.test.js'] } };
    const greenFixture = make('green', greenInput, red, greenHead);
    rejectTampering(greenFixture, greenInput, red);
    const green = greenFixture.execute(project);
    assert.equal(green.rc, 0, JSON.stringify(green));
    stages.push(green);
    const tdd = JSON.parse(green.files[green.result.artifacts[1].name]);
    assert.equal(tdd.status, 'PASS');
    assert.equal(tdd.digest, `sha256:${hashPayload(green.result.payload)}`);
    const commits = [redHead, greenHead];
    const holdout = { digest: policy, subject_head: greenHead, spec_digest: approved.digest, status: 'PASS', required: true, isolation: 'verified' };
    const deliveryReview = { spec_digest: approved.digest, tdd_digest: tdd.digest, reviewed_base: baseCommit, reviewed_head: greenHead,
      commits, verdict: 'PASS', findings: [], holdout_receipt: holdout.digest };
    const deliveryInput = { review: deliveryReview, holdout, commits };
    const deliveryFixture = make('delivery', deliveryInput, green, greenHead);
    rejectTampering(deliveryFixture, deliveryInput, green);
    const delivery = deliveryFixture.execute(project);
    assert.equal(delivery.rc, 0, JSON.stringify(delivery));
    stages.push(delivery);

    let parent = intake.result;
    for (const out of stages) {
      assertEnvelope(out.result);
      assert.ok(Object.keys(out.files).length > 0, 'CLI writes the evidence closure');
      for (const [name, bytes] of Object.entries(out.files)) {
        assert.equal(digest(bytes), `sha256:${name.match(/-([0-9a-f]{64})\.json$/)[1]}`);
        assert.doesNotThrow(() => JSON.parse(bytes), name);
      }
      for (const artifact of out.result.artifacts) {
        assert.equal(digest(out.files[artifact.name]), artifact.sha256);
        assert.equal(out.files[artifact.name].at(-1), 10, 'artifact ends with a newline');
      }
      assert.equal(out.result.parents[0], parent.evidence_id);
      const records = new Map(Object.values(out.files).map(bytes => JSON.parse(bytes)).filter(value => value.evidence_id).map(value => [value.evidence_id, value]));
      for (const id of out.result.parents) assert.ok(records.has(id), id);
      const source = records.get(out.result.parents[1]);
      assert.equal(source.kind, 'input');
      assert.deepEqual(source.parents, [parent.evidence_id]);
      assert.ok(Object.values(out.files).some(bytes => digest(bytes) === source.artifacts[0].sha256));
      parent = out.result;
    }
    if (!checkTampering) return;
    for (const [patch, reason] of [
      [{ holdout: { ...holdout, status: 'FAIL' } }, 'holdout failure'],
      [{ commits: [] }, 'missing commits'],
      [{ holdout: { ...holdout, status: 'FAIL', unpublished_requirement: true } }, 'NEEDS-BOSS'],
      [{ tdd: { ...tdd, status: 'FAIL' } }, 'forged TDD'],
      [{ approvedSpec: { ...approved, digest: policy } }, 'forged approval'],
    ]) {
      deliveryFixture.prepare({ ...deliveryInput, ...patch }, green);
      assert.equal(deliveryFixture.execute(project, `reject-delivery-${reason.replaceAll(' ', '-')}.json`).rc, 1, reason);
    }
    greenFixture.prepare({ ...greenInput, red: { forged: true } }, red);
    assert.equal(greenFixture.execute(project, 'reject-red-substitution.json').rc, 1);
    greenFixture.prepare(greenInput, red);
    rmSync(join(project, 'specs/changes/sample/tdd/snapshots/green-0'), { recursive: true });
    assert.equal(greenFixture.execute(project, 'missing-snapshot.json').rc, 3);
  } finally {
    for (const f of fixtures) f.clean();
    rmSync(project, { recursive: true, force: true });
  }
}

test('CLI stores parseable evidence and consumes handoffs through Spec → spec-check → red → green → delivery', () => {
  assertCliChain(false);
});

test('CLI chains stages with traceable parents and rejects tampered sealed handoffs', () => {
  assertCliChain();
});

test('content-addressed filenames bind input bytes and reject malformed output extensions', () => {
  const candidate = fixture();
  let consumer;
  try {
    const out = candidate.run();
    assert.equal(out.rc, 0);
    const artifact = out.result.artifacts[0];
    consumer = fixture({ review }, { role: 'evalid-review', stage: 'spec-check' }, out);
    consumer.prepare({ review }, { ...out, files: { ...out.files, [artifact.name]: Buffer.concat([out.files[artifact.name], Buffer.from(' ')]) } });
    assert.deepEqual(consumer.run().result.issues, ['evidence_file_digest_mismatch']);
    const malformed = artifact.name.replace('.json', 'Xjson');
    assert.throws(() => storeRoleResult(join(candidate.dir, 'invalid.json'), out.result,
      { ...out.files, [malformed]: out.files[artifact.name] }), /invalid_output_artifact/);
  } finally { candidate.clean(); consumer?.clean(); }
});

test('parent closure rejects missing, cross-change, cross-attempt, wrong-stage and unregistered inputs', () => {
  const candidate = fixture();
  const out = candidate.run();
  const f = fixture({ review }, { role: 'evalid-review', stage: 'spec-check' }, out);
  try {
    for (const patch of [{ change_id: 'other' }, { attempt: 1 }, { base_commit: 'b'.repeat(40) }, { policy_digest: digest('other') }]) {
      f.prepare({ review }, { ...out, result: seal({ ...out.result, ...patch, evidence_id: undefined }) });
      assert.equal(f.run().rc, 1);
    }
    f.prepare({ review }, { result: out.result, files: {} });
    assert.equal(f.run().rc, 1);
    f.prepare({ review }, rootEvidence());
    assert.equal(f.run().rc, 1);
    f.prepare({ review }, out);
    f.request.inputs = f.request.inputs.filter(entry => entry.name !== 'parent.json');
    assert.equal(f.run().rc, 1);
    assert.throws(() => storeRoleResult(join(f.dir, 'incomplete.json'), out.result), /missing_parent_evidence/);
  } finally { candidate.clean(); f.clean(); }
});
