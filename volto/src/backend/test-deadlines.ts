// Vitest's per-test budget, strictly longer than the 10 s DOM wait deadline in
// `src/renderer/src/setupTests.ts`, which cannot import from here. A stalled wait
// then fails with the query that never matched, not with vitest's generic
// "test timed out".

/** The DOM wait deadline in `setupTests.ts`. */
const WAIT_TIMEOUT_MS = 10_000;

/** Vitest's per-test budget. Strictly greater, so a wait deadline always reports first. */
export const TEST_TIMEOUT_MS = WAIT_TIMEOUT_MS * 2;
