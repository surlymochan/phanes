# M10 #64: backend reliability findings 12-14

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Backend reliability findings 12, 13 and 14 below (slow providers are never forgotten; shown configuration warnings are kept for the life of the app; MCP shape command lookup cache has no limit). Bound each with a clear rule (size or age) and keep current behavior otherwise. Do not touch src/backend/browser-host.ts, local-mcp-bridge.ts or dispatch code (another run owns them). Add or extend an E2E script case for each fix.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 12. The slow providers are never forgotten

Size: S.

Evidence:

- `src/backend/agent/provider-runtime.ts:418` — `readonly #slowStarts = new Set<AgentProvider>();`
- `src/backend/agent/provider-runtime.ts:2789` — `if (stage === "initialize") this.#slowStarts.add(provider);`

Why it matters: one timeout puts the provider in the set for the life of the runtime. Every later
start then waits 180 seconds. The set is never cleared, not even after a provider that ran well
for a long time.

Fix: remove the provider from `#slowStarts` when a start answers `initialize`. Then only the
current state of the provider decides the budget.

### 13. The shown configuration warnings are kept for the life of the app

Size: S.

Evidence:

- `src/backend/agent/provider-runtime.ts:431` — `readonly #reportedConfigWarnings = new Set<string>();`
- `src/backend/agent/provider-runtime.ts:3031` — `this.#reportedConfigWarnings.add(redacted);`

Why it matters: the set holds one entry per distinct warning text. It is never cleared. A provider
that quotes a changing part of its warning, such as a path with a number in it, adds a new entry at
each start.

Fix: keep a bounded set, and drop the oldest entry when the set is over the bound.

### 14. The command lookup cache of the MCP shapes has no limit

Size: S.

Evidence:

- `src/backend/mcp-provider-shapes.ts:289` — `const resolvedCommands = new Map<string, Deferred.Deferred<string | null, McpShapeFailed>>();`
- `src/backend/mcp-provider-shapes.ts:318` — `export function clearMcpCommandCache(): void {`

Why it matters: the key holds the command and the PATH. The map keeps one entry per pair, and only
`clearMcpCommandCache` removes them. A PATH that changes, or a user with many MCP servers, grows the
map for the life of the app.

Fix: bound the map, and drop the oldest entry when the map is over the bound. A command that is no
longer on the disk already removes its own entry.


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
- Run only narrow checks, one at a time: `bun run typecheck:node` and the E2E scripts you add or change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
