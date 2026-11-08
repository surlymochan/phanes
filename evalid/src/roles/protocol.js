import { createHash, randomUUID } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync, linkSync, unlinkSync, realpathSync } from 'node:fs';
import { dirname, resolve, relative, isAbsolute } from 'node:path';
import Ajv from 'ajv';
import { hashPayload } from '../core/workflow-integrity.js';
import { freezeApprovedSpec, validateSpec } from './spec.js';
import { runTddStage } from './tdd.js';
import { validateDelivery } from './review.js';

const digest = (bytes) => `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
const schema = (name) => JSON.parse(readFileSync(new URL(`../../schemas/${name}.schema.json`, import.meta.url), 'utf8'));
const ajv = new Ajv({ allErrors: true, strict: true });
ajv.addSchema(schema('evalid-role'));
ajv.addSchema(schema('evalid-artifact'));
const definition = (file, name) => ajv.compile({ $ref: `https://openflow/schemas/evalid-${file}.schema.json#/definitions/${name}` });
const validateRequest = definition('role', 'role_request');
const validateEnvelope = ajv.getSchema('https://openflow/schemas/evalid-artifact.schema.json');
const validateProducer = definition('artifact', 'producer');
const validateArtifact = definition('artifact', 'artifact_entry');
const validatePayload = Object.fromEntries(['spec_payload', 'spec_check_payload', 'delivery_payload'].map(name => [name, definition('artifact', name)]));
const jsonBytes = value => Buffer.from(`${JSON.stringify(value, null, 2)}\n`);
const artifactFilename = /^(?:input|evidence|spec|review|tdd|tdd-handoff)-([0-9a-f]{64})\.json$/;
const seal = body => ({ ...body, evidence_id: `sha256:${hashPayload(body)}` });
const same = (a, b) => hashPayload(a) === hashPayload(b);
const previousStage = { spec: 'intake', 'spec-check': 'spec', red: 'spec-check', green: 'red', delivery: 'green' };
const failure = (rc, code, issues = []) => ({ rc, result: { ok: false, code, issues } });

// Transport paths stay outside the public request and envelope.
function readInputs(request, inputRoot) {
  const root = realpathSync(inputRoot);
  const inputs = new Map();
  for (const entry of request.inputs) {
    if (inputs.has(entry.name)) throw new Error('duplicate_input_name');
    const path = realpathSync(resolve(root, entry.name));
    const rel = relative(root, path);
    if (rel === '..' || rel.startsWith('../') || isAbsolute(rel)) throw new Error('input_outside_root');
    const bytes = readFileSync(path);
    if (digest(bytes) !== entry.sha256) throw new Error('input_digest_mismatch');
    inputs.set(entry.name, bytes);
  }
  if (!inputs.has('stage.json')) throw new Error('stage_input_required');
  return inputs;
}

// Every dependency must be included in the controller's byte-pinned transport.
// Intake/input records are transport evidence, not additional role stages.
function readEvidence(inputs, request, expectedStage = previousStage[request.stage]) {
  const records = new Map();
  const blobs = new Map([...inputs.values()].map(bytes => [digest(bytes), bytes]));
  for (const [name, bytes] of inputs) {
    const namedHash = artifactFilename.exec(name);
    if (namedHash && digest(bytes) !== `sha256:${namedHash[1]}`) throw new Error('evidence_file_digest_mismatch');
    let value;
    try { value = JSON.parse(bytes); } catch { continue; }
    if (value?.evidence_id) {
      const { evidence_id, ...body } = value;
      if (evidence_id !== seal(body).evidence_id) throw new Error('evidence_digest_mismatch');
      records.set(evidence_id, value);
    }
  }
  const artifact = entry => {
    const bytes = blobs.get(entry.sha256);
    if (!bytes) throw new Error('missing_evidence_artifact');
    return bytes;
  };
  const visited = new Set();
  const active = new Set();
  const visit = id => {
    if (active.has(id)) throw new Error('evidence_cycle');
    if (visited.has(id)) return records.get(id);
    const record = records.get(id);
    if (!record) throw new Error('missing_parent_evidence');
    const { evidence_id, ...body } = record;
    if (evidence_id !== seal(body).evidence_id) throw new Error('evidence_digest_mismatch');
    if (record.change_id !== request.change_id || record.attempt !== request.attempt || record.base_commit !== request.base_commit) throw new Error('evidence_binding_mismatch');
    active.add(id);
    if (record.kind === 'intake') {
      const keys = ['schema_version', 'kind', 'change_id', 'attempt', 'base_commit', 'policy_digest', 'payload', 'evidence_id'];
      if (Object.keys(record).some(key => !keys.includes(key)) || keys.some(key => !(key in record)) || record.schema_version !== 1 || record.policy_digest !== request.policy_digest || !record.payload || typeof record.payload !== 'object' || Array.isArray(record.payload) || Object.keys(record.payload).some(key => !['requirements', 'holdout_required'].includes(key))) throw new Error('invalid_intake_evidence');
    } else if (record.kind === 'input') {
      const keys = ['schema_version', 'kind', 'stage', 'change_id', 'attempt', 'base_commit', 'policy_digest', 'parents', 'artifacts', 'evidence_id'];
      if (Object.keys(record).some(key => !keys.includes(key)) || keys.some(key => !(key in record)) || record.schema_version !== 1 || record.policy_digest !== request.policy_digest || record.parents?.length !== 1 || record.artifacts?.length !== 1 || !previousStage[record.stage]) throw new Error('invalid_input_evidence');
      const parent = visit(record.parents[0]);
      if ((parent.stage || parent.kind) !== previousStage[record.stage]) throw new Error('parent_stage_mismatch');
      if (!validateArtifact(record.artifacts[0])) throw new Error('invalid_input_artifact');
      artifact(record.artifacts[0]);
    } else {
      if (!validateEnvelope(record) || record.producer.policy_digest !== request.policy_digest) throw new Error('invalid_parent_envelope');
      if (record.parents.length !== 2) throw new Error('invalid_stage_parents');
      const parent = visit(record.parents[0]);
      const source = visit(record.parents[1]);
      if ((parent.stage || parent.kind) !== previousStage[record.stage] || source.kind !== 'input' || source.stage !== record.stage || source.parents[0] !== parent.evidence_id) throw new Error('parent_stage_mismatch');
      for (const entry of record.artifacts) artifact(entry);
      if (!same(JSON.parse(artifact(record.artifacts[0])), record.payload)) throw new Error('payload_artifact_mismatch');
      if (record.kind === 'tdd') readTdd(record, artifact);
    }
    active.delete(id);
    visited.add(id);
    return record;
  };
  if (!inputs.has('parent.json')) throw new Error('parent_input_required');
  const parent = JSON.parse(inputs.get('parent.json'));
  visit(parent.evidence_id);
  if ((parent.stage || parent.kind) !== expectedStage) throw new Error('parent_stage_mismatch');
  const lineage = [];
  for (let current = parent; current; current = records.get(current.parents?.[0])) lineage.push(current);
  return { parent, lineage, artifact };
}

function readTdd(envelope, artifact) {
  const entry = envelope.artifacts[1];
  if (!entry) throw new Error('tdd_handoff_required');
  const bundle = JSON.parse(artifact(entry));
  if (!same(bundle.payload, envelope.payload) || bundle.digest !== `sha256:${hashPayload(envelope.payload)}` || bundle.status !== 'PASS' || bundle.phase !== envelope.stage || bundle.spec_digest !== envelope.payload.spec_digest || !bundle.evidence?.proof || bundle.evidence.source_commit !== envelope.head_commit) throw new Error('invalid_tdd_handoff');
  return bundle;
}

// Duplicated caller fields may not replace the sealed upstream values.
function bindInput(input, key, value) {
  if (key in input && !same(input[key], value)) throw new Error(`upstream_mismatch:${key}`);
  input[key] = value;
}

export function runRoleRequest(request, { inputRoot = process.cwd(), projectRoot = process.cwd() } = {}) {
  if (!validateRequest(request)) return failure(2, 'invalid_request', structuredClone(validateRequest.errors));
  let inputs;
  let input;
  let chain;
  try {
    inputs = readInputs(request, inputRoot);
    input = JSON.parse(inputs.get('stage.json'));
  } catch (error) {
    return failure(error.code ? 3 : 2, 'invalid_input', [error.message]);
  }
  const allowed = ['issue_ref', 'producer', 'spec', 'intake', 'review', 'approvedSpec', 'tdd', 'holdout', 'commits', 'scenario_tests', 'red'];
  if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).some(key => !allowed.includes(key))) return failure(2, 'invalid_stage_input');
  if (typeof input.issue_ref !== 'string' || !input.issue_ref.trim() || !validateProducer(input.producer) || input.producer.policy_digest !== request.policy_digest) return failure(2, 'invalid_producer_or_issue');
  const { stage } = request;
  try {
    chain = readEvidence(inputs, request);
    const intake = chain.lineage.find(record => record.kind === 'intake');
    bindInput(input, 'intake', intake.payload);
    if (chain.parent.issue_ref && chain.parent.issue_ref !== input.issue_ref) throw new Error('issue_ref_mismatch');
    if (stage === 'spec-check') bindInput(input, 'spec', chain.parent.payload);
    if (['red', 'green', 'delivery'].includes(stage)) {
      const candidate = chain.lineage.find(record => record.stage === 'spec');
      const check = chain.lineage.find(record => record.stage === 'spec-check');
      const approved = freezeApprovedSpec(candidate.payload, { intake: intake.payload, review: check.payload });
      if (!approved.ok) throw new Error('approved_spec_invalid');
      bindInput(input, stage === 'delivery' ? 'approvedSpec' : 'spec', approved);
    }
    if (stage === 'green') bindInput(input, 'red', readTdd(chain.parent, chain.artifact).evidence);
    if (stage === 'delivery') {
      if (chain.parent.head_commit !== request.head_commit) throw new Error('green_head_mismatch');
      bindInput(input, 'tdd', readTdd(chain.parent, chain.artifact));
    }
  } catch (error) { return failure(1, 'invalid_evidence_chain', [error.message]); }
  let result;
  let payload;
  if (stage === 'spec' || stage === 'spec-check') {
    if (!validatePayload.spec_payload(input.spec)) return failure(2, 'invalid_spec', structuredClone(validatePayload.spec_payload.errors));
    if (stage === 'spec-check' && !validatePayload.spec_check_payload(input.review)) return failure(2, 'invalid_review', structuredClone(validatePayload.spec_check_payload.errors));
    result = validateSpec(input.spec, { intake: input.intake, review: input.review, requireReview: stage === 'spec-check' });
    payload = stage === 'spec' ? input.spec : input.review;
  } else if (stage === 'red' || stage === 'green') {
    // Always replay the sealed proof. Author-supplied replay receipts are not used.
    result = runTddStage({ projectRoot, changeId: request.change_id, phase: stage, spec: input.spec,
      scenarioTests: input.scenario_tests, baseCommit: request.base_commit, sourceCommit: request.head_commit,
      redEvidence: input.red, diffRef: request.base_commit, attempt: 0 });
    if (!result.evidence?.proof || result.findings.some(item => item.severity === 'infra') || result.gates.some(gate => gate.rc === null)) return failure(3, 'tdd_infra', result.findings);
    if (result.evidence.proof.git_head !== request.head_commit) return failure(1, 'proof_head_mismatch');
    payload = result.payload;
  } else {
    if (!validatePayload.delivery_payload(input.review)) return failure(2, 'invalid_review', structuredClone(validatePayload.delivery_payload.errors));
    result = validateDelivery({ ...input, baseCommit: request.base_commit, headCommit: request.head_commit });
    payload = input.review;
  }
  const ok = result.ok === true || result.verdict === 'PASS';
  if (!ok) return failure(1, 'stage_rejected', result.issues || result.findings);
  const kind = stage === 'spec' ? 'spec' : (['red', 'green'].includes(stage) ? 'tdd' : 'review');
  const files = {};
  const addFile = (prefix, bytes) => {
    const entry = { name: `${prefix}-${digest(bytes).slice(7)}.json`, sha256: digest(bytes), media_type: 'application/json' };
    files[entry.name] = bytes;
    return entry;
  };
  // Preserve all received bytes, including the parent closure, before publishing.
  for (const bytes of inputs.values()) addFile('input', bytes);
  const source = seal({ schema_version: 1, kind: 'input', stage, change_id: request.change_id,
    attempt: request.attempt, base_commit: request.base_commit, policy_digest: request.policy_digest,
    parents: [chain.parent.evidence_id], artifacts: [addFile('input', inputs.get('stage.json'))] });
  addFile('evidence', jsonBytes(source));
  const artifacts = [addFile(kind, jsonBytes(payload))];
  if (kind === 'tdd') {
    const { phase, status, spec_digest, digest: tddDigest, evidence } = result;
    artifacts.push(addFile('tdd-handoff', jsonBytes({ phase, status, spec_digest, digest: tddDigest, payload, evidence })));
  }
  const envelope = seal({
    schema_version: 1, kind, role: request.role, stage, change_id: request.change_id,
    attempt: request.attempt, run_id: request.run_id, issue_ref: input.issue_ref,
    base_commit: request.base_commit, head_commit: request.head_commit,
    parents: [chain.parent.evidence_id, source.evidence_id], producer: input.producer,
    artifacts, payload,
  });
  if (!validateEnvelope(envelope)) return failure(2, 'invalid_envelope', structuredClone(validateEnvelope.errors || []));
  return { rc: 0, result: envelope, files };
}

function writeExclusive(target, bytes, allowIdentical = false) {
  mkdirSync(dirname(target), { recursive: true });
  const temp = `${target}.${randomUUID()}.tmp`;
  writeFileSync(temp, bytes, { flag: 'wx' });
  try { linkSync(temp, target); } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    if (!allowIdentical || !readFileSync(target).equals(bytes)) throw new Error('output_exists');
  } finally { unlinkSync(temp); }
}

export function storeRoleResult(outputPath, result, files = {}) {
  const target = resolve(outputPath);
  if (result.evidence_id) {
    const { evidence_id, ...body } = result;
    if (!validateEnvelope(result) || evidence_id !== `sha256:${hashPayload(body)}`) throw new Error('invalid_envelope');
    // Validate the complete closure before writing any of the success package.
    const inputs = new Map(Object.entries(files));
    inputs.set('parent.json', jsonBytes(result));
    readEvidence(inputs, { ...result, policy_digest: result.producer.policy_digest }, result.stage);
    for (const [name, bytes] of inputs) {
      if (name === 'parent.json') continue;
      if (!artifactFilename.test(name) || !Buffer.isBuffer(bytes)) throw new Error('invalid_output_artifact');
      writeExclusive(resolve(dirname(target), name), bytes, true);
    }
  }
  const bytes = jsonBytes(result);
  writeExclusive(target, bytes);
  return { path: target, digest: digest(bytes) };
}

export function readRoleRequest(path) { return JSON.parse(readFileSync(path, 'utf8')); }
