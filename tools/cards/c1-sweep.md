# M10 #64: E2E for the browser tab sweep overlap guard (reliability 15)

Repository: OpenBot desktop. Read AGENTS.md and src/backend/AGENTS.md first.
Base: your commit b11bc81523, which adds an overlap guard to the browser tab sweep in src/backend/browser-host.ts.

## Task

Add an E2E case (extend an existing browser E2E script under scripts/, or add one) that starts two sweeps at the
same time and proves only one runs while the other is skipped or joined, and that a later sweep still runs. Run it.
Running this repository's Electron E2E scripts is allowed; do not use Computer Use or drive other desktop apps.
If the case exposes a bug in the guard, fix the guard.

## Checks

`bun run typecheck:node`, the E2E script you add or change, and `bunx biome check <changed files>`. Do not commit;
do not push. Report the script name and its result.
