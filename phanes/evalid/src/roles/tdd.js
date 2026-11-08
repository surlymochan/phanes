/**
 * TDD stage reducer and proof adapter for the `evalid-tdd` role.
 *
 * The role never trusts a claim of a red/green run. It reads the proof
 * files the existing I6 atoms already write, replays them through the very
 * same atoms, and reduces the result into a stage verdict the controller can
 * act on. The atoms are reused as they are: this module never edits them,
 * it only calls them and interprets their receipts.
 *
 * Contract (handoff/EVALID-ROLES.md §2.2, §2.3, §3.2, §5 C3):
 *
 *   red   an assertion failure that replays, on a production tree identical
 *         to the frozen base, with every public scenario mapped to a test.
 *   green the same frozen tests turn green without a byte of test, fixture
 *         or runner configuration changing, inside the approved file scope.
 *
 * A stage is accepted only when every check below holds. Anything the role
 * cannot measure is a refusal, never a pass:
 *
 *   - the red failure must be an assertion failure (`failure_kind=assertion`),
 *     not an import/syntax error, a timeout, an empty run or a skip;
 *   - the run must report a readable, non-zero test count;
 *   - the sealed snapshot must be present so the proof can be replayed;
 *   - the red commit may only carry test scope; a production change in it
 *     means the red was recorded after the implementation existed;
 *   - the tests, fixtures and runner configuration frozen at red must be
 *     byte-identical at green;
 *   - every public scenario must map to a test that exists.
 *
 * The reducer itself is pure: it takes evidence objects and returns a
 * verdict. The adapter (`runTddStage`) turns a live working tree into that
 * evidence by running the existing atoms.
 */
import { hashPayload } from '../core/workflow-integrity.js';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { basename, extname, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = resolve(fileURLToPath(new URL('../..', import.meta.url)));

export const TDD_PHASES = Object.freeze(['red', 'green']);

export const TDD_STATES = Object.freeze({
  RED_ACCEPTED: 'RED_ACCEPTED',
  GREEN_ACCEPTED: 'GREEN_ACCEPTED',
  RED_REJECTED: 'RED_REJECTED',
  GREEN_REJECTED: 'GREEN_REJECTED',
  INFRA: 'INFRA',
});

/** The only failure class a red proof may carry (I6c rejects the rest). */
export const RED_FAILURE_KIND = 'assertion';

/** The only failure class a green proof may carry. */
export const GREEN_FAILURE_KIND = 'passed';

/** Existing atoms this role reuses. They are never modified here. */
export const TDD_ATOMS = Object.freeze({
  run: 'xflow/atoms/i6a_tdd_run.py',
  validate: 'xflow/atoms/i6b_tdd_proof_validate.py',
  quality: 'xflow/atoms/i6c_tdd_quality_review.py',
});

const SKIP_DIR_NAMES = new Set([
  '.git',
  'node_modules',
  '__pycache__',
  '.pytest_cache',
  '.venv',
  'venv',
  'coverage',
  'specs',
  'docs',
  '.xflow',
  '.as-xflow',
]);

const CODE_EXTENSIONS = new Set([
  '.js', '.jsx', '.mjs', '.cjs',
  '.ts', '.tsx',
  '.py', '.go', '.rs', '.java', '.kt', '.rb', '.php',
]);

const TEST_DIR_PATTERN = /(^|\/)(test|tests|spec|__tests__)\//;
const TEST_FILE_PATTERN = /(^|\/)[^\/]+(\.test|\.spec|_test)\.[^\/]+$/;
const FIXTURE_DIR_PATTERN = /(^|\/)(fixtures|__fixtures__|testdata|test-data|test_fixtures)\//;

const CONFIG_FILE_NAMES = new Set([
  'package.json',
  'package-lock.json',
  'npm-shrinkwrap.json',
  'yarn.lock',
  'pnpm-lock.yaml',
  'pnpm-workspace.yaml',
  'bun.lock',
  'bun.lockb',
  'deno.json',
  'deno.jsonc',
  'deno.lock',
  'tsconfig.json',
  'pytest.ini',
  'tox.ini',
  'setup.cfg',
  'setup.py',
  'pyproject.toml',
  'conftest.py',
  'pipfile',
  'poetry.lock',
  'uv.lock',
  'go.mod',
  'go.sum',
  'cargo.toml',
  'cargo.lock',
  'composer.json',
  'composer.lock',
  'gemfile',
  'gemfile.lock',
  'build.gradle',
  'pom.xml',
  'mix.exs',
  'pubspec.yaml',
  'package.swift',
  'makefile',
]);

const CONFIG_FILE_PATTERN = /^(?:requirements[^\/]*\.txt|tsconfig[^\/]*\.json|jest\.config\.[cm]?[jt]s|vitest\.(?:config|workspace)\.[cm]?[jt]s|playwright\.config\.[cm]?[jt]s|cypress\.config\.[cm]?[jt]s|karma\.conf\.[cm]?[jt]s|mocha\.opts|\.mocharc[^\/]*|babel\.config\.[cm]?[jt]s|\.babelrc[^\/]*|webpack\.config\.[cm]?[jt]s|rollup\.config\.[cm]?[jt]s)$/i;

/** Mirrors the assertion shapes the existing quality gate recognises. */
const ASSERTION_PATTERN = /(?:\bassert\.(?:ok|equal|strictEqual|deepEqual|deepStrictEqual|notEqual|notStrictEqual|match|doesNotMatch|throws|rejects)\b|\bassert\s+(?!from\b)\S|\bself\.assert[A-Z]\w*|\bpytest\.raises\b|\bexpect\s*\(|\bshould\.|\.should\b)/g;

const FULL_SUITE_PATTERN = /^(?:npm|pnpm|yarn)\s+test\b|^(?:python3?|py)\s+-m\s+(?:pytest|unittest)\b|^pytest\b|^node\s+--test\b/;

const NODE_GATE_LABELS = ['pass', 'fail', 'skipped', 'todo'];
const NODE_SIGNATURE_LABELS = ['tests', 'suites', 'cancelled'];

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function normalizeRel(rel) {
  if (typeof rel !== 'string') return '';
  return rel.replace(/\\/g, '/').replace(/^\.\//, '').replace(/^\/+/, '');
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function readJsonFile(path) {
  if (!existsSync(path)) return null;
  try {
    const raw = readFileSync(path, 'utf8');
    return raw.trim() ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

// --- path classification ----------------------------------------------------

export function isTestPath(rel) {
  const normalized = normalizeRel(rel);
  if (!normalized) return false;
  if (normalized.startsWith('docs/') || normalized.startsWith('specs/')) return false;
  if (normalized.startsWith('test/fixtures/') || normalized.startsWith('tests/fixtures/')) return false;
  if (!CODE_EXTENSIONS.has(extname(normalized))) return false;
  return TEST_DIR_PATTERN.test(normalized) || TEST_FILE_PATTERN.test(normalized);
}

export function isFixturePath(rel) {
  const normalized = normalizeRel(rel);
  if (!normalized) return false;
  if (normalized.startsWith('docs/') || normalized.startsWith('specs/')) return false;
  return FIXTURE_DIR_PATTERN.test(normalized);
}

export function isConfigPath(rel) {
  const normalized = normalizeRel(rel);
  if (!normalized) return false;
  const name = basename(normalized);
  if (CONFIG_FILE_NAMES.has(name.toLowerCase())) return true;
  return CONFIG_FILE_PATTERN.test(name);
}

/** A file whose bytes are production behaviour, not test material. */
export function isProductionPath(rel) {
  const normalized = normalizeRel(rel);
  if (!normalized) return false;
  if (!CODE_EXTENSIONS.has(extname(normalized))) return false;
  if (isTestPath(normalized) || isFixturePath(normalized) || isConfigPath(normalized)) return false;
  return true;
}

export function countAssertions(content) {
  if (typeof content !== 'string' || !content) return 0;
  return (content.match(ASSERTION_PATTERN) || []).length;
}

// --- runner output ----------------------------------------------------------

function summaryCount(text, label) {
  const match = new RegExp(`^[#\\u2139]\\s*${label}\\s+(\\d+)\\b`, 'm').exec(text || '');
  return match ? Number(match[1]) : null;
}

/**
 * Test counts of a `node --test` run, or null when the run carries no
 * readable summary. A summary that exists but cannot be read completely is
 * unknown, never zero.
 */
export function nodeRunCounts(text) {
  if (!text) return null;
  if (NODE_SIGNATURE_LABELS.some((label) => summaryCount(text, label) === null)) return null;
  const counts = {};
  for (const label of [...NODE_SIGNATURE_LABELS, ...NODE_GATE_LABELS]) {
    counts[label] = summaryCount(text, label);
  }
  return counts;
}

/** Counts of any supported runner, or null when the run reports none. */
export function testRunCounts(text) {
  const nodeCounts = nodeRunCounts(text);
  if (nodeCounts) return { runner: 'node', ...nodeCounts };
  const body = text || '';
  const ran = /^Ran (\d+) tests?\b/m.exec(body);
  if (ran) return { runner: 'unittest', tests: Number(ran[1]) };
  const passed = /^(\d+) passed\b/m.exec(body);
  const failed = /^(\d+) failed\b/m.exec(body);
  if (passed || failed) {
    return {
      runner: 'pytest',
      tests: (passed ? Number(passed[1]) : 0) + (failed ? Number(failed[1]) : 0),
    };
  }
  if (/no tests ran/i.test(body)) return { runner: 'pytest', tests: 0 };
  return null;
}

/** True when the run reported skips or todos instead of a real result. */
export function outputIsNonResult(text) {
  const counts = testRunCounts(text);
  if (!counts) return false;
  if (counts.runner === 'node') {
    const { skipped, todo, pass, fail } = counts;
    if ([skipped, todo, pass, fail].some((value) => value === null)) return false;
    return skipped + todo > 0 && pass === 0 && fail === 0;
  }
  return false;
}

/** True when a summary is present but a gate count cannot be read. */
export function outputCountsUnknown(text) {
  const counts = nodeRunCounts(text);
  if (!counts) return false;
  return NODE_GATE_LABELS.some((label) => counts[label] === null);
}

// --- command shape ----------------------------------------------------------

function commandTokens(command) {
  if (typeof command !== 'string' || !command.trim()) return [];
  return command.trim().split(/\s+/);
}

function isTestToken(token) {
  if (!token || token.startsWith('-')) return false;
  const normalized = token.replace(/\\/g, '/');
  const name = normalized.split('/').pop();
  if (/\.(test|spec)\.[cm]?[jt]sx?$/.test(name)) return true;
  if (/(^|\/)(test|tests|__tests__)\/.+\.(py|js|mjs|cjs|ts|tsx)$/.test(normalized)) return true;
  if (name.startsWith('test_') && name.endsWith('.py')) return true;
  if (name.endsWith('_test.py')) return true;
  return false;
}

/** Test files a command names, in the order they appear. */
export function commandTestPaths(command) {
  return commandTokens(command).filter((token) => isTestToken(token));
}

/** True when a command runs the whole suite rather than named tests. */
export function isFullSuiteCommand(command) {
  if (commandTestPaths(command).length) return false;
  return FULL_SUITE_PATTERN.test((command || '').trim());
}

/**
 * None when green runs the red test or a superset of it, otherwise the
 * reason the two runs are not comparable.
 */
export function commandProblem(redCommand, greenCommand) {
  if (!redCommand || !greenCommand) return null;
  if (redCommand.trim().replace(/\s+/g, ' ') === greenCommand.trim().replace(/\s+/g, ' ')) return null;
  // Only named-file expansion is provably a superset. Keep runner flags and
  // selectors identical; unknown selector syntax must fail closed.
  const options = (command) => commandTokens(command).filter(token => !isTestToken(token)).join(' ');
  const redPaths = commandTestPaths(redCommand);
  const greenPaths = commandTestPaths(greenCommand);
  if (!redPaths.length && !isFullSuiteCommand(redCommand)) return 'red_command_not_a_test';
  if (options(redCommand) !== options(greenCommand)) return 'red_green_command_mismatch';
  if (isFullSuiteCommand(greenCommand) && !greenPaths.length) return null;
  if (redPaths.length && redPaths.every((path) => greenPaths.includes(path))) return null;
  if (isFullSuiteCommand(redCommand) && isFullSuiteCommand(greenCommand) && !greenPaths.length) return null;
  return 'red_green_command_mismatch';
}

// --- evidence readers -------------------------------------------------------

export function proofPath({ projectRoot, changeId, phase, attempt = 0 }) {
  return resolve(projectRoot, 'specs', 'changes', changeId, 'tdd', `${phase}-${attempt}.json`);
}

export function readProof({ projectRoot, changeId, phase, attempt = 0 }) {
  return readJsonFile(proofPath({ projectRoot, changeId, phase, attempt }));
}

export function readQualityProof({ projectRoot, changeId }) {
  return readJsonFile(resolve(projectRoot, 'specs', 'changes', changeId, 'tdd', 'quality-0.json'));
}

export function snapshotDir({ projectRoot, changeId, phase, attempt = 0 }) {
  return resolve(projectRoot, 'specs', 'changes', changeId, 'tdd', 'snapshots', `${phase}-${attempt}`);
}

function listFiles(root, prefix = '') {
  let entries = [];
  try {
    entries = readdirSync(root, { withFileTypes: true });
  } catch {
    return [];
  }
  const files = [];
  for (const entry of entries) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) {
      files.push(...listFiles(resolve(root, entry.name), rel));
      continue;
    }
    if (entry.isFile()) files.push(rel);
  }
  return files.sort();
}

/** The sealed snapshot of a phase: present only when it can be replayed. */
export function readSnapshot({ projectRoot, changeId, phase, attempt = 0 }) {
  const dir = snapshotDir({ projectRoot, changeId, phase, attempt });
  if (!existsSync(dir)) return { present: false, files: [] };
  const files = listFiles(dir);
  return { present: files.length > 0, files };
}

/** A content address of a file on disk, or null when it cannot be read. */
export function fileDigest(path) {
  if (!existsSync(path)) return null;
  try {
    return sha256(readFileSync(path));
  } catch {
    return null;
  }
}

/**
 * The frozen test surface of a change: every visible test, the fixtures
 * they read and the runner configuration (including the dependency lock).
 * The manifest is what red freezes and green must reproduce byte for byte.
 */
export function buildTestManifest({ projectRoot, changeId = null } = {}) {
  const root = resolve(projectRoot);
  const manifest = {};
  const assertions = {};
  const testFiles = [];
  const fixtureFiles = [];
  const configFiles = [];

  const walk = (dir) => {
    let entries = [];
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const full = resolve(dir, entry.name);
      if (entry.isDirectory()) {
        if (SKIP_DIR_NAMES.has(entry.name)) continue;
        walk(full);
        continue;
      }
      if (!entry.isFile()) continue;
      const rel = relative(root, full).split('\\').join('/');
      if (isTestPath(rel)) {
        testFiles.push(rel);
        manifest[rel] = fileDigest(full);
        assertions[rel] = countAssertions(readFileSync(full, 'utf8'));
        continue;
      }
      if (isFixturePath(rel)) {
        fixtureFiles.push(rel);
        manifest[rel] = fileDigest(full);
        continue;
      }
      if (isConfigPath(rel)) {
        configFiles.push(rel);
        manifest[rel] = fileDigest(full);
      }
    }
  };
  walk(root);

  return {
    manifest: Object.fromEntries(Object.entries(manifest).sort(([a], [b]) => a.localeCompare(b))),
    assertions,
    test_files: testFiles.sort(),
    fixture_files: fixtureFiles.sort(),
    config_files: configFiles.sort(),
  };
}

// --- git helpers ------------------------------------------------------------

function git(projectRoot, args) {
  return spawnSync('git', args, { cwd: projectRoot, encoding: 'utf8', timeout: 30000 });
}

/** Paths that differ between two commits. */
export function readCommitDiff({ projectRoot, base, head }) {
  if (!base || !head) return { ok: false, error: 'missing_commit', changed_paths: [] };
  const result = git(projectRoot, ['diff', '--name-only', `${base}..${head}`]);
  if (result.status !== 0) {
    return { ok: false, error: (result.stderr || result.stdout || 'git diff failed').trim().slice(0, 200), changed_paths: [] };
  }
  const changed_paths = (result.stdout || '').split('\n').map((line) => line.trim()).filter(Boolean);
  return { ok: true, changed_paths };
}

/** The ordered commit list of base..head. */
export function readCommitRange({ projectRoot, base, head }) {
  if (!base || !head) return { ok: false, error: 'missing_commit', commits: [] };
  const result = git(projectRoot, ['rev-list', '--reverse', `${base}..${head}`]);
  if (result.status !== 0) {
    return { ok: false, error: (result.stderr || result.stdout || 'git rev-list failed').trim().slice(0, 200), commits: [] };
  }
  const commits = (result.stdout || '').split('\n').map((line) => line.trim()).filter(Boolean);
  return { ok: true, commits };
}

/** True when `ancestor` is contained in `descendant`. */
export function isAncestorCommit({ projectRoot, ancestor, descendant }) {
  if (!ancestor || !descendant) return false;
  const result = git(projectRoot, ['merge-base', '--is-ancestor', ancestor, descendant]);
  return result.status === 0;
}

// --- atom adapters ----------------------------------------------------------

function formatCommand(script, args) {
  return ['python3', script, ...args].join(' ');
}

function parseAtomJson(stdout) {
  const lines = (stdout || '').split('\n').map((line) => line.trim()).filter(Boolean);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      return JSON.parse(lines[index]);
    } catch {
      continue;
    }
  }
  return null;
}

function runAtom({ projectRoot, changeId, script, args = [], timeout = 600000 }) {
  const scriptPath = resolve(REPO_ROOT, script);
  if (!existsSync(scriptPath)) {
    return { ok: false, rc: null, parsed: null, error: 'atom_script_missing', command: formatCommand(script, args), argv: ['python3', script, ...args] };
  }
  const result = spawnSync('python3', [scriptPath, '--project-root', projectRoot, '--change-id', changeId, ...args], {
    cwd: projectRoot,
    encoding: 'utf8',
    timeout,
  });
  const stdout = result.stdout || '';
  return {
    ok: result.status === 0,
    rc: result.status === null ? null : result.status,
    parsed: parseAtomJson(stdout),
    stderr: (result.stderr || '').slice(0, 2000),
    command: formatCommand(script, args),
    argv: ['python3', script, ...args],
  };
}

/** Run `I6a.tdd.run` for a phase. */
export function runProofAtom({ projectRoot, changeId, phase, testCommand, attempt = 0 }) {
  return runAtom({
    projectRoot,
    changeId,
    script: TDD_ATOMS.run,
    args: ['--phase', phase, '--attempt', String(attempt), '--test-command', testCommand],
  });
}

/** Run `I6b.tdd.proof_validate` for a phase: the independent replay. */
export function validateProofAtom({ projectRoot, changeId, phase, tddMode = 'red-green', attempt = 0 }) {
  return runAtom({
    projectRoot,
    changeId,
    script: TDD_ATOMS.validate,
    args: ['--phase', phase, '--attempt', String(attempt), '--tdd-mode', tddMode],
  });
}

/** Run `I6c.tdd.quality_review` for the change. */
export function reviewQualityAtom({ projectRoot, changeId, diffRef, tddMode = 'red-green' }) {
  const args = ['--diff-ref', diffRef];
  if (tddMode) args.push('--tdd-mode', tddMode);
  return runAtom({ projectRoot, changeId, script: TDD_ATOMS.quality, args });
}

// --- scope ------------------------------------------------------------------

function matchesScope(path, pattern) {
  if (!pattern) return false;
  if (pattern === path) return true;
  if (!pattern.includes('*')) return false;
  const escaped = pattern.split('*').map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('[^\\/]*');
  return new RegExp(`^${escaped}$`).test(path);
}

function isEvidencePath(path, changeId) {
  const normalized = normalizeRel(path);
  if (!changeId) return false;
  return normalized.startsWith(`specs/changes/${changeId}/`);
}

function scopeViolations(paths, scope = {}) {
  const allowed = Array.isArray(scope.allowed_files) ? scope.allowed_files : [];
  const excluded = Array.isArray(scope.excluded_files) ? scope.excluded_files : [];
  const violations = [];
  for (const path of paths) {
    if (excluded.some((pattern) => matchesScope(path, pattern))) {
      violations.push({ path, reason: 'excluded_file' });
      continue;
    }
    if (allowed.length && !allowed.some((pattern) => matchesScope(path, pattern))) {
      violations.push({ path, reason: 'outside_allowed_scope' });
    }
  }
  return violations;
}

// --- reducer ----------------------------------------------------------------

function finding(code, message, extra = {}) {
  return { severity: 'fail', code, message, ...extra };
}

function infraFinding(code, message, extra = {}) {
  return { severity: 'infra', code, message, ...extra };
}

function warnFinding(code, message, extra = {}) {
  return { severity: 'warn', code, message, ...extra };
}

function proofText(proof) {
  return `${proof?.stdout_tail || ''}\n${proof?.stderr_tail || ''}`;
}

function countAssertionsInManifest(manifest, assertions) {
  if (!isPlainObject(assertions)) return null;
  let total = 0;
  for (const path of Object.keys(manifest || {})) {
    if (!isTestPath(path)) continue;
    const count = Number(assertions[path] || 0);
    if (!Number.isFinite(count)) return null;
    total += count;
  }
  return total;
}

function checkScenarioMapping({ spec, scenarioTests, manifest }) {
  const findings = [];
  const scenarios = Array.isArray(spec?.scenarios) ? spec.scenarios : [];
  if (!scenarios.length) {
    findings.push(finding('spec_missing', 'The stage has no public scenario to map.'));
    return findings;
  }
  const mapping = isPlainObject(scenarioTests) ? scenarioTests : {};
  const known = new Set(scenarios.map((scenario) => scenario?.id).filter(Boolean));
  for (const scenario of scenarios) {
    const id = scenario?.id;
    const targets = Array.isArray(mapping[id]) ? mapping[id] : [mapping[id]];
    if (!targets.length || targets.some(target => typeof target !== 'string' || !target.trim())) {
      findings.push(finding('scenario_unmapped', `Scenario ${id || '(unnamed)'} has no test mapping.`, { scenario_id: id || null }));
      continue;
    }
    for (const target of targets) {
      const path = target.split('#')[0].trim();
      if (!path) continue;
      if ((path.includes('/') || /\.(test|spec)\./.test(path)) && manifest && !(path in manifest)) {
        findings.push(finding('scenario_test_missing', `Scenario ${id} maps to ${path}, which is not in the test manifest.`, { scenario_id: id, path }));
      }
    }
  }
  for (const id of Object.keys(mapping)) {
    if (!known.has(id)) {
      findings.push(warnFinding('scenario_unknown', `Scenario ${id} is mapped but is not part of the spec.`, { scenario_id: id }));
    }
  }
  return findings;
}

function checkManifest({ manifest, assertions, phase }) {
  const findings = [];
  if (!isPlainObject(manifest) || !Object.keys(manifest).length) {
    findings.push(finding('manifest_missing', `The ${phase} stage carries no test manifest.`));
    return findings;
  }
  const testPaths = Object.keys(manifest).filter((path) => isTestPath(path));
  if (!testPaths.length) {
    findings.push(finding('tests_missing', 'The test manifest holds no visible test.'));
  }
  const assertionTotal = countAssertionsInManifest(manifest, assertions);
  if (assertionTotal !== null && assertionTotal === 0) {
    findings.push(finding('no_assertions', 'The manifested tests hold no assertion.'));
  }
  return findings;
}

/**
 * A sealed test must still carry the bytes the manifest froze. A test that
 * changed between the proof and the manifest is a test that moved after the
 * evidence was taken.
 */
export function checkManifestDrift({ manifest, proof } = {}) {
  const findings = [];
  const frozen = isPlainObject(manifest) ? manifest : {};
  const sealed = isPlainObject(proof?.file_hashes) ? proof.file_hashes : {};
  for (const path of Object.keys(sealed)) {
    if (!isTestPath(path)) continue;
    if (!(path in frozen)) {
      findings.push(finding('test_manifest_missing_file', `The sealed test ${path} is not in the frozen manifest.`, { path }));
      continue;
    }
    if (frozen[path] !== sealed[path]) {
      findings.push(finding('test_manifest_drift', `The sealed test ${path} changed between the proof and the manifest.`, { path }));
    }
  }
  return findings;
}

function checkRunCounts({ proof, phase }) {
  const findings = [];
  const text = proofText(proof);
  const counts = testRunCounts(text);
  if (!counts) {
    findings.push(finding('tests_counts_unknown', `The ${phase} run reports no readable test count.`, { phase }));
    return findings;
  }
  if (outputCountsUnknown(text)) {
    findings.push(finding('tests_counts_unknown', `The ${phase} run summary cannot be read completely.`, { phase }));
    return findings;
  }
  if (outputIsNonResult(text)) {
    findings.push(finding('tests_skipped', `The ${phase} run skipped or todoed the tests it claims to have executed.`, { phase }));
    return findings;
  }
  if (counts.tests === 0) {
    findings.push(finding('tests_empty', `The ${phase} run executed no test.`, { phase }));
  }
  return findings;
}

function checkRed(evidence = {}, context = {}) {
  const findings = [];
  const proof = isPlainObject(evidence.proof) ? evidence.proof : null;
  if (!proof) {
    findings.push(finding('red_proof_missing', 'The red stage carries no proof.'));
    return findings;
  }
  if (proof.phase !== 'red') {
    findings.push(finding('red_phase_mismatch', `The proof is a ${proof.phase || 'unknown'} proof, not a red proof.`, { phase: proof.phase || null }));
  }
  if (proof.passed !== false || proof.exit_code === 0) {
    findings.push(finding('red_not_failing', 'The red proof does not record a failing run.'));
  }
  if (proof.expectation_met === false) {
    findings.push(finding('red_expectation_not_met', 'The red proof records that the raw result did not meet the phase expectation.'));
  }
  if (proof.failure_kind !== RED_FAILURE_KIND) {
    findings.push(finding('red_failure_not_assertion', `The red failure is ${proof.failure_kind || 'unknown'}, not an assertion failure.`, { failure_kind: proof.failure_kind || null }));
  }
  if (proof.failure_kind === 'skipped') {
    findings.push(finding('tests_skipped', 'The red run skipped or todoed the tests it claims to have executed.'));
  }
  if (proof.failure_kind === 'counts_unknown') {
    findings.push(finding('tests_counts_unknown', 'The red run summary counts could not be fully parsed.'));
  }
  findings.push(...checkRunCounts({ proof, phase: 'red' }));

  const snapshot = evidence.snapshot || {};
  if (!snapshot.present) {
    findings.push(infraFinding('snapshot_missing', 'The red proof has no sealed snapshot, so it cannot be replayed.'));
  }

  const receipt = evidence.receipt;
  if (!isPlainObject(receipt) || receipt.ok !== true) {
    findings.push(finding('red_replay_unverified', 'The red proof has no independent replay receipt.'));
  }

  const changedPaths = (Array.isArray(evidence.changed_paths) ? evidence.changed_paths : [])
    .filter((path) => !isEvidencePath(path, context.changeId));
  const productionPaths = changedPaths.filter((path) => isProductionPath(path));
  if (productionPaths.length) {
    findings.push(finding('red_production_changed', 'The red commit already carries production changes, so the red was not recorded before the implementation.', { paths: productionPaths }));
  }
  const testScopePaths = changedPaths.filter((path) => !isProductionPath(path));
  if (changedPaths.length && !testScopePaths.length) {
    findings.push(finding('red_no_test_change', 'The red commit carries no test scope change.'));
  }
  const sealedProduction = Object.keys(isPlainObject(proof.file_hashes) ? proof.file_hashes : {}).filter((path) => isProductionPath(path));
  if (sealedProduction.length) {
    findings.push(finding('red_uncommitted_production', 'The red run sealed production files that are not part of the base.', { paths: sealedProduction }));
  }

  const violations = scopeViolations(changedPaths, context.spec?.scope);
  for (const violation of violations) {
    findings.push(finding('red_scope_violation', `The red commit changes ${violation.path}, which is outside the approved scope.`, { path: violation.path, reason: violation.reason }));
  }
  return findings;
}

function checkGreen(evidence = {}, redEvidence = {}, context = {}) {
  const findings = [];
  const proof = isPlainObject(evidence.proof) ? evidence.proof : null;
  if (!proof) {
    findings.push(finding('green_proof_missing', 'The green stage carries no proof.'));
    return findings;
  }
  if (proof.phase !== 'green') {
    findings.push(finding('green_phase_mismatch', `The proof is a ${proof.phase || 'unknown'} proof, not a green proof.`, { phase: proof.phase || null }));
  }
  if (proof.passed !== true || proof.exit_code !== 0) {
    findings.push(finding('green_not_passing', 'The green proof does not record a passing run.'));
  }
  if (proof.expectation_met === false) {
    findings.push(finding('green_expectation_not_met', 'The green proof records that the raw result did not meet the phase expectation.'));
  }
  if (proof.failure_kind !== GREEN_FAILURE_KIND) {
    findings.push(finding('green_failure_not_pass', `The green run is classified ${proof.failure_kind || 'unknown'}, not a clean pass.`, { failure_kind: proof.failure_kind || null }));
  }
  findings.push(...checkRunCounts({ proof, phase: 'green' }));

  const snapshot = evidence.snapshot || {};
  if (!snapshot.present) {
    findings.push(infraFinding('snapshot_missing', 'The green proof has no sealed snapshot, so it cannot be replayed.'));
  }

  if (evidence.receipt?.ok !== true) findings.push(finding('green_replay_unverified', 'Green requires a successful independent replay.'));

  const problem = commandProblem(redEvidence?.proof?.command, proof.command);
  if (problem) {
    findings.push(finding('green_command_mismatch', 'The green run does not cover the test the red run froze.', { reason: problem }));
  }

  const redManifest = isPlainObject(redEvidence.manifest) ? redEvidence.manifest : {};
  const greenManifest = isPlainObject(evidence.manifest) ? evidence.manifest : {};
  for (const path of Object.keys(redManifest)) {
    const kind = manifestKind(path);
    if (!(path in greenManifest)) {
      findings.push(finding(manifestChangeCode(kind, 'removed'), `The frozen file ${path} is gone at green.`, { path, kind }));
      continue;
    }
    if (greenManifest[path] !== redManifest[path]) {
      findings.push(finding(manifestChangeCode(kind, 'changed'), `The frozen file ${path} changed between red and green.`, { path, kind }));
    }
  }
  for (const path of Object.keys(greenManifest)) {
    if (!(path in redManifest)) {
      const kind = manifestKind(path);
      findings.push(finding(manifestChangeCode(kind, 'added'), `The file ${path} was added after the red authorization; a new red authorization is required.`, { path, kind }));
    }
  }

  const changedPaths = (Array.isArray(evidence.changed_paths) ? evidence.changed_paths : [])
    .filter((path) => !isEvidencePath(path, context.changeId));
  const violations = scopeViolations(changedPaths, context.spec?.scope);
  for (const violation of violations) {
    findings.push(finding('green_scope_violation', `The green commit changes ${violation.path}, which is outside the approved scope.`, { path: violation.path, reason: violation.reason }));
  }
  return findings;
}

function manifestKind(path) {
  if (isTestPath(path)) return 'test';
  if (isFixturePath(path)) return 'fixture';
  if (isConfigPath(path)) return 'config';
  return 'other';
}

function manifestChangeCode(kind, change) {
  const prefix = kind === 'test'
    ? 'green_test'
    : (kind === 'fixture' ? 'green_fixture' : (kind === 'config' ? 'green_config' : 'green_manifest'));
  return `${prefix}_${change}`;
}

function buildPayload({ phase, spec, sourceCommit, manifest, proofRefs, scenarioTests, gates }) {
  return {
    spec_digest: spec?.spec_digest || null,
    phase,
    source_commit: sourceCommit || null,
    test_manifest: Object.fromEntries(Object.entries(manifest || {}).map(([path, hash]) => [path, hash.startsWith('sha256:') ? hash : `sha256:${hash}`])),
    proof_refs: (proofRefs || []).map(ref => ref.artifact_ref ? ref : ({ kind: ref.name.startsWith('snapshots/') ? 'snapshot' : 'proof', phase, artifact_ref: `sha256:${ref.sha256}`, path: ref.name })),
    scenario_tests: Object.fromEntries(Object.entries(scenarioTests || {}).map(([id, targets]) => [id, Array.isArray(targets) ? targets : [targets]])),
    gate_results: (gates || []).map(gate => ({ command: gate.command, exit_code: gate.rc, report_digest: gate.report_digest?.startsWith('sha256:') ? gate.report_digest : `sha256:${gate.report_digest || hashPayload(gate)}` })),
  };
}

/**
 * Reduce stage evidence into a verdict.
 *
 * `input.red` and `input.green` are evidence bundles:
 *   { proof, manifest, assertions, receipt, changed_paths, snapshot, source_commit }
 */
export function reduceTddStage(input = {}) {
  const phase = input.phase;
  if (!TDD_PHASES.includes(phase)) {
    return {
      ok: false,
      phase: phase || null,
      state: TDD_STATES.RED_REJECTED,
      verdict: 'rejected',
      findings: [finding('invalid_phase', `phase must be one of ${TDD_PHASES.join(', ')}.`)],
      payload: null,
    };
  }

  const context = { spec: input.spec?.approved ? { ...input.spec.spec, spec_digest: input.spec.digest } : (input.spec || {}), changeId: input.change_id || null };
  const findings = [];
  const redEvidence = isPlainObject(input.red) ? input.red : {};
  const greenEvidence = isPlainObject(input.green) ? input.green : {};

  if (!input.change_id) {
    findings.push(finding('change_id_missing', 'The stage carries no change id.'));
  }

  findings.push(...checkScenarioMapping({
    spec: context.spec,
    scenarioTests: input.scenario_tests,
    manifest: phase === 'green' ? greenEvidence.manifest : redEvidence.manifest,
  }));

  const activeEvidence = phase === 'green' ? greenEvidence : redEvidence;
  findings.push(...checkManifest({
    manifest: activeEvidence.manifest,
    assertions: activeEvidence.assertions,
    phase,
  }));

  if (phase === 'green') {
    const redFindings = checkRed(redEvidence, context);
    if (redFindings.some((item) => item.severity !== 'warn')) {
      findings.push(finding('green_red_not_accepted', 'The green stage has no accepted red stage to build on.'));
      findings.push(...redFindings);
    }
    findings.push(...checkGreen(greenEvidence, redEvidence, context));
    for (const id of ['I6b.tdd.proof_validate', 'I6c.tdd.quality_review']) {
      const gate = input.gates?.find(gate => gate.id === id);
      if (!gate || gate.ok !== true || gate.rc !== 0) findings.push(finding('green_gate_failed', `Green requires successful ${id}.`));
    }
    if (input.contains_red !== true) {
      findings.push(finding('green_missing_red_commit', 'The green commit does not contain the red commit.'));
    }
  } else {
    findings.push(...checkRed(redEvidence, context));
  }

  const failed = findings.some((item) => item.severity === 'fail');
  const infra = findings.some((item) => item.severity === 'infra');
  const state = failed
    ? (phase === 'red' ? TDD_STATES.RED_REJECTED : TDD_STATES.GREEN_REJECTED)
    : (infra ? TDD_STATES.INFRA : (phase === 'red' ? TDD_STATES.RED_ACCEPTED : TDD_STATES.GREEN_ACCEPTED));

  const payload = buildPayload({
    phase,
    spec: context.spec,
    sourceCommit: activeEvidence.source_commit || input.source_commit || null,
    manifest: activeEvidence.manifest,
    proofRefs: input.proof_refs,
    scenarioTests: input.scenario_tests,
    gates: input.gates,
  });

  return {
    ok: !failed && !infra,
    phase,
    state,
    verdict: failed ? 'rejected' : (infra ? 'infra' : 'accepted'),
    findings,
    status: !failed && !infra ? 'PASS' : 'FAIL',
    spec_digest: payload.spec_digest,
    digest: `sha256:${hashPayload(payload)}`,
    payload,
  };
}

// --- adapter ----------------------------------------------------------------

function proofRefsFor({ projectRoot, changeId, phase, attempt = 0 }) {
  const refs = [];
  const proof = proofPath({ projectRoot, changeId, phase, attempt });
  const digest = fileDigest(proof);
  if (digest) {
    refs.push({ name: `${phase}-${attempt}.json`, sha256: digest, media_type: 'application/json' });
  }
  const snapshot = snapshotDir({ projectRoot, changeId, phase, attempt });
  const files = listFiles(snapshot);
  if (files.length) {
    refs.push({
      name: `snapshots/${phase}-${attempt}`,
      sha256: sha256(files.map((file) => `${file}:${fileDigest(resolve(snapshot, file))}`).join('\n')),
      media_type: 'application/x-evalid-snapshot',
    });
  }
  return refs;
}

function gateResult(atom, result, reportDigest = null) {
  return {
    id: atom,
    command: result.command,
    rc: result.rc === null ? null : result.rc,
    report_digest: reportDigest,
    ok: result.ok === true,
  };
}

/**
 * Run a TDD stage end to end: the existing atoms produce the proof and the
 * replay, this module reduces them into a stage verdict.
 *
 * The red stage returns the evidence bundle the controller has to freeze
 * before it may authorize green. The stage payload is returned, not written:
 * the artifact sink that owns the output area belongs to the CLI layer.
 */
export function runTddStage(options = {}) {
  const {
    projectRoot,
    changeId,
    phase,
    attempt = 0,
    testCommand = null,
    tddMode = 'red-green',
    diffRef = null,
    spec = {},
    scenarioTests = {},
    baseCommit = null,
    sourceCommit = null,
    redEvidence = null,
  } = options;

  if (!projectRoot || !changeId || !TDD_PHASES.includes(phase)) {
    return {
      ok: false,
      phase: phase || null,
      state: TDD_STATES.RED_REJECTED,
      verdict: 'rejected',
      findings: [finding('invalid_stage', 'A stage needs a project root, a change id and a known phase.')],
      payload: null,
      gates: [],
    };
  }

  const gates = [];
  let runResult = null;
  if (testCommand) {
    runResult = runProofAtom({ projectRoot, changeId, phase, testCommand, attempt });
    gates.push(gateResult('I6a.tdd.run', runResult, fileDigest(proofPath({ projectRoot, changeId, phase, attempt }))));
  }

  const validateResult = validateProofAtom({ projectRoot, changeId, phase, tddMode, attempt });
  gates.push(gateResult('I6b.tdd.proof_validate', validateResult));

  let qualityResult = null;
  if (phase === 'green') {
    if (diffRef) {
      qualityResult = reviewQualityAtom({ projectRoot, changeId, diffRef, tddMode });
      gates.push(gateResult('I6c.tdd.quality_review', qualityResult, fileDigest(resolve(projectRoot, 'specs', 'changes', changeId, 'tdd', 'quality-0.json'))));
    }
  }

  const proof = readProof({ projectRoot, changeId, phase, attempt });
  const snapshot = readSnapshot({ projectRoot, changeId, phase, attempt });
  const built = buildTestManifest({ projectRoot, changeId });
  const source = sourceCommit || proof?.git_head || null;
  const diff = baseCommit && source
    ? readCommitDiff({ projectRoot, base: baseCommit, head: source })
    : { ok: false, error: 'missing_commit', changed_paths: [] };

  const evidence = {
    proof,
    manifest: built.manifest,
    assertions: built.assertions,
    receipt: validateResult.parsed
      ? { ok: validateResult.ok === true, verdict: validateResult.parsed.verdict || null, source: 'I6b.tdd.proof_validate' }
      : { ok: false, verdict: null, source: 'I6b.tdd.proof_validate' },
    changed_paths: diff.ok ? diff.changed_paths : [],
    snapshot,
    source_commit: source,
  };

  const findings = [];
  if (baseCommit && source && !diff.ok) {
    findings.push(infraFinding('commit_diff_unavailable', 'The commit range of this stage could not be read.'));
  }
  if (runResult && runResult.ok !== true && !proof) {
    findings.push(finding('proof_not_produced', 'The run atom produced no proof for this stage.'));
  }
  if (phase === 'green' && !diffRef) {
    findings.push(finding('quality_gate_missing', 'The green stage has no quality gate to pass.'));
  }
  findings.push(...checkManifestDrift({ manifest: evidence.manifest, proof }));
  if (phase === 'green' && redEvidence?.proof) {
    findings.push(...checkManifestDrift({ manifest: redEvidence.manifest, proof: redEvidence.proof }));
  }

  let containsRed;
  if (phase === 'green' && redEvidence?.source_commit && source) {
    containsRed = isAncestorCommit({ projectRoot, ancestor: redEvidence.source_commit, descendant: source });
  }

  const reduction = reduceTddStage({
    phase,
    change_id: changeId,
    attempt,
    spec,
    scenario_tests: scenarioTests,
    red: phase === 'green' ? (redEvidence || {}) : evidence,
    green: phase === 'green' ? evidence : undefined,
    contains_red: containsRed,
    gates,
    proof_refs: proofRefsFor({ projectRoot, changeId, phase, attempt }),
    source_commit: source,
  });

  const merged = [...findings, ...reduction.findings];
  const failed = merged.some((item) => item.severity === 'fail');
  const infra = merged.some((item) => item.severity === 'infra');
  const state = failed
    ? (phase === 'red' ? TDD_STATES.RED_REJECTED : TDD_STATES.GREEN_REJECTED)
    : (infra ? TDD_STATES.INFRA : (phase === 'red' ? TDD_STATES.RED_ACCEPTED : TDD_STATES.GREEN_ACCEPTED));

  return {
    ok: !failed && !infra,
    phase,
    state,
    verdict: failed ? 'rejected' : (infra ? 'infra' : 'accepted'),
    findings: merged,
    gates,
    status: !failed && !infra ? 'PASS' : 'FAIL',
    spec_digest: reduction.payload.spec_digest,
    digest: reduction.digest,
    payload: reduction.payload,
    evidence,
    quality: qualityResult?.parsed || null,
  };
}
