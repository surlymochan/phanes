/**
 * Concurrency policy contract — Refs #2012.
 *
 * The concurrency policy is the single place that turns workflow/atom
 * configuration into a parallel budget, so it must not guess what the author
 * meant. Only positive integers are accepted:
 *
 *   - numbers: 1, 2, 3, ...
 *   - strings: "1", "2", "3", ... — decimal digits only, no sign, no
 *     exponent, no surrounding whitespace
 *
 * Every other value is rejected with an Error that names the offending
 * field. In particular the policy must not coerce through Number():
 *
 *   - `true` must not become 1 (a boolean is not a concurrency limit)
 *   - `2.9` must not become 2 (a decimal is not truncated)
 *   - `" 4 "` / `"   "` must not become 4 / the default
 *   - `"0x10"` / `"1e3"` must not become 16 / 1000
 *   - NaN, Infinity, 0 and negative numbers must not fall back silently
 *
 * An absent value is not an error: `max_agents` falls back to the default
 * budget (3) and an unset atom weight falls back to 1.
 */

import { describe, test } from 'node:test';
import assert from 'node:assert/strict';

import { createConcurrencyPolicy } from '../src/core/concurrency-policy.js';

const DEFAULT_MAX_AGENTS = 3;
const DEFAULT_WEIGHT = 1;

const INVALID_LIMITS = [
  { label: 'boolean true', value: true },
  { label: 'boolean false', value: false },
  { label: 'fractional number', value: 2.9 },
  { label: 'fractional string', value: '2.9' },
  { label: 'empty string', value: '' },
  { label: 'whitespace-only string', value: '   ' },
  { label: 'whitespace-padded string', value: ' 4 ' },
  { label: 'NaN', value: Number.NaN },
  { label: 'NaN string', value: 'NaN' },
  { label: 'Infinity', value: Number.POSITIVE_INFINITY },
  { label: 'Infinity string', value: 'Infinity' },
  { label: 'zero', value: 0 },
  { label: 'zero string', value: '0' },
  { label: 'negative number', value: -1 },
  { label: 'negative string', value: '-2' },
  { label: 'hexadecimal string', value: '0x10' },
  { label: 'exponent string', value: '1e3' },
];

function policyWithMaxAgents(value) {
  return createConcurrencyPolicy({ workflow: { parallel: { max_agents: value } } });
}

describe('concurrency policy — workflow max_agents', () => {
  test('accepts a positive integer max_agents', () => {
    const policy = createConcurrencyPolicy({ workflow: { parallel: { max_agents: 5 } } });
    assert.equal(policy.maxAgents, 5);
    assert.equal(policy.source, 'workflow');
  });

  test('accepts a positive integer string max_agents', () => {
    const policy = createConcurrencyPolicy({ workflow: { parallel: { max_agents: '5' } } });
    assert.equal(policy.maxAgents, 5);
    assert.equal(policy.source, 'workflow');
  });

  test('defaults to 3 agents when max_agents is absent', () => {
    const policy = createConcurrencyPolicy({ workflow: { name: 'no-parallel-section' } });
    assert.equal(policy.maxAgents, DEFAULT_MAX_AGENTS);
    assert.equal(policy.source, 'workflow');
  });

  for (const { label, value } of INVALID_LIMITS) {
    test(`rejects ${label} max_agents`, () => {
      assert.throws(
        () => policyWithMaxAgents(value),
        Error,
        `max_agents=${JSON.stringify(value)} must be rejected, not coerced`,
      );
    });
  }

  test('does not turn boolean true into a budget of 1', () => {
    assert.throws(() => policyWithMaxAgents(true), Error);
  });

  test('does not truncate a fractional max_agents', () => {
    assert.throws(() => policyWithMaxAgents(2.9), Error);
  });
});

describe('concurrency policy — mission override', () => {
  test('accepts a positive integer override', () => {
    const policy = createConcurrencyPolicy({
      workflow: { parallel: { max_agents: 5 } },
      maxParallelAgentsOverride: 7,
    });
    assert.equal(policy.maxAgents, 7);
    assert.equal(policy.source, 'mission_override');
  });

  test('accepts a positive integer string override', () => {
    const policy = createConcurrencyPolicy({
      workflow: { parallel: { max_agents: 5 } },
      maxParallelAgentsOverride: '7',
    });
    assert.equal(policy.maxAgents, 7);
    assert.equal(policy.source, 'mission_override');
  });

  for (const { label, value } of INVALID_LIMITS) {
    test(`rejects ${label} mission override`, () => {
      assert.throws(
        () => createConcurrencyPolicy({
          workflow: { parallel: { max_agents: 5 } },
          maxParallelAgentsOverride: value,
        }),
        Error,
        `maxParallelAgentsOverride=${JSON.stringify(value)} must be rejected, not coerced`,
      );
    });
  }
});

describe('concurrency policy — atom weight', () => {
  const registry = { atoms: { 'TEST.weighted.1': { track: 'heavy', parallel_weight: 4 } } };

  test('accepts a positive integer weight', () => {
    const policy = createConcurrencyPolicy({ workflow: { parallel: { max_agents: 5 } } });
    assert.equal(policy.weightFor({ id: 'TEST.weighted.1', parallel_weight: 3 }, registry), 3);
  });

  test('accepts a positive integer string weight', () => {
    const policy = createConcurrencyPolicy({ workflow: { parallel: { max_agents: 5 } } });
    assert.equal(policy.weightFor({ id: 'TEST.weighted.1', parallel_weight: '3' }, registry), 3);
  });

  test('reads the weight from the atom registry definition', () => {
    const policy = createConcurrencyPolicy({ workflow: { parallel: { max_agents: 5 } } });
    assert.equal(policy.weightFor({ id: 'TEST.weighted.1' }, registry), 4);
  });

  test('defaults to weight 1 when no weight is configured', () => {
    const policy = createConcurrencyPolicy({ workflow: { parallel: { max_agents: 5 } } });
    assert.equal(policy.weightFor({ id: 'TEST.weighted.2' }, { atoms: {} }), DEFAULT_WEIGHT);
  });

  for (const { label, value } of INVALID_LIMITS) {
    test(`rejects ${label} atom weight`, () => {
      const policy = createConcurrencyPolicy({ workflow: { parallel: { max_agents: 5 } } });
      assert.throws(
        () => policy.weightFor({ id: 'TEST.weighted.1', parallel_weight: value }, registry),
        Error,
        `parallel_weight=${JSON.stringify(value)} must be rejected, not coerced`,
      );
    });
  }

  for (const { label, value } of INVALID_LIMITS) {
    test(`rejects ${label} registry atom weight`, () => {
      const policy = createConcurrencyPolicy({ workflow: { parallel: { max_agents: 5 } } });
      assert.throws(
        () => policy.weightFor(
          { id: 'TEST.weighted.3' },
          { atoms: { 'TEST.weighted.3': { track: 'heavy', parallel_weight: value } } },
        ),
        Error,
        `registry parallel_weight=${JSON.stringify(value)} must be rejected, not coerced`,
      );
    });
  }
});

describe('concurrency policy — budget and charges', () => {
  test('clamps a valid weight to the agent budget', () => {
    const policy = createConcurrencyPolicy({ workflow: { parallel: { max_agents: 3 } } });
    assert.equal(policy.chargeFor({ id: 'TEST.weighted.1', parallel_weight: 5 }, { atoms: {} }), 3);
  });

  test('fixed policy charges one agent per atom', () => {
    const policy = createConcurrencyPolicy({
      workflow: { parallel: { max_agents: 3, policy: 'fixed' } },
    });
    assert.equal(policy.mode, 'fixed');
    assert.equal(policy.chargeFor({ id: 'TEST.weighted.1', parallel_weight: 2 }, { atoms: {} }), 1);
  });

  test('rejection errors name the offending field', () => {
    assert.throws(() => policyWithMaxAgents(2.9), /max[_ ]?agents/i);
    assert.throws(
      () => createConcurrencyPolicy({
        workflow: { parallel: { max_agents: 5 } },
        maxParallelAgentsOverride: true,
      }),
      /max[_ ]?agents|override/i,
    );

    const policy = createConcurrencyPolicy({ workflow: { parallel: { max_agents: 5 } } });
    assert.throws(
      () => policy.weightFor({ id: 'TEST.weighted.1', parallel_weight: 2.9 }, { atoms: {} }),
      /weight/i,
    );
  });
});
