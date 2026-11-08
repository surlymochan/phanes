const DEFAULT_MAX_AGENTS = 3;
const DEFAULT_WEIGHT = 1;

// Decimal digits only: no sign, no exponent, no surrounding whitespace.
const POSITIVE_INTEGER_STRING = /^[0-9]+$/;

function describeValue(value) {
  if (typeof value === 'number') return String(value);
  if (typeof value === 'string') return JSON.stringify(value);
  if (value === null) return 'null';
  if (value === undefined) return 'undefined';
  try {
    const json = JSON.stringify(value);
    return json === undefined ? String(value) : json;
  } catch {
    return String(value);
  }
}

/**
 * Accept only positive integers: numbers (1, 2, 3, ...) and decimal
 * positive integer strings ("1", "2", ...). Everything else — booleans,
 * fractions, blank or padded strings, NaN/Infinity, zero, negatives, hex or
 * exponent strings — is rejected instead of being coerced or truncated.
 */
function parsePositiveInteger(value, field) {
  if (typeof value === 'number' && Number.isInteger(value) && value >= 1) {
    return value;
  }
  if (typeof value === 'string' && POSITIVE_INTEGER_STRING.test(value)) {
    const parsed = Number(value);
    if (Number.isSafeInteger(parsed) && parsed >= 1) {
      return parsed;
    }
  }
  throw new Error(
    `Invalid ${field}: expected a positive integer, got ${describeValue(value)}`,
  );
}

function isUnset(value) {
  return value === undefined;
}

export function createConcurrencyPolicy({ workflow = {}, maxParallelAgentsOverride } = {}) {
  const workflowParallel = workflow.parallel || {};
  const configuredMaxAgents = isUnset(workflowParallel.max_agents)
    ? DEFAULT_MAX_AGENTS
    : parsePositiveInteger(workflowParallel.max_agents, 'max_agents');
  const hasOverride = !isUnset(maxParallelAgentsOverride);
  const maxAgents = hasOverride
    ? parsePositiveInteger(maxParallelAgentsOverride, 'override')
    : configuredMaxAgents;
  const mode = workflowParallel.policy || 'weighted';

  return {
    mode,
    maxAgents,
    source: hasOverride ? 'mission_override' : 'workflow',
    weightFor(atomRef = {}, registry = {}) {
      const atomDef = registry.atoms?.[atomRef.id] || {};
      const rawWeight = isUnset(atomRef.parallel_weight)
        ? atomDef.parallel_weight
        : atomRef.parallel_weight;
      if (isUnset(rawWeight)) {
        return DEFAULT_WEIGHT;
      }
      return parsePositiveInteger(rawWeight, 'parallel_weight');
    },
    chargeFor(atomRef = {}, registry = {}) {
      if (mode === 'fixed') {
        return 1;
      }
      return Math.min(this.weightFor(atomRef, registry), maxAgents);
    },
  };
}
