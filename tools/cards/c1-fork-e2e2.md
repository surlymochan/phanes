# Continue: two fork E2E checks still fail after the upstream sync

Repository: OpenBot desktop. Read AGENTS.md and src/backend/AGENTS.md. Base: your commit `2d8949daba` (one product fix in `src/backend/mailbox-store.ts`: message batching no
longer adds queued messages to a run-now turn). Main is `fd5719fc7f`; the fork before the sync is `44c1e550da` (compare behavior with `git worktree add` at that commit when needed).

## State after your first run

- `test:steer-interrupt-pause` (find the real script name in `.github/workflows/ci.yml`): the first nine checks pass now, then it times out waiting for the target's held turn
  after overlapping teammate interrupts.
- `test:backup-model-failover` (`scripts/backup-model-failover-e2e.ts`): five checks pass, then `usage-work` times out. A `claude-haiku-5-5` turn fails and `drain-scheduler` logs
  `A queued message waits` with reason `usage-limit`. You found that this usage-limit event holds the whole Claude account, including the configured Claude backup model.

## Task: find and fix the causes. Do not stop at a partial result.

1. Backup failover. Compare how a usage-limit failure is scoped, before and after the sync:
   `git diff 44c1e550da fd5719fc7f -- src/backend/agent/drain-scheduler.ts src/backend/agent src/backend/mailbox-store.ts` (restrict to the files that mention `usage`, `limit`, `backup`, `hold`).
   Search for where a hold or wait is created for `usage-limit` (`grep -rn "usage-limit\|usageLimit\|usage_limit" src/backend`) and what key it holds (account, provider, model).
   The fork's feature: when a provider reports a usage limit for a model, the agent moves to its configured backup model and keeps working; the hold must cover only what is
   really limited, not a backup that has its own limit. Decide from the old code and the test what the correct scope is, and restore it. If the upstream code deliberately holds the whole
   account for another case (for example a real account-wide limit), keep that case and make the scope depend on what the provider reported.
2. Steer/interrupt/pause. Find why, after overlapping teammate interrupts, the target has no held turn. Compare the interrupt and hold handling before and after the sync in the same way.
3. Both are about merge resolutions that kept one side. Prefer a product fix that restores the fork behavior while keeping upstream's behavior elsewhere. Change a test only when you can
   show with the upstream code that the new behavior is intended; say so.
4. Run and pass, one at a time, both scripts, then every other step of the CI job `Team API and reverse actions E2E` (list in `.github/workflows/ci.yml`, each is `bun run test:<name>`),
   `bun run test:team-legacy-clients`, `bun run typecheck:node`, `bunx biome check <changed files>`.

Budget: take the time you need. Never write or remove anything in `/tmp` or outside the worktree. No Computer Use. Do not commit; do not push. Report the cause of each failure (file and line),
what you changed, and every check result.
