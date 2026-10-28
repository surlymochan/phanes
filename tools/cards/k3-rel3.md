# M10 #64: the watcher of a dispatch run never ends (reliability 3)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Backend reliability finding 3 below. An earlier run could not fix it. The dispatch code also changed in another branch (it keeps the latest 100 delivered terminal records in dispatch-service.ts); keep your change small and in the watcher code so the two merge. Add an E2E script case that covers an unknown run, a stop, a restart and a delivery, and shows the watcher ends in each. Preserve existing deadlines and retries (AGENTS.md).

## Findings (from an audit written before the upstream sync; file and line references may have moved)

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

- First confirm each finding against the current code. If upstream already fixed or covered it, do not change it:
  report "already fixed" with the file and line that show it. That is a valid result.
- Do every finding in scope before you finish; report each one as fixed / already fixed / not fixed (why).
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`. Running this repository's own
  E2E scripts, Electron ones included, is allowed.
- Do not change released Team API wire meaning, IPC contract names, or migrations. No database migration.
- Never read or print `.env*`, `.env.keys`, `.dev.vars`, auth or credential files. Read one file per command.
- Stay inside this worktree. Do not use Computer Use or drive other desktop apps. Do not commit (the runner
  commits) and do not push.
- Run only narrow checks, one at a time: `bun run typecheck:node` and the E2E script you add or change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
