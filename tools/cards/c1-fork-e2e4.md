# Fix the last two fork E2E failures (CI shows the exact errors; the fake provider CLI cannot start in your sandbox, so work from code and these logs)

Repository: OpenBot desktop. Read AGENTS.md and src/backend/AGENTS.md. Base: your commit `18aa0ea58c` (PR nibiru-labs/openbot#54 branch `fix/fork-e2e-regressions`).

## CI result after your last change (real runs, GitHub Actions)

1. `bun run test:backup-model-failover` now passes the earlier cases and fails here:
   `Error: Timed out waiting for "usage-partial case:stopped" to end.` at `scripts/backup-model-failover-e2e.ts:512` (inside `check`).
   Read the `usage-partial` case in that script: what limit it sends (partial = a scope that names part of an account), and what the agent must do (the turn stops; the agent must not stay held).
   Find how a partial or family-scoped usage limit is turned into a hold in `src/backend/agent/usage-limit-gate.ts` and `turn-lifecycle.ts`, and make the stopped turn end.
2. `bun run test:steer-interrupt-pause` fails: `Timed out waiting for the held turn of the target.` at `scripts/steer-interrupt-pause-e2e.ts:200` (`waitFor`). Find the call
   at the end of the failing check, and the code that creates a "held" turn after teammate interrupts. Compare with the fork before the sync:
   `git diff 44c1e550da fd5719fc7f -- src/backend/agent src/backend/mailbox-store.ts src/backend/agent-service.ts` and `git show 44c1e550da:<file>` for the old behavior.

## Rules

- You must end with a code change for each failure you can explain. If one stays unexplained, say what you read and what you ruled out, then still fix the other.
- Change a test only if the upstream code shows the new behavior is intended; name the upstream line.
- Checks you can run: `bun run typecheck:node`, `bunx biome check <changed files>`. Do not run the E2E scripts (the fake CLI cannot start here). CI will run them.
- Never read or print `.env*` or credential files. Write only inside the worktree. No Computer Use. Do not commit; do not push.

Report: cause of each failure (file:line), the change, check results.
