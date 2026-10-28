# Two fork E2E checks fail after the upstream sync: steer/interrupt/queue pause and backup model failover

Repository: OpenBot desktop. Read AGENTS.md, src/backend/AGENTS.md and src/main/AGENTS.md. Base: `fd5719fc7f` (main, after merging 429 upstream commits into the fork).

## Problem

In CI, the job `Team API and reverse actions E2E` fails exactly two steps on every pull request on this base:

1. `bun run test:steer-interrupt-queue-pause` (the step "Run the steer, interrupt and queue pause E2E test" in `.github/workflows/ci.yml`; find the real script name there).
2. `bun run test:backup-model-failover` (`scripts/backup-model-failover-e2e.ts`). Five checks pass, then it fails with
   `Error: Timed out waiting for "usage-work: save a checkpoint, then finish the last step." to end.` (waitFor at line ~390, called from `send` at ~474 and ~695).
   The log shows `drain-scheduler: A queued message waits {"reason":"usage-limit"}` and turns with status `failed` on `claude-haiku-5-5`, then a turn that completed on `claude-sonnet-5-5`.

Both test features that this fork added before the sync (steer by default, queue pause, run a message now, teammates steer or interrupt; backup models when a provider hits
its usage limit). The sync merged upstream's own changes to the same area (`src/backend/agent/drain-scheduler.ts`, the mailbox/queue code, turn lifecycle). The same two steps
passed on the fork before the sync (`git stash`-free check: use `git worktree add` at `44c1e550da` if you need the old behavior to compare; its tests are in the same scripts).

## Task

1. Reproduce both on this base with the scripts above (single worker, one at a time). Record exactly which check fails and the log lines around it.
2. Find the cause. Compare the fork's behavior (`44c1e550da`) with the current code for the paths those checks cover: how a usage-limit failure is handled, when a queued
   message waits and when a backup model takes over, how steer/interrupt/pause/run-now interact with the drain scheduler. Typical cause after a merge: both sides changed the same
   function and the resolution kept one side's order of steps. Decide whether the PRODUCT is wrong (a fork behavior was lost) or the TEST encodes an old assumption that upstream
   changed on purpose. Prefer fixing the product when a fork feature was lost; change a test only when you can show, with the upstream code, that the new behavior is intended, and say so.
3. Fix it with the smallest change. Keep upstream's queue semantics and keep the fork's features. No migration, no released Team API or IPC contract change.
4. Run and pass, one at a time: both scripts, then every other step of that CI job (read the list in `.github/workflows/ci.yml`, job `Team API and reverse actions E2E`; each is a `bun run test:<name>`),
   `bun run test:team-legacy-clients`, `bun run typecheck:node`, `bunx biome check <changed files>`.

Never write or remove anything in `/tmp` or outside the worktree. Do not use Computer Use. Do not commit; do not push. Report the cause (file and line), whether you changed product or test and why, and every check result.
