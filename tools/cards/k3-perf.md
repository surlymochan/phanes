# M10 #61: renderer performance findings 13-15

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Renderer performance findings 13, 14 and 15 below (WorkspaceConversation allocations per prop read, composer key strings per keystroke, rows that scan the whole agent list). Files: only the components these findings name; do not touch app-message-projection.ts, MessageRendering.tsx, message-content-blocks.ts, DataTable.tsx or SidebarNav (other runs own them). Keep output and behavior identical.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 13. `WorkspaceConversation` allocates a new array and a new function on each prop read

**Evidence**

- `src/renderer/src/features/conversation/WorkspaceConversation.tsx:412` —
  `mentionAgents={agentList().filter((agent) => !userHiddenAgentIds().has(agent.id))}`. The
  compiler turns this into a getter, so every read of the prop filters the agent list
  again and returns a new array.
- `src/renderer/src/features/conversation/WorkspaceConversation.tsx:442` — `isOwnSender` is
  a new arrow function on every read.
- `src/renderer/src/features/conversation/WorkspaceConversation.tsx:417` —
  `messageReferences` returns a new `{}` when the agent has no references.
- `src/renderer/src/features/conversation/ConversationTimeline.tsx:180-196` — `otherSender`
  and `memberAuthor` read `props.isOwnSender` and scan `props.presence.members` per row.

**Why it matters**

`mentionAgents` filters the whole agent list on each read, and the composer reads it. The
new function identity per read makes the per-row `otherSender` chain in the timeline
re-run more often than the values change.

**Fix**

Wrap the derived values in `createMemo` in `WorkspaceConversation`, and pass a stable
callback for `isOwnSender`. A memo keeps one array identity for as long as its inputs hold.

**Size** S

### 14. The composer rebuilds three key strings on every keystroke

**Evidence**

- `packages/ui/src/features/conversation/ComposerEditor.tsx:280-313` — the effect reads
  `props.value`, so it runs on every keystroke.
- `packages/ui/src/features/conversation/ComposerEditor.tsx:307-311` — `attachmentKey`,
  `skillKey` and `mcpKey` are joined from every attachment, skill and MCP server on each
  run, only to compare them with the previous run.

**Why it matters**

The cost is O(attachments + skills + MCP servers) string building per character typed. With
dozens of skills and servers installed, this is work the user feels as typing lag in a
busy workspace.

**Fix**

Compare the arrays by identity instead of by a joined string, or compute the keys only
when the arrays change identity.

**Size** S

### 15. Rows look up their agent by scanning the whole agent list

**Evidence**

- `packages/ui/src/features/conversation/ChatMessageRow.tsx:228` — a reaction by an
  agent runs `props.agents.find(…)` per reaction.
- `packages/ui/src/features/conversation/ChatActionMarker.tsx:260` —
  `agentIds().map((agentId) => props.agents.find(…))` runs per marker read.
- `packages/ui/src/features/conversation/ChatActionMarker.tsx:452`, `:643`, `:647`, `:670`,
  `:677` and `:688` — more `props.agents.find(…)` calls in the same component.

**Why it matters**

Each lookup is O(agents). A marker row with several recipients walks the list several
times, and the timeline has one marker row per action.

**Fix**

Build one `Map` of agent id to agent once per agent list and read from it. The map can
live in the agent context, next to the list it describes.

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
- Run only narrow checks, one at a time: `bun run typecheck:renderer`, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
