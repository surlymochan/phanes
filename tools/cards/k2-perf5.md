# M10 #61: renderer performance findings 8 and 9

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Renderer performance findings 8 and 9 below (conversation controller keyed writes spread the whole map; turns provider keeps eleven keyed records and reads them whole). Your base 7f1b4c97f2 has your fixes for findings 1, 3, 4, 6, 7 and 12. Keep behavior identical. Extend scripts/renderer-conversation-perf-e2e.ts with a measurement for each fix and report the median before/after over at least 5 runs; revert a change that is not clearly faster.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 8. The conversation controller is a wall of signals, and every keyed write spreads the whole map

**Evidence**

- `src/renderer/src/features/conversation/conversation-controller.ts:125-145` and `:210-222`
  — 21 `createSignal` calls in the stable half, of which `drafts`, `channelDrafts`,
  `conversationErrors`, `composerErrors` and `composerErrorReferences` are keyed records.
- `src/renderer/src/features/conversation/conversation-controller.ts:363-397` — 21 more in
  the server half, of which `rightPanels` is a keyed record and `sidePanels` and
  `sideQuestions` are already stores.
- `src/renderer/src/features/conversation/stores/composer-store.ts:44-56` — every reader
  reads the whole record: `deps.drafts()[composerDraftKey(target)]`.
- `src/renderer/src/features/conversation/stores/composer-store.ts:109-113` — a draft write
  replaces the whole map: `{ ...current, [key]: … }`.

**Why it matters**

A draft typed in one chat replaces the `drafts` object, so `currentDraft()` re-runs, and
every consumer of `drafts()` re-runs. The same holds for the error records and for
`rightPanels`. This is the case the renderer instructions describe directly: "Ten keyed
`Record` signals are one row type shredded into ten columns, every write spreads the whole
map so every consumer of every key re-runs."

**Fix**

One `createStore` per concern, as `packages/ui/src/features/custom-providers/CustomProviderDialog.tsx`
does for a form. Write the field where it changes, and keep the setter private behind a
named mutation where the store is the exported surface.

**Size** M

### 9. The turns provider keeps eleven keyed records, and reads them whole

**Evidence**

- `src/renderer/src/turns.tsx:80-113` — eleven `createSignal<Record<…>>` calls:
  `activeTurns`, `turnProgress`, `failedTurns`, `contextStates`, `usageLimits`,
  `waitedUsageLimits`, `queues`, `waits`, `routinesByConversation`, `pendingPrompts` and
  `pendingApprovals`, plus `usageLimitedModels`.
- `src/renderer/src/turns.tsx:447-459` — `activeQueue`, `activeWaits` and
  `activeRoutineSnapshot` read the whole record and then pick one key.
- `src/renderer/src/turns.tsx:190-212` — a change in the agent list loads the queue of
  **every** agent, one request each.

**Why it matters**

A queue change for one agent replaces the whole `queues` object, so `activeQueue` re-runs
for the open agent. The agent-list effect asks the host for the queue of every agent, so a
list of 50 agents costs 50 requests on every change of the list.

**Fix**

One store for the turn state, with a row per agent, so a write touches one agent. Load the
queue of the open agent on demand, and the others when their row is read.

**Size** M


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
- Run only narrow checks, one at a time: `bun run typecheck:renderer` and `bun scripts/renderer-conversation-perf-e2e.ts`, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
