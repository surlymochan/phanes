# M10 #63: keep error codes through runCauseEffect and agent error events (errors 1-2)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Base commit: b50d5ce925 (head of PR nibiru-labs/openbot#42,
the upstream sync). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Error-handling findings 1 and 2 below. Make the typed error and its public code survive runCauseEffect, and give the agent error event a reference so the UI can show the code. Keep public error codes and localized messages unchanged (AGENTS.md Effect rules). Do not change released Team API wire meaning. Add or extend an E2E script that shows the code reaching the client.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 1. `runCauseEffect` throws the cause, so the error code never reaches the user

**Size: M**

Evidence:

- `src/backend/effect-boundary.ts:25-29`

  ```ts
  export async function runCauseEffect<A>(operation: Effect.Effect<A, { readonly cause: unknown }>): Promise<A> {
    const result = await Effect.runPromise(Effect.result(operation));
    if (Result.isFailure(result)) throw result.failure.cause;
  ```

- `src/backend/agent-service.ts:4144-4147` — `AgentLifecycleFailed` has `operation` and `cause`.
- `src/main/ipc/agent-handlers.ts:211` — `local: (agentId) => runCauseEffect(service.getUsage(agentId))`, and the same shape in most local branches.

The typed error is the thing that names the failure. `errorReference` reads its `_tag`
(`packages/user-errors/src/reference.ts:107`). `runCauseEffect` throws `.cause` instead, so the
`_tag` and the `operation` are gone before the error reaches `withReference`.

A probe on this checkout (`.openbot-build/probe-ref.ts`, Effect 4.0.0):

```
tagged _tag: StoredStateFailure
reference(tagged): error/StoredStateFailure
reference(cause): null
```

Why it matters: every backend domain failure that crosses IPC arrives with no copyable code. The
banner in `packages/ui/src/features/conversation/ComposerErrorBanner.tsx:19` can show a reference,
but for these failures there is none. A user who reports "the settings save failed" has nothing to
quote.

Fix: keep the typed error. Change `runCauseEffect` to throw the failure itself, or to copy the
`_tag` onto the cause before throwing it. Then check the call sites that expect the raw cause:
`src/main/team-api/route-providers.ts:199` and the other `instanceof` checks in
`src/main/team-api-server.ts:853-859` need the typed error, not the cause.

### 2. The agent error event carries no reference

**Size: M**

Evidence:

- `packages/contracts/src/ipc-agent-events.ts:130` — `| { type: "error"; agentId?: string; code: string; message: string }`
- `src/backend/agent-service.ts:4089-4107` — `#emitError` writes `code` (an internal event name such as `usage_limit_hold_failed`) and `message`. No reference.
- `src/renderer/src/features/conversation/conversation-controller.ts:212` — references are kept only for `composerErrors`. `conversationErrors` has no reference map.
- `src/renderer/src/features/agents/agent-event-bridge.tsx:300-303` — the agent error goes into `conversationErrors`.

Why it matters: the agent and turn failures are the most common failure the user meets. The
`code` field is an internal name, not a code the user can copy, and the renderer never calls
`errorReference` for this path. The result is the same as finding 1, on the path that matters most.

Fix: add an optional `reference` to the error event, compute it in `#emitError` with
`errorReference(error)`, and keep it beside `conversationErrors` in the renderer. The field is
additive, so an older client keeps working.

## Rules

- First confirm each finding against the current code. If upstream already fixed one, do not change it: report
  "already fixed" with the file and line that show it. That is a valid result.
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`.
- Do not change released Team API wire contracts, IPC contract names, or migrations.
- Stay inside this worktree. Do not use GUI apps or Computer Use. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: `bun run typecheck:node`, `bun run typecheck:renderer` if you touch src/renderer, and the E2E script you add or change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
