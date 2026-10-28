# M10 #65: mobile gaps below the top 15 (client-only, up to 3)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

The list below has mobile parity gaps that are smaller than the top 15. Pick up to three that are client-only (apps/mobile) and fix them. HARD RULE: a result that changes any file under packages/contracts, packages/team-client, src/main or src/backend is rejected; use only existing Team API routes. Say which gaps you picked and why the others need server or protocol work. User text from @openbot/i18n keys. Add or extend a mobile script under scripts/ with a case for each fix. Temporary files only under $TMPDIR or .openbot-build/.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## Gaps below the top 15

These gaps are real. They have a low user impact, so they are not in the table.

- **Stop and send.** The desktop stops the running turn and sends the text of the composer in one
  step (`src/renderer/src/features/conversation/stores/composer-actions.ts:612`). The mobile stops
  the turn (`apps/mobile/src/features/chat/model/voice-dictation.ts:177`) and sends in a second step.
  Size: **S**.
- **The scroll rail.** The desktop draws a rail with the questions of the reader
  (`packages/ui/src/features/conversation/ChatScrollRail.tsx:219`). The mobile has a button that jumps
  to the latest message, without a count of the new messages.
  Size: **M**.
- **The open waits panel.** The desktop shows the waits of an agent above the composer and cancels
  them (`packages/ui/src/features/conversation/OpenWaits.tsx:51`). The mobile shows a badge of the
  wait in the agent list (`apps/mobile/src/features/agents/components/agent-wait-badge.tsx`), but no
  note and no cancel.
  Size: **M**.
- **The routine card in the chat.** The desktop edits the schedule of a routine from the chat
  (`src/renderer/src/features/conversation/RoutineChatCard.tsx:40`). The mobile shows the routine as
  a marker and edits it in the routine screen
  (`apps/mobile/src/app/(app)/server-routines/index.tsx`).
  Size: **M**.
- **Citations.** The desktop draws the sources of an answer
  (`packages/ui/src/features/conversation/RichMessageText.tsx:229`). The mobile has no citation
  field, and no host field carries it. A fix needs a new field first.
  Size: **L**.
- **The quote chip opens the source.** The desktop opens the quoted message
  (`packages/ui/src/features/conversation/RichMessageText.tsx:178`). The mobile shows the text of the
  quote in quotation marks
  (`apps/mobile/src/features/chat/model/chat-markdown-parser.ts:39`).
  Size: **M**.
- **The grouped messages.** The desktop groups the messages of one author
  (`src/renderer/src/features/conversation/chat-grouping.ts:21`). The mobile draws one bubble for
  each message.
  Size: **S**.


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
- Run only narrow checks, one at a time: `bun run --cwd apps/mobile typecheck` and the mobile script you change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
