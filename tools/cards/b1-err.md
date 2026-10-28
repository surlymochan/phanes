# M10 #63: catalog keys for raw error messages (errors 4, 8, 11)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Base commit: b50d5ce925 (head of PR nibiru-labs/openbot#42,
the upstream sync). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Error-handling findings 4, 8 and 11 below: an attachment import failure, a shell.openPath failure and the remote-server refusal classifier show raw or lost messages. Map them to catalog keys / codes. Only the files these findings name.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 4. An attachment import failure shows the raw Electron error in the chat

**Size: S**

Evidence:

- `src/preload/index.ts:384-389` — `message: error instanceof Error ? error.message : String(error)`
- `src/renderer/src/features/conversation/conversation-scope.ts:700-709` — the text goes into `conversationErrors` as it is.
- `src/renderer/src/features/agents/agent-error-text.ts:12-17` — the agent path uses `readableAgentError`, which classifies, redacts and falls back. The import path does not.

Why it matters: the message that Electron sends is `Error invoking remote method
'attachmentImports:importAttachments': Error: ENOENT: no such file or directory, open
'/Users/name/Library/Application Support/OpenBot/…'`. The user sees that in the chat banner, with a
local path, in English, and with no fallback sentence.

Fix: run the import failure through `readableAgentError` in the renderer, the same way the agent
path does. A change in `src/main` is not needed.

### 8. `shell.openPath` failures reach the user as an OS message

**Size: S**

Evidence:

- `src/main/ipc/attachment-handlers.ts:347` — `if (error) throw new Error(error);`
- `src/main/ipc/storage-handlers.ts:117` — the same.

`shell.openPath` reports failure by resolving with a message, so the message is an English sentence
from the operating system.

Why it matters: the user reads an OS string with no catalog key, so it is not translated. The
renderer classifies it as an unknown kind and shows it as it is.

Fix: throw `new Error(sourceText("error.storage.openFailed"), { cause: error })` for the storage path
and `new Error(sourceText("error.attachment.openFailed"), { cause: error })` for the attachment
path. Keep the OS text as the cause for the log. Add both keys to
`packages/i18n/src/messages/en/error/storage.ts` and
`packages/i18n/src/messages/en/error/attachment.ts`.

### 11. The remote-server classifier drops every refusal that is not 401

**Size: S**

Evidence:

- `src/main/remote-server-connection-status.ts:143-145` — the comment says "Any other refusal is the host answering normally. It is the caller's to report, not a connection problem", then `return NO_OUTCOME;`
- `src/main/remote-server-connection-status.ts:161` — the same for an error of an unknown kind.
- `src/main/remote-server-connections.ts:176-178` — `reportError` applies that outcome.

Why it matters: a 403, 404 or 429 from a host leaves the old issue on the screen, or none at all.
The user sees a host that looks online and does not work, with no sentence and no code.

Fix: map the remaining statuses to the `error.kind.*` sentences and keep the `http/<status>`
reference. A 429 becomes the rate-limit sentence, a 403 the permission sentence.

## Rules

- First confirm each finding against the current code. If upstream already fixed one, do not change it: report
  "already fixed" with the file and line that show it. That is a valid result.
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`.
- Do not change released Team API wire contracts, IPC contract names, or migrations.
- Stay inside this worktree. Do not use GUI apps or Computer Use. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: `bun run typecheck:node` and `bun run i18n:check`, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
