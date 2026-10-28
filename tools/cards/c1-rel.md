# M10 #64: fix backend reliability findings 1-3

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Base commit: b50d5ce925 (head of PR nibiru-labs/openbot#42,
the upstream sync). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Backend reliability findings 1, 2 and 3 below (provider start timeout on the account read, provider process left after an agent is deleted, dispatch run watcher that never ends). Files: provider runtime / agent deletion / dispatch watcher code only. Each fix needs an E2E script case that fails before and passes after, or an extension of an existing provider or dispatch E2E script.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 1. A five second account read can stop a provider from starting at all

Size: S.

Evidence:

- `src/backend/agent/provider-runtime.ts:2732` — `.request("account/read", { refreshToken: false }, decodeAccountReadResult, 5_000)`
- `src/backend/agent/provider-runtime.ts:2176` — the same call with `refreshToken: true`
- `src/backend/agent/provider-runtime.ts:2786` — `if (isProviderTimeout(error)) {`
- `src/backend/agent/provider-runtime.ts:2790` — `if (this.#countStartFailure(provider)) return this.#showStartStopped(provider);`
- `src/backend/agent/provider-runtime.ts:3124` — `#countStartFailure`
- `src/backend/agent/provider-runtime.ts:123` — `const START_FAILURE_LIMIT = 3;`

Why it matters: the `initialize` request before this one gets 30 seconds, or 180 seconds for a
slow provider. The account read that follows gets five seconds. A slow or busy machine answers the
account read later than that. The timeout then counts as a failed start. Three such timeouts stop
the provider. The chat stays unusable until the user acts. The status row says that OpenBot stopped
starting the provider, so the user does not learn that the machine was busy.

Fix: give the account read the same budget as the initialize request. Use `this.#requestTimeoutMs`,
or `LONG_START_TIMEOUT_MS` for a provider in `#slowStarts`. Do not count an account read timeout as
a start failure. Let the timeout path use the backoff of `#retryAfterTimeout` instead.

### 2. The deletion of an agent leaves the provider process of that agent running

Size: M.

Evidence:

- `src/backend/agent/provider-runtime.ts:320` — `readonly #confined = new Map<string, ConfinedClient>();`
- `src/backend/agent/provider-runtime.ts:766` — the idle release of a confined process
- `src/backend/agent/provider-runtime.ts:724` — `if (this.#hooks.isStopping() || this.#status.phase !== "ready") return;`
- `src/backend/agent/agent-removal.ts:158` — `#removeAgentDataEffect`
- `src/backend/agent/agent-removal.ts:166` — `yield* this.#threads.releaseAgentSessions(agent.id);`
- `src/backend/agent/agent-removal.ts:192` and `src/backend/agent/agent-removal.ts:204` — the forget calls

Why it matters: an agent gets a process of its own when it is a Workspace only agent on a provider
that confines a process, or when it uses a provider account. The deletion path never asks the
provider runtime to stop that process. It closes the provider threads of the agent, but the process
and its MCP servers stay. Only the idle sweep stops the process. The sweep returns early unless
the provider status is "ready", so a process of a deleted agent can live until the app stops. It
holds its memory, and on Windows it keeps the deleted workspace locked.

Fix: add `ProviderRuntime.forgetAgent(agentId)`. It stops the confined client and removes the map
entry. Call it in `AgentRemoval.#removeAgentDataEffect` before the files of the agent are removed.
The rule of the repository asks that an owned resource is tied to a finalizer.

### 3. The watcher of a dispatch run never ends

Size: M.

Evidence:

- `src/backend/dispatch/dispatch-service.ts:506` — `readonly #watch = Effect.fn("DispatchService.watch")`
- `src/backend/dispatch/dispatch-service.ts:516` — `readonly #poll = Effect.fn("DispatchService.poll")`
- `src/backend/dispatch/dispatch-service.ts:521` — `yield* Effect.sleep("2 seconds");`
- `src/backend/dispatch/dispatch-process.ts:262` — `duration: "30 seconds",`
- `src/backend/dispatch/dispatch-service.ts:240` — `for (const record of this.#runs) if (!record.delivered) yield* this.#watch(record.host, record.runId);`

Why it matters: a run that never reaches a terminal state keeps a poll loop for the life of the
app. Every poll spawns an `openswarm` process. A restart of the app watches every record that was
not delivered again. The remote host then sees one process every two seconds, for as long as the
app runs. The row of the run in the interface also stays "running" for ever.

Fix: give the watch a maximum duration and a backoff between the polls. Mark the record as
unknown when the limit passes. The user can then stop the run.

## Rules

- First confirm each finding against the current code. If upstream already fixed one, do not change it: report
  "already fixed" with the file and line that show it. That is a valid result.
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`.
- Do not change released Team API wire contracts, IPC contract names, or migrations.
- Stay inside this worktree. Do not use GUI apps or Computer Use. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: `bun run typecheck:node` and the E2E scripts you add or change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
