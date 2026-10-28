# M10 #61: renderer performance findings 3, 6 and 7

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Renderer performance findings 3, 6 and 7 below (timeline rebuilds rows and index map on every change; chat stops virtualizing at 100 messages; content-block cache holds one body). Your base 8c0047e48b has your finding-1 fix. Do not touch the components of findings 13-15 (WorkspaceConversation, composer, agent lookup in rows: another run owns them). Keep output identical. Extend scripts/renderer-conversation-perf-e2e.ts with a measurement for each fix and report median before/after over at least 5 runs; a change that is not clearly faster is reverted.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 3. The timeline rebuilds its rows and its index map over every message, on every change

**Evidence**

- `src/renderer/src/features/conversation/stores/scroll-store.ts:96-115` — `timelineMessages`
  runs `groupAgentMessageMarkers`, `groupRoutineRunMarkers`, `summarizeRoutineRunMessages`,
  `placeRoutineRunTurns` and `expandRoutineRunGroups` over the loaded messages.
  The passes live in `src/renderer/src/features/conversation/agent-message-timeline.ts:21-58`
  and `src/renderer/src/features/conversation/routine-run-timeline.ts:39-72`, `:118-145`,
  `:170-196`, `:222-258`.
- `src/renderer/src/features/conversation/stores/scroll-store.ts:117-119` —
  `timelineIndexById` builds a new `Map` of every message id.
- `src/renderer/src/features/conversation/ConversationTimeline.tsx:337` — every visible row
  reads that map through `createMemo(() => timelineIndexById().get(virtualRow.key))`, so a
  new `Map` re-runs the memo of every row on screen.
- `src/renderer/src/features/conversation/stores/scroll-store.ts:121-124` — `timelineRows`
  maps every message a second time, for the new-message tally.

**Why it matters**

Every arrival of a message, every page load and every end-of-stream flag re-runs five to
seven passes over the loaded transcript and rebuilds a map of the same size. The map
re-runs the index memo of each visible row, so the reader sees a frame of extra work for
each new message in a long chat.

**Fix**

Key the derived rows on the message id list, so an unchanged list reuses the previous rows
and the previous map. Build the index map incrementally, or keep the map and update only
the entries that changed.

**Size** M

### 6. The chat stops virtualizing at 100 messages, so the common chat renders every row

**Evidence**

- `packages/ui/src/features/conversation/createChatVirtualizer.ts:38` —
  `const STATIC_CHAT_LIMIT = 100`.
- `packages/ui/src/features/conversation/createChatVirtualizer.ts:96-98` — under the limit,
  `refresh` uses `fallbackItems` instead of the measured items.
- `packages/ui/src/features/conversation/createChatVirtualizer.ts:241` — `fallbackItems`
  starts at index 0 when the count is 100 or less, so every row is returned.
- `packages/ui/src/features/conversation/createChatVirtualizer.ts:173` —
  `isVirtualized()` reports false under the limit, and
  `src/renderer/src/features/conversation/ConversationTimeline.tsx:330-332` then drops the
  fixed-height container.

**Why it matters**

Ninety-nine messages is an ordinary chat, not an edge case. Each row carries a full
markdown render, a code highlight and an action toolbar, so the chat is at its heaviest
exactly when a user is reading a busy thread. The measurement shortcut has a real cost,
but the cliff is at a size most chats reach.

**Fix**

Lower the limit (20 rows is enough for the measurement to settle) or virtualize always and
keep the estimate for unmeasured rows. Measure the effect on a chat with 60 messages before
and after.

**Size** M

### 7. The content-block cache holds one body, so scrolling re-parses every row that remounts

**Evidence**

- `packages/ui/src/features/conversation/MessageRendering.tsx:41-52` —
  `lastContentBlocks` is a single module-level slot. A body that is not the last one read
  is parsed again.
- `packages/ui/src/features/conversation/MessageRendering.tsx:54-60` —
  `conversationBubbleVariant` reads the same slot, and
  `packages/ui/src/features/conversation/ChatMessageRow.tsx:108` calls it for every row.
- `packages/ui/src/features/conversation/DataTable.tsx:136-174` — the parse itself scans
  every line of the body.

**Why it matters**

The markdown lexer next door has a 200-entry LRU for exactly this reason
(`MarkdownMessageText.tsx:125-140`). With one slot, scrolling a chat remounts the rows
that come back into view and each one re-parses its body.

**Fix**

Replace the slot with an LRU keyed by body and streaming flag, with the same 200-entry
limit as the lexer cache.

**Size** S

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
