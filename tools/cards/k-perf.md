# M10 #61: fix renderer performance findings 1-2

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Base commit: b50d5ce925 (head of PR nibiru-labs/openbot#42,
the upstream sync). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Renderer performance findings 1 and 2 below (conversation snapshot projection with JSON.stringify comparisons; streaming reply re-parses its whole body on every revealed word). Files: src/renderer only (for example app-message-projection and the streaming text/markdown path). Keep output identical. Add an E2E or measurement script under scripts/ that shows the before/after cost on a long synthetic thread and writes a JSON report under .openbot-build/.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 1. A conversation snapshot re-projects the whole thread and compares every message with ten `JSON.stringify` calls

**Evidence**

- `src/renderer/src/app-message-projection.ts:240-266` — `agentMessagesEqual` compares
  `reactions`, `reactionSummary`, `attachments`, `questionPrompt`, `exchange`, `routine`,
  `actionMarker`, `items`, `itemIds` and `plan` with `JSON.stringify` on both sides.
- `src/renderer/src/features/conversation/conversation-context.tsx:553` — `applyConversation`
  calls `toAgentMessages(sourceMessages, …)` on the whole snapshot.
- `src/renderer/src/features/conversation/conversation-context.tsx:565` and `:642` — the
  equality check runs for every message of every snapshot and every page.
- `src/renderer/src/app-message-projection.ts:86` — each converted message calls
  `formatMessageTime`, which formats a `Date` through `Intl`
  (`packages/ui/src/features/conversation/chat-timestamp.ts:11-21`).
- `src/renderer/src/features/conversation/conversation-context.tsx:336-347` — snapshots are
  applied on an animation frame, so this cost lands in a frame the reader sees.

**Why it matters**

For the active agent the whole snapshot is converted, not a tail
(`conversation-context.tsx:544-547` only trims for agents that are *not* open). A chat with
1000 loaded messages therefore costs 1000 message objects, 1000 `Intl` date formats and
10 000 `JSON.stringify` calls on one frame. The user sees the chat freeze when a turn
finishes, when the host reconnects, or when a page is merged.

**Fix**

Compare the stored message and the mapped message field by field instead of by
`JSON.stringify`, with an identity check first. Keep the stored `time` string when
`createdAt` has not changed, so the `Intl` format happens once per message and not once per
snapshot. Skip the conversion entirely for a message whose id, revision and body are
unchanged.

**Size** M

### 2. A streaming reply re-parses its whole body on every revealed word

**Evidence**

- `packages/ui/src/features/conversation/MessageRendering.tsx:321-330` — the
  `contentBlocks` memo calls `sharedContentBlocks(streamedBody(), …)` on every change.
- `packages/ui/src/features/conversation/DataTable.tsx:136-174` — `messageContentBlocks`
  splits the body into lines and runs the code-fence and table regexes over every line.
- `packages/ui/src/features/conversation/MessageRendering.tsx:142-175` — the reveal step runs
  about every `--stream-gap` (40 ms by default), so roughly 25 times a second.

**Why it matters**

The cost per step is O(body length), so a reply of 20 000 characters costs about 400 000
character scans over the reply, on the main thread, while the reader is watching the text
appear. The markdown lexer above it already solved this with an incremental tail
(`MarkdownMessageText.tsx:154-186`); the block splitter did not.

**Fix**

Give `messageContentBlocks` the same incremental behaviour: keep the settled prefix of the
blocks and only re-scan from the last settled block, exactly as `lexStreamingTokens` does
for the markdown tokens.

**Size** M

## Rules

- First confirm each finding against the current code. If upstream already fixed one, do not change it: report
  "already fixed" with the file and line that show it. That is a valid result.
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`.
- Do not change released Team API wire contracts, IPC contract names, or migrations.
- Stay inside this worktree. Do not use GUI apps or Computer Use. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: `bun run typecheck:renderer` and your measurement script, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
