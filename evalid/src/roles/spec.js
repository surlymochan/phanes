import { hashPayload } from '../core/workflow-integrity.js';

const fail = (issues) => ({ ok: false, issues });

/** Validate a public, executable Spec against its frozen intake and review receipt. */
export function validateSpec(spec, { intake = null, review = null, specDigest = null, requireReview = true } = {}) {
  const issues = [];
  if (!spec || typeof spec !== 'object' || Array.isArray(spec)) return fail(['spec_required']);
  if (!Number.isInteger(spec.spec_version) || spec.spec_version < 1) issues.push('invalid_spec_version');
  if (!spec.scope || !Array.isArray(spec.scope.allowed_files) || !Array.isArray(spec.scope.excluded_files)) issues.push('invalid_scope');
  if (!Array.isArray(spec.scenarios) || spec.scenarios.length === 0) issues.push('scenarios_required');
  if (!Array.isArray(spec.commands) || spec.commands.length === 0) issues.push('commands_required');

  const commandIds = new Set((spec.commands || []).map((command) => command?.id).filter(Boolean));
  const scenarioIds = new Set();
  for (const scenario of spec.scenarios || []) {
    if (!scenario?.id || scenarioIds.has(scenario.id)) issues.push('scenario_id_missing_or_duplicate');
    scenarioIds.add(scenario?.id);
    if (!scenario?.requirement_ref) issues.push(`scenario_requirement_missing:${scenario?.id || 'unknown'}`);
    if (!scenario?.given?.trim() || !scenario?.when?.trim() || !Array.isArray(scenario?.then) || scenario.then.length === 0) issues.push(`scenario_not_executable:${scenario?.id || 'unknown'}`);
    if (!commandIds.has(scenario?.command_id)) issues.push(`scenario_command_missing:${scenario?.id || 'unknown'}`);
  }
  const ids = new Set();
  for (const command of spec.commands || []) {
    if (!command?.id || ids.has(command.id)) issues.push('command_id_missing_or_duplicate');
    ids.add(command?.id);
    if (!Array.isArray(command?.argv) || command.argv.length === 0 || command.argv.some((part) => typeof part !== 'string')) issues.push(`invalid_command_argv:${command?.id || 'unknown'}`);
    if (typeof command?.cwd !== 'string' || command.cwd.startsWith('/') || command.cwd.split(/[\\/]/).includes('..')) issues.push(`invalid_command_cwd:${command?.id || 'unknown'}`);
    if (!Number.isInteger(command?.expected_exit) || !Number.isInteger(command?.timeout_s) || command.timeout_s < 1 || typeof command?.environment_digest !== 'string') issues.push(`invalid_command_contract:${command?.id || 'unknown'}`);
  }
  if (intake && Array.isArray(intake.requirements)) {
    const requirements = new Set(intake.requirements.map((item) => typeof item === 'string' ? item : item.id));
    for (const scenario of spec.scenarios || []) if (scenario?.requirement_ref && !requirements.has(scenario.requirement_ref)) issues.push(`unknown_requirement:${scenario.requirement_ref}`);
  }
  if (specDigest && specDigest !== `sha256:${hashPayload(spec)}`) issues.push('spec_digest_mismatch');
  if (requireReview && (review?.verdict !== 'PASS' || review?.subject !== (specDigest || `sha256:${hashPayload(spec)}`) || (review?.findings || []).some((finding) => finding.severity === 'BLOCKER'))) issues.push('approved_spec_review_required');
  return issues.length ? fail([...new Set(issues)]) : { ok: true, issues: [], digest: `sha256:${hashPayload(spec)}` };
}

export function freezeApprovedSpec(spec, options = {}) {
  const validation = validateSpec(spec, { ...options, requireReview: true });
  if (!validation.ok) return validation;
  return { ok: true, approved: true, digest: validation.digest, spec: structuredClone(spec), receipt: { subject: validation.digest, verdict: 'PASS' } };
}
