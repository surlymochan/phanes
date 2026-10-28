# Issue #8 acceptance: real-renderer E2E for non-empty response process notes and the working avatar

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md, src/renderer/AGENTS.md and
README.md "Commands" (dev:automation) first. Base: b50d5ce925 (PR nibiru-labs/openbot#42 head).

## Background

Feature (openbot issue 17, merged): process notes of a turn are shown (not filtered out) during and after the
answer; text-block summaries are kept; an empty reasoning completion does not replace streamed notes; the header
avatar animates while the agent works. `scripts/response-process-e2e.ts` already proves the backend path with a
fake Codex app-server (`FAKE_CODEX` in that file). There is no proof in the real renderer yet.

## Task

Add `scripts/response-process-renderer-e2e.ts` that:
1. Starts the real desktop app in an isolated profile/data directory (follow how existing Electron E2E scripts in
   scripts/ start the app, or use `bun run dev:automation` as README describes; never drive another worktree's app).
2. Uses the fake Codex app-server from `scripts/response-process-e2e.ts` (reuse or extract it; no real provider,
   no account, no network) so a turn streams reasoning/commentary with non-empty text, including a text-block
   summary and an empty reasoning completion item.
3. Sends one message and asserts in the renderer (DOM or the automation snapshot): while the turn runs, the process
   notes are visible with non-empty text and the header avatar is in its working state; after completion, the notes
   are still readable and the streamed text was not replaced by the empty completion.
4. Writes `.openbot-build/response-process-renderer-e2e/report.json` (each assertion true/false) and one screenshot.
5. Assert behavior and data, not CSS classes or markup structure (AGENTS.md test rules). Wait on state, not time.

## Environment on this host

Headless Linux. Run Electron under Xvfb: `xvfb-run -a bun scripts/response-process-renderer-e2e.ts`.
Use `ELECTRON_OVERRIDE_DIST_PATH=/opt/openbot-task-tools/electron-runtime` (already installed, chrome-sandbox set up).
Temporary files only under `$TMPDIR` or `.openbot-build/` in the worktree.

## Checks

`bun run typecheck:node`, the new script under xvfb-run (must pass), `bunx biome check <changed files>`.
Do not use Computer Use. Do not commit; do not push. Report each assertion result and the screenshot path.
If the feature itself fails an assertion, report the failing behavior precisely instead of weakening the test.
