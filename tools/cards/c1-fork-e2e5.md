# Fix a regression that your last change caused in the token budget E2E

Repository: OpenBot desktop. Read AGENTS.md and src/backend/AGENTS.md. Base: `a4b0de2d66` (PR nibiru-labs/openbot#54).

CI on #54 now passes `steer, interrupt and queue pause` and `backup model failover`, but `bun run test:token-budget` fails. It passes on other PRs (for example #46), so it
is caused by the two backend changes in `a4b0de2d66` and `18aa0ea58c`:

- `src/backend/agent/turn-lifecycle.ts` (partial or family-scoped limit after work now ends the delivery as failed, with no account-wide hold)
- `src/backend/agent/openbot-tool-router.ts` (automatic steering no longer overrides an explicit busy mode)
- earlier: `src/backend/agent/usage-limit-gate.ts`, `src/backend/claude-client.ts`, `src/backend/agent-service.ts`

CI error: `Error: Timed out waiting for the refused turn to end.` at `scripts/token-budget-e2e.ts:211` (`waitFor`).

## Task

1. Read the failing check in `scripts/token-budget-e2e.ts` around line 211: which turn is "refused", and what must end it (a token-budget refusal, not a usage limit).
2. Find which of the changes above stops that turn from ending, for example a refusal now treated as a limit with a hold, or a busy mode that no longer lets the refusal finish.
3. Fix it without undoing the other two fixes: the E2Es for steer/interrupt/pause and for backup model failover must still pass (CI runs them; you cannot start the fake CLIs here).
4. Run `bun run typecheck:node` and `bunx biome check <changed files>`.

Never read or print `.env*` or credential files. Write only inside the worktree. No Computer Use. Do not commit; do not push.
Report: cause (file:line), change, check results.
