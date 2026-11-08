import { describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { buildMemoryMount } from '../src/core/memory-state.js';

function makeState(count = 5) {
  return {
    memories: Array.from({ length: count }, (_, index) => ({
      memory_id: `memory-${index + 1}`,
      status: 'active',
    })),
  };
}

describe('buildMemoryMount limit', () => {
  test('defaults to 20 items when limit is omitted', () => {
    assert.equal(buildMemoryMount(makeState(25)).total, 20);
  });

  test('uses a positive integer limit exactly', () => {
    const mount = buildMemoryMount(makeState(5), { limit: 2 });

    assert.equal(mount.total, 2);
    assert.deepEqual(mount.items.map((item) => item.memory_id), ['memory-1', 'memory-2']);
  });

  test('accepts a numeric string limit', () => {
    assert.equal(buildMemoryMount(makeState(5), { limit: '3' }).total, 3);
  });

  for (const limit of [-1, 0, Number.NaN, Number.POSITIVE_INFINITY, 1.5, '0', '', '  ', 'abc']) {
    test(`rejects invalid limit ${String(limit)}`, () => {
      assert.throws(
        () => buildMemoryMount(makeState(5), { limit }),
        { name: 'RangeError' },
        `expected limit ${String(limit)} to be rejected`,
      );
    });
  }
});
