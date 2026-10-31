# C1 rework: the new routing scope check breaks the next scenario in backup-model-failover E2E

Rework of run 20261029-132608-dac7c2 (commit 0c4527884a). Base: that commit. Keep its product changes in
`profile-generation.ts` and `agent-service.ts` unless the evidence below shows they are wrong.
Do not commit; the runner commits. Do not push. ASD-STE100 English for comments.

## Host verify result of the previous run

`bun run typecheck:node` and `bun run test:token-budget` passed. Then `bun run test:backup-model-failover`:

```
INFO [drain-scheduler] A queued message waits. {"agentId":"agent-9e15...","reason":"usage-limit"}
PASS routing refusal holds the account scope
FAIL .../.openbot-build/backup-model-failover-e2e/report.json
Error: Timed out waiting for the initial Codex turn to end.
    at waitFor (scripts/backup-model-failover-e2e.ts:391:42)
    at async createCodex (scripts/backup-model-failover-e2e.ts:535:9)
    at async <anonymous> (scripts/backup-model-failover-e2e.ts:589:27)
    at async check (scripts/backup-model-failover-e2e.ts:513:24)
```

The new check passes, and the scenario after it times out. Probable cause (not proven): the account-wide hold that
the new check makes stays in the gate or the shared fake provider state, so the next scenario's first Codex turn
is held by `usage-limit`.

## Task

1. Find the real cause. Compare the new check's setup and teardown with the other scenarios in the script
   (how each one gets a fresh service, accounts and fake provider state).
2. Fix the isolation of the new check: for example give it its own provider account, its own service instance,
   or release the hold (fake reset in the past / explicit reset) before the check ends. Use what the script already does elsewhere.
3. If the cause is in product code (a hold that never releases after its reset), fix the product code instead and say so.
4. Do not weaken, skip or reorder away any existing assertion or scenario. Do not raise timeouts to hide it.

## Acceptance (host verify)

`bun run typecheck:node && bun run test:token-budget && bun run test:backup-model-failover && bun run test:steer-interrupt-pause && bun scripts/channel-usage-limit-e2e.ts` exits 0.
In the summary: the root cause with the line numbers, and how you know the new check still fails on `b5146b9fbb`.
