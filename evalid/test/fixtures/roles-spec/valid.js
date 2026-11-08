import { hashPayload } from '../../../src/core/workflow-integrity.js';

export const spec = {
  spec_version: 1,
  scope: { allowed_files: ['src/api.js'], excluded_files: ['secrets/**'] },
  scenarios: [{ id: 'public-api', requirement_ref: 'REQ-1', given: 'a valid request', when: 'the API is called', then: [{ channel: 'exit', operator: 'eq', expected: 0 }], command_id: 'api-check' }],
  commands: [{ id: 'api-check', argv: ['node', 'test/api.test.js'], cwd: '.', expected_exit: 0, timeout_s: 30, environment_digest: 'sha256:fixture' }],
  tdd_mode: 'red-green',
};
export const intake = { requirements: ['REQ-1'] };
export const approvedReview = { subject: `sha256:${hashPayload(spec)}`, verdict: 'PASS', findings: [] };
