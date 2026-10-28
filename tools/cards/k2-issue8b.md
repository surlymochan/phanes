# Issue #8 acceptance (continue): real-renderer E2E for non-empty response process notes and the working avatar

Repository: OpenBot desktop. Read AGENTS.md and src/renderer/AGENTS.md. Base: your commit ab9074479a, which already has
`scripts/response-process-renderer-e2e.ts` (413 lines) and `scripts/fake-codex-response-process-e2e.ts` from an earlier run that ran
out of time (45 minutes) while fighting the start-up of the Auth API (the 30 second wait of `dev-services` is too short on a cold start).

## Task

Make `xvfb-run -a bun scripts/response-process-renderer-e2e.ts` pass on this host and write its JSON report and one screenshot under
`.openbot-build/response-process-renderer-e2e/`. Rules for the script: it starts the real app in an isolated profile with the fake
Codex app-server; it sends one message; it asserts in the renderer that (1) while the turn runs, the process notes are visible with
non-empty text and the header avatar is in its working state, and (2) after completion the notes are still readable and were not
replaced by an empty reasoning completion. Assert behavior and data, not classes or markup. Wait on state, not time.

Time box: 30 minutes. For the Auth API start-up, start the local auth service yourself and wait for its port with a long enough deadline
(or point `OPENBOT_AUTH_API_URL` at a small local stub if the app only needs a reachable loopback origin to show the chat); do not
change product code to make a test pass. If an assertion fails because the FEATURE is wrong, report the exact failing behavior and stop.
If you are blocked by the host (Xvfb, Electron, ports), write the exact blocker and the command that shows it, and stop.

Environment: `ELECTRON_OVERRIDE_DIST_PATH=/opt/openbot-task-tools/electron-runtime`, run under `xvfb-run -a`. Temporary files only
under `$TMPDIR` or `.openbot-build/`; never write or remove anything in `/tmp`. Do not use Computer Use.

## Checks

The script (must pass), `bun run typecheck:node`, `bunx biome check <changed files>`. Do not commit; do not push. Report each assertion.
