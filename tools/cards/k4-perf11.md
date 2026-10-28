# M10 #61: file list draws every file row (perf 11)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Renderer performance finding 11 below. Keep keyboard behavior and accessibility. Keep output identical. Add a measurement script under scripts/ with the median before/after over at least 5 runs on a list of 5,000 files; revert if not clearly faster.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 11. The file list draws every file row

**Evidence**

- `packages/ui/src/features/files/FileList.tsx:247` — `For each={group.files}` over
  every file of every group.
- `packages/ui/src/features/files/FileList.tsx:340` — the metadata row of every file.
- `packages/ui/src/features/files/AgentFilesView.tsx:85` and `:119` — the same for the
  agent view.

**Why it matters**

A chat that produced hundreds of files renders hundreds of rows, each with a date format
(`FileList.tsx:254-260`) and a preview button. The panel opens over the chat, so the cost
lands on the frame the reader opens it.

**Fix**

Virtualize the group list with the shared list virtualizer.

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
