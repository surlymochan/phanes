# M10 #65: mobile skill and MCP tags, dropped markers, chat visual page (mobile 6, 7, 9)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Mobile parity findings 6, 7 and 9 below, in apps/mobile (read apps/mobile/AGENTS.md). Your base 3cb28d9f69 has your fix for finding 15. Client-side only: do NOT add or change any Team API route, capability, contract or IPC endpoint. If a finding needs one, report it as not fixed with the reason. Do not edit the composer (another run owns it). User text from @openbot/i18n keys. Add or extend a mobile script under scripts/ with a case for each finding.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 6. A skill tag and an MCP tag show as raw text

- Desktop: `packages/ui/src/features/conversation/RichMessageText.tsx:167` draws a skill chip, and
  `:171` draws an MCP chip.
- Mobile: `apps/mobile/src/features/chat/model/chat-markdown-parser.ts:29` reads the agent tag and
  `:39` reads the quote tag. The other kinds have no token, so the raw marker text stays in the
  bubble.
- The kinds are in `packages/contracts/src/chat-tag-references.ts:11`: `agent`, `skill`, `mcp` and
  `quote`.
- Why it matters: a message that names a skill or an MCP server shows `@[Name](skill:id)` on the
  phone. The text looks broken.
- Fix: add a `skill` and an `mcp` token to the parser in
  `apps/mobile/src/features/chat/model/chat-markdown-parser.ts`. Draw them as a small chip with the
  name, like the desktop. The name of a skill comes from the message text, as the desktop does it.
- Size: **S**.

### 7. The model switch, skill and marketplace markers are dropped

- Desktop: `src/renderer/src/app-message-projection.ts:309` makes the model switch marker, `:311`
  the skill marker and `:313` the marketplace marker.
- Mobile: `apps/mobile/src/features/chat/model/chat-messages.ts:362` drops every message of the
  `system` author. Only the routine, the context reset and the dispatch markers survive.
- The host writes these messages with readable text. `packages/contracts/src/ipc-conversation-events.ts:317`
  has `modelSwitchReason`, `packages/contracts/src/skill-conversation-events.ts:22` has
  `skillConversationEvent`, and `packages/contracts/src/marketplace-suggestion-events.ts:17` has
  `marketplaceSuggestionEvent`. The contract states that the text is a readable sentence, so that a
  client which does not know the item type still shows what happened. The mobile shows nothing.
- Why it matters: the user does not learn that the agent changed model for a usage limit, that a
  skill was installed, or that the agent suggests an app. The last one has a card on the desktop with
  an install action.
- Fix: add a `marker` kind to `ChatMessage` with the text of the host message. Add the three marker
  kinds to the projection. Draw the marketplace suggestion as a card that opens the install screen
  `apps/mobile/src/app/(app)/install-agent/index.tsx`.
- Size: **S**.

### 9. A chat visual page does not render

- Desktop: `src/renderer/src/features/conversation/ConversationTimeline.tsx:658` draws a
  `ChatVisual`, a page that an agent wrote, in a sandboxed frame.
- Mobile: the mobile app has no such frame. The projection only skips the message when it counts the
  readable messages (`apps/mobile/src/features/chat/model/chat-messages.ts:497`), so the message
  shows as a file attachment.
- Why it matters: an agent that draws a chart or a report gives a page on the desktop. The phone
  shows the HTML file of the page instead of the page.
- Fix: render the page in the WebView that the app already uses for a HTML preview
  (`apps/mobile/src/features/chat/components/html-preview.dom.tsx`). The page needs the same sandbox
  policy as the desktop frame. The desktop reads the page from the attachment
  (`src/renderer/src/features/conversation/chat-visual-url.ts:20`); the mobile must read the bytes of
  the attachment first, because the URL of the desktop points at the computer of the user.
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
