# Fix two fork E2E regressions after the upstream sync (third attempt: make a code change)

Repository: OpenBot desktop. Read AGENTS.md and src/backend/AGENTS.md. Base: your commit `2d8949daba`. Main is `fd5719fc7f`; the fork before the sync is `44c1e550da`.

## What is already known

- Run 2 found the likely cause of the backup failure, but made no change:
  `src/backend/agent/usage-limit-gate.ts` (around the `completed`/limit-key code) treats an unspecified scope as account-wide, so a limit on one Claude model can hold the configured
  Claude backup model too. The sync also added a provider-failure hold in the same file (`failed()`, `isFailed()`, `PROVIDER_FAILURE_HOLD_MS`, 10 minutes) and
  `scripts/backup-model-failover-e2e.ts` (991 lines, new).
- Your last verify output showed the fake provider failing a `claude-haiku-5-5` turn right after a completed one. Do not spend more time on "fake provider CLI startup": run the script,
  read its own failure message for the step `usage-work`, and trace the exact call that creates the hold (`grep -rn "usageLimit\|usage-limit\|\.failed(" src/backend`).

## Task (you must end with a code change; a report without a change fails this task)

1. Backup failover (`bun run test:backup-model-failover`): find which call closes the gate for the backup model, and make the key cover only the limited provider account and model
   (or family) as the fork did before the sync. Keep upstream's whole-account hold only when the provider reported an account-wide limit.
2. Steer/interrupt/pause (find the script name in `.github/workflows/ci.yml`, step `steer, interrupt and queue pause`): after overlapping teammate interrupts the target has no held turn.
   Compare interrupt and hold handling in `src/backend/agent` before and after the sync (`git diff 44c1e550da fd5719fc7f -- src/backend/agent`) and restore the fork behavior.
3. If, after real reproduction, a test expects old behavior that upstream changed on purpose, change the test and say which upstream commit/line shows the intent.
4. Run one at a time: both scripts, the other steps of CI job `Team API and reverse actions E2E`, `bun run test:team-legacy-clients`, `bun run typecheck:node`,
   `bunx biome check <changed files>`.

Never read or print `.env*` or credential files. Write only inside the worktree. No Computer Use. Do not commit; do not push.
Report: cause of each failure (file:line), the change, each check result. If one failure is still open, make the other fix and say exactly what you saw for the open one.
