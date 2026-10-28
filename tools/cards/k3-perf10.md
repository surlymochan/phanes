# M10 #61: sidebar draws every chat row (perf 10)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Renderer performance finding 10 below. Keep keyboard behavior, aria names and the Alt+Arrow reorder that other runs added (do not remove roles or labels). Keep output identical. Add a measurement script under scripts/ (or extend scripts/renderer-conversation-perf-e2e.ts) with the median before/after over at least 5 runs on a sidebar with 500 chats; revert if not clearly faster.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 10. The sidebar draws every chat row, and every row carries an avatar engine

**Evidence**

- `packages/ui/src/features/sidebar/SidebarAgentSection.tsx:46` — `For each={chats()}`
  over every chat of the section, with no virtualizer.
- `packages/ui/src/features/sidebar/SidebarPeopleSection.tsx:49` — the same for people.
- `packages/ui/src/features/sidebar/SidebarPinnedGroup.tsx:60` and
  `SidebarHiddenGroup.tsx:58` — the same for pinned and hidden rows.- `packages/ui/src/features/agents/AgentAvatar.tsx:348-360` — each row mounts a `BloubBot`,
  which owns an animation engine.
- `packages/ui/src/components/VirtualListbox.tsx` and
  `packages/ui/src/components/createListVirtualizer.ts` — the shared list virtualizer exists
  and is used only by the global search.

**Why it matters**

The sidebar is not virtualized, so a workspace with many agents mounts every row and
every avatar engine. The avatar code even carries a comment about this
(`AgentAvatar.tsx:44-50`: "The sidebar is not virtualized, so every row would otherwise
animate on screen").

**Fix**

Virtualize the section lists with the shared `createListVirtualizer`, as the global search
does. Keep the avatar work gated behind the virtualizer so off-screen rows never mount an
engine.

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
- Run only narrow checks, one at a time: `bun run typecheck:renderer` and your measurement script, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
