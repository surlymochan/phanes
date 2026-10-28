# M10 #65: mobile composer skill picker and context usage meter (mobile 10, 12)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Mobile parity findings 10 and 12 below, in apps/mobile (Expo / React Native; read apps/mobile/AGENTS.md). Work in the composer and its header area only; another run changes the chat message list (paused marker, process notes), so do not edit message rows. Reuse the Team API calls and contracts that the desktop already uses for skills and context usage; do not add or change a wire protocol. User text from @openbot/i18n keys. Add or extend a mobile E2E or regression script under scripts/ with a case for each finding.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 10. The composer has no skill picker

- Desktop: `packages/ui/src/features/conversation/ComposerEditor.tsx:70` takes the skills of the host
  and `:72` takes the MCP servers. Both answer to the `$` key in the composer.
- Mobile: `apps/mobile/src/features/chat/components/chat-composer.tsx:128` takes only the agents for
  the `@` key. There is no `$` key.
- Why it matters: on the desktop the user names a skill in a message to load it. On the phone there
  is no way to name a skill.
- Fix: add a `$` picker to the composer of the mobile app. The skills of an agent are already read by
  the app (`apps/mobile/src/features/workspace/context/host-request-actions.ts:419`). Send the name as
  a skill tag of `packages/contracts/src/chat-tag-references.ts:11`, and not as plain text.
- Size: **M**.

### 12. The context usage meter is missing

- Desktop: `packages/ui/src/features/conversation/ContextUsage.tsx:75` draws the ring of the
  composer, and `src/renderer/src/features/conversation/ConversationComposer.tsx:694` places it. The
  popover shows the fill of the thread window and the moment of the compaction.
- Mobile: the app has no meter and no compaction notice. The value comes from the provider runtime
  (`src/backend/claude-client.ts:1060`), and the Team API has no route for it.
- Why it matters: the user does not see that the thread is nearly full, or that the agent compacted
  it. The desktop tells the user before the agent loses the older messages.
- Fix: add a host route that reads the context usage of an agent, and draw the ring in the composer
  of the mobile app. The values of the categories of the desktop popover come from
  `src/backend/claude-context-categories.ts:6`.
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
