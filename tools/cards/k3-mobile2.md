# M10 #65: mobile interactive prompt block and in-chat search (mobile 1, 14)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Mobile parity findings 1 and 14 below, in apps/mobile (read apps/mobile/AGENTS.md). HARD RULE: client-side only. A result that changes any file under packages/contracts, packages/team-client, src/main or src/backend is rejected as a whole. Use only Team API routes and contracts that already exist at your base. If a finding needs a new route, report it as not fixed and explain what route it would need. Do not edit the composer or the message-list separators (other runs own them). User text from @openbot/i18n keys. Add or extend a mobile script under scripts/ with a case for each finding.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 1. The interactive block of an agent prompt is dropped

- Desktop: `src/renderer/src/features/conversation/ConversationTimeline.tsx:830` draws a
  `UiBlockPrompt`, and `packages/ui/src/features/conversation/ui-blocks/UiBlockingBlock.tsx:16`
  draws the block. The block kinds are `confirm`, `quick_replies`, `choice` and `form`
  (`packages/contracts/src/ui-blocks.ts:205`).
- Mobile: `apps/mobile/src/features/chat/model/chat-messages.ts:29` has no `uiBlock` kind in
  `ChatMessage`, and `projectChatMessages` has no branch for `message.uiBlock`. It keeps only the
  `questionPrompt` branch at `apps/mobile/src/features/chat/model/chat-messages.ts:347`.
- The block arrives on the phone. The client advertises the capability
  (`packages/contracts/src/team-protocol/current.ts:159`, sent by
  `packages/team-client/src/remote-peer.ts:1352`), the host sends the block with the message
  (`packages/contracts/src/team-protocol/ui-blocks-v1.ts:20`), and the mobile decoder keeps the
  field (`apps/mobile/src/features/workspace/model/conversation.ts:28`,
  `packages/contracts/src/ipc-conversation-messages.ts:218`). The projection then drops it.
- Why it matters: an agent that asks a question with a card shows the card on the desktop. The phone
  shows the fallback questions of the card as a plain list of options. The user loses the layout, the
  fields of a form, the held button of a dangerous action, and the stored outcome of the block.
- Fix: add a `uiBlock` kind to `ChatMessage` in
  `apps/mobile/src/features/chat/model/chat-messages.ts`. Draw it with a new component in
  `apps/mobile/src/features/chat/components/` that uses the kinds of
  `packages/contracts/src/ui-blocks.ts`. Answer it with the same route that the question prompt uses
  (`respondToPrompt`), and read the answer back with `uiBlockAnswersFromResponse` and
  `uiBlockOutcomeText`. Keep the fallback path for a host without the capability.
- Size: **L**.

### 14. The open chat has no search

- Desktop: `src/renderer/src/features/conversation/ConversationTimeline.tsx:268` opens the search box
  of the open chat, and `packages/ui/src/features/conversation/ChatSearch.tsx:15` draws it. The
  matches are in the text of the messages
  (`src/renderer/src/features/conversation/chat-search.ts:138`).
- Mobile: the app has a global search (`apps/mobile/src/features/search/model/mobile-search.ts:1`)
  that finds a message across all agents. The open chat has no search.
- Why it matters: the global search needs the host and returns a page of results. The search of the
  open chat finds a word in the chat that the user reads now, and moves between the matches.
- Fix: add a search field to the chat header or the chat screen. Search the loaded messages of the
  chat in the app, and highlight the matches in the bubble. The desktop search uses the CSS
  Highlights API of the browser, which the phone does not have, so the phone needs its own
  highlighting in the text of the bubble.
- Size: **M**.


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
- Run only narrow checks, one at a time: `bun run --cwd apps/mobile typecheck` and the mobile script you add or change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
