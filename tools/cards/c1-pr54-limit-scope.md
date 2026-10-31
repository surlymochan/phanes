# C1: Keep explicit usage-limit scopes on every path (openbot PR 54, Codex P2)

Repo: openbot. Base: `b5146b9fbb` (head of PR 54, branch `fix/fork-e2e-regressions`).
Tracking: nibiru-labs/openbot-roadmap #69. Write commit text and code comments in ASD-STE100 English (see `AGENTS.md`).
Do not commit; the runner commits. Do not push. Do not edit `.github/workflows/*`.

## Background

PR 54 changed `UsageLimitGate.reached(agentId, resetsAt, model, family)` in
`src/backend/agent/usage-limit-gate.ts`:

- `family` is a string: hold that model family on the account.
- `family === null`: hold the whole provider account.
- `family === undefined` (new default): hold only `model:<turnModel>`.

Before PR 54 the default was `null` (whole account). `claude-client.ts` `claudeLimitFamily()` now maps
`five_hour`/`seven_day` to `null`, `seven_day_opus`/`seven_day_sonnet` to a family, and an unknown window to
`undefined`. Member turns carry `family` through `turn-lifecycle.ts` (lines ~733, ~908, ~1032, ~1050).

The Codex review on PR 54 (P2, `usage-limit-gate.ts:210`) found a path that loses the scope:

- `src/backend/agent/profile-generation.ts`: the `USAGE_LIMIT_METHOD` notification handler keeps only
  `resetsAt` and drops `params.family`. `GenerationUsageLimitError` has no `family` field.
- `src/backend/agent-service.ts` ~line 1034: the channel routing handler calls
  `this.#usageLimits.reached(lead.id, failure.resetsAt, lead.model)` without a family. So an explicit
  account-wide refusal of a routing turn now holds only the lead's model. Other models on the same exhausted
  account can start and get refused again.

## Task

1. Carry the reported scope through profile/routing generation:
   - Add `family: Schema.optional(Schema.NullOr(Schema.String))` (or an equivalent shape that keeps
     "absent" different from `null`) to `GenerationUsageLimitError`.
   - In `profile-generation.ts`, read `params.family` with the same rule as `turn-lifecycle.ts` line ~733
     (string or `null` keeps the value, anything else is `undefined`), and put it on the error.
   - In `agent-service.ts`, pass `failure.family` to `reached`.
2. Audit every other `reached(` caller that passes no family, and decide each one with a reason in a short
   code comment only where the reason is not obvious:
   - `src/backend/agent/drain-scheduler.ts` ~lines 930 and 934 (start refused with a plan-limit diagnostic
     text; no structured scope is available).
   - `agent-service.ts` ~line 1036 (`isPlanLimitDiagnostic` text path).
   For a text-only diagnostic there is no scope. Keep the PR 54 rule (model only) unless you find code or an
   upstream test that shows the account-wide hold is required there. Do not change these lines without that evidence.
3. Check the other provider clients (`app-server-client.ts`, `pi-client.ts`, `acp-client.ts`, `muse-client.ts`,
   `custom-acp-agents-client.ts`) for the usage-limit notification they emit. Report in your summary which ones
   send `family` and which do not, and what hold each gets now. Do not change them in this task; this is input
   for a follow-up decision.
4. Add one E2E check (extend an existing script, do not add unit tests). Best fit: `scripts/channel-usage-limit-e2e.ts`
   if it drives a channel routing turn; else `scripts/backup-model-failover-e2e.ts`. The check:
   a routing (lead) turn refused with an explicit account-wide window (`family: null`) holds a second agent on
   the same provider account with a different model; the same refusal with no `family` holds only the lead's model.
   Use the existing fake provider controls in that script. Do not weaken any existing assertion.

## Constraints

- Change only the files named above plus the one E2E script and, if needed, the fake provider fixture it uses.
- Do not change `scripts/token-budget-e2e.ts`, `mailbox-store.ts`, `openbot-tool-router.ts` or migrations.
- No full lint/build. The pre-commit hook and CI run them.
- The sandbox may not start the fake provider CLIs. If a script cannot start, do not spend budget on it; the host
  verify runs it.

## Acceptance

- `bun run typecheck:node` passes.
- On the host: `bun run test:token-budget`, `bun run test:backup-model-failover`, `bun run test:steer-interrupt-pause`
  and the E2E script you changed all pass.
- The new check fails on the base `b5146b9fbb` code (say how you know) and passes with the fix.
- Summary lists: files changed, the decision for each `reached(` caller, and the provider family table from step 3.
