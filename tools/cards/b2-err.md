# M10 #63: bounded retries and visible failures (errors 6, 9, 13)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Base commit: b50d5ce925 (head of PR nibiru-labs/openbot#42,
the upstream sync). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Error-handling findings 6, 9 and 13 below: the hosted-site marker retry that never stops, the tool router that drops the real failure reason, and the usage read failure that leaves the token budget at zero in silence. Only the files these findings name.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 6. The hosted-site terminal marker retries every second, forever

**Size: S**

Evidence:

- `src/backend/agent/hosted-site-coordinator.ts:437-447`

  ```ts
  if (this.#pendingTerminalEvents.size > 0) this.#scheduleTerminalRetry();
  ...
  #scheduleTerminalRetry(): void {
    if (this.#terminalRetryTimer || this.#isStopping()) return;
    this.#terminalRetryTimer = setTimeout(() => {
      this.#terminalRetryTimer = null;
      this.#flushPendingTerminalEvents();
    }, 1_000);
  ```

- `src/backend/agent/hosted-site-coordinator.ts:398-406` — every failed write calls `#emitError("hosted_site_marker_persistence_failed", …)`.

Why it matters: there is no attempt cap and no backoff. A full or locked database makes OpenBot
write to SQLite every second for the rest of the session, and the renderer gets an error event
every second. The pending row is already in the database, so the next start can retry it.

Fix: cap the attempts, then use a backoff, and leave the row for the next start. Report the failure
once with a code.

### 9. The tool router drops the real reason of a failed tool call

**Size: S**

Evidence:

- `src/backend/agent/openbot-tool-router.ts:310-315` — `catch { client.respond(request.id, openBotToolFailure(sourceText("error.agent.toolRequestInvalid"))); }`

Why it matters: every failure inside `discoverTools` becomes "the tool request is invalid". A
database failure, a provider failure and a malformed request all read the same to the agent, and
to the user who reads the transcript. The cause is neither logged nor reported.

Fix: report the cause with `this.#hooks.emitError("tool_discovery_failed", error)` and keep the
generic sentence for the client.

### 13. A failed usage read leaves the token budget at zero, in silence

**Size: S**

Evidence:

- `src/backend/agent/token-budget.ts:200-207` — `catch { // A failed read counts from this start on. The limit then holds later, not never. }`
Why it matters: the budget counts what the database stored. When the read fails the count is zero,
so the pause that protects the user's quota can arrive late. Nothing is reported, so the user does
not know the limit is not being enforced.

Fix: report the failure through the hooks and mark the ledger as unknown until the next good
read, so the panel can say that the reading failed.

## Rules

- First confirm each finding against the current code. If upstream already fixed one, do not change it: report
  "already fixed" with the file and line that show it. That is a valid result.
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`.
- Do not change released Team API wire contracts, IPC contract names, or migrations.
- Never read or print `.env*`, `.env.keys`, `.dev.vars`, auth or credential files; the risk guard stops the run. Read one file per command.
- Stay inside this worktree. Do not use GUI apps or Computer Use. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: `bun run typecheck:node`, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
