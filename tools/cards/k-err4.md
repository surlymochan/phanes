# M10 #63: queued message failure reason without raw provider text (errors 15)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Error-handling finding 15 below. Your base already contains your fixes for findings 1-3, 12 and 14. Do not add a database migration: keep the stored column, store a catalog key or code for new failures, and map old raw rows at display time (old rows must still show something useful). Redact secrets on every path (AGENTS.md). Add or extend an E2E script that shows a failed queued message reaching the UI with a catalog message.

## Findings (from an audit written before the upstream sync; references may have moved)

### 15. The reason a queued message failed is a raw provider error in the database

**Size: M**

Evidence:

- `src/backend/mailbox-store.ts:1530-1543` — `markTerminal` writes `error: error === null ? null : redactText(error)` into the delivery row.
- `packages/contracts/src/ipc-queue.ts:53` — the delivery carries `error: string | null` to every client.
- `apps/mobile/src/features/chat/model/chat-messages.ts:474` — the mobile app shows that text as the failure reason.

Why it matters: the text is a provider sentence. It is stored in the database, so it outlives the
session, and it is not a catalog key, so the mobile user reads it in English with no code. On the
desktop no surface reads `delivery.error` at all, so the desktop user gets no reason (searched
`src/renderer/src` and `packages/ui/src` for a read of the delivery `error` field).

Fix: write a catalog sentence for the failure kind in the delivery row, keep the raw text as the
detail, and add a `reference` to the delivery. Then render the reason in the desktop queue row.


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
- Run only narrow checks, one at a time: the typecheck project for the code you change and the E2E scripts you add or change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
