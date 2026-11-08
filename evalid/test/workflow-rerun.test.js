import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { prepareWorkflowRerun } from '../src/core/workflow-rerun.js';

function makeProject() {
  const projectRoot = mkdtempSync(join(tmpdir(), 'evalid-workflow-rerun-'));
  const pendingGate = resolve(projectRoot, '.as-xflow', 'pending-gates', 'target.json');
  mkdirSync(join(projectRoot, '.as-xflow', 'pending-gates'), { recursive: true });
  writeFileSync(pendingGate, '{"gate":"pending"}\n');
  return { projectRoot, pendingGate };
}

function assertMalformedInputFailsBeforeWriting(input, diagnostic) {
  const { projectRoot, pendingGate } = makeProject();
  try {
    assert.throws(
      () => prepareWorkflowRerun({
        workflow: { name: 'example' },
        phases: [{ id: 'target', atoms: [] }],
        phaseId: 'target',
        projectRoot,
        ...input,
      }),
      (error) => {
        assert.equal(error instanceof TypeError, false, 'must report invalid workflow input, not an implementation TypeError');
        assert.match(error.message, diagnostic);
        return true;
      },
    );
    assert.equal(existsSync(resolve(projectRoot, '.as-xflow', 'workflow-state.json')), false);
    assert.equal(existsSync(pendingGate), true, 'pending gate must remain when validation fails');
  } finally {
    rmSync(projectRoot, { recursive: true, force: true });
  }
}

test('rerun preparation rejects missing phases before writing state', () => {
  assertMalformedInputFailsBeforeWriting({ phases: undefined }, /phases/i);
});

test('rerun preparation rejects duplicate phase IDs before writing state', () => {
  assertMalformedInputFailsBeforeWriting({
    phases: [{ id: 'target', atoms: [] }, { id: 'target', atoms: [] }],
  }, /duplicate|unique/i);
});

test('rerun preparation rejects a workflow without a name before writing state', () => {
  assertMalformedInputFailsBeforeWriting({ workflow: {} }, /workflow.*name|name.*workflow/i);
});

test('rerun preparation rejects non-array phase atoms before writing state', () => {
  assertMalformedInputFailsBeforeWriting({
    phases: [{ id: 'target', atoms: {} }],
  }, /atoms/i);
  assertMalformedInputFailsBeforeWriting({
    phases: [{ id: 'target', atoms: null }],
  }, /atoms/i);
});

test('valid rerun permits omitted atoms in target and other phases', () => {
  const { projectRoot, pendingGate } = makeProject();
  try {
    const result = prepareWorkflowRerun({
      workflow: { name: 'example' },
      phases: [
        { id: 'previous', label: 'Previous', required: true },
        { id: 'target', label: 'Target', required: true },
      ],
      phaseId: 'target',
      projectRoot,
    });

    assert.equal(result.state.last_completed_phase, 'previous');
    assert.equal(existsSync(resolve(projectRoot, '.as-xflow', 'workflow-state.json')), true);
    assert.equal(existsSync(pendingGate), false);
  } finally {
    rmSync(projectRoot, { recursive: true, force: true });
  }
});

test('valid rerun preparation removes the pending gate for its phase', () => {
  const { projectRoot, pendingGate } = makeProject();
  try {
    const result = prepareWorkflowRerun({
      workflow: { name: 'example' },
      phases: [{ id: 'target', atoms: [{ id: 'atom' }] }],
      phaseId: 'target',
      projectRoot,
    });

    assert.equal(result.state.workflow, 'example');
    assert.equal(existsSync(resolve(projectRoot, '.as-xflow', 'workflow-state.json')), true);
    assert.equal(existsSync(pendingGate), false);
  } finally {
    rmSync(projectRoot, { recursive: true, force: true });
  }
});
