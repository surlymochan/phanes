# M10 #66: E2E for exporting the local data (tests 5)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Test-coverage finding 5 below. scripts/diagnostics-export-e2e.ts already covers the diagnostics export; check it and any backup/export scripts first and report what they cover. Add one new E2E script for the uncovered local data export path through the real maintenance service, and assert that secrets are redacted in the exported files (AGENTS.md: redact secrets on every export path). No product code change unless a test exposes a real bug; then report it. Temporary files only under $TMPDIR or .openbot-build/.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 5. The data export and the diagnostics export have no test

**Evidence**

- `README.md:31` promises local data and privacy-safe diagnostics exports from the account menu.
- `src/main/maintenance-service.ts:31` is `exportOpenBotData`. `src/main/maintenance-service.ts:131`
  is `exportDiagnostics`.
- The manifest carries `schemaVersion: 4` at `src/main/maintenance-service.ts:70`, and the comment
  at `src/main/maintenance-service.ts:67` says that a wrong number gives a reader the wrong shape.
  The diagnostics report has the same version at `src/main/maintenance-service.ts:162`.
- No script imports `maintenance-service`.

**Why it matters**

The zip is the only backup the user owns. A defect can write a zip that no reader understands, or
drop the attachments. The diagnostics report is sent to somebody else, so it must not carry a
conversation, a path, or a token.

**Fix**

Add `scripts/data-export-e2e.ts`. It builds a profile with two agents, conversations, queues, and
attachments. It calls `exportOpenBotData` with a stubbed save dialog, unpacks the zip, and checks the
manifest version, the agent list, the conversation pages, the queue rows, and the attachment files.
Then it calls `exportDiagnostics` and checks that the JSON holds no conversation text, no file path,
no token, and no raw error message.

**Size: M**

---


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
- Run only narrow checks, one at a time: `bun run typecheck:node` and `bun scripts/<your new script>.ts`, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
