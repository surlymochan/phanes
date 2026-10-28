# M10 #66: E2E for importing a Grok Bot export (tests 2)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Test-coverage finding 2 below. First check existing import scripts under scripts/ (grep for agent-import-service and legacy-import) and report what they cover. Add one new E2E script for the uncovered path: build a small synthetic Grok Bot export fixture under .openbot-build/, import it through the real import service, and read the result back. No product code change unless a test exposes a real bug; then report it instead of fixing it. Temporary files only under $TMPDIR or .openbot-build/ in the worktree; never touch /tmp.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 2. Importing a Grok Bot export has no test

**Evidence**

- `README.md:286` describes the flow. It adds each agent's name, instructions, avatar, skills,
  routines, and memories, and copies the workspace files to `imported/`.
- `src/main/agent-import-service.ts:152` is `AgentImportService`.
- `src/backend/agent-store.ts:149` names the released command `legacy-import:bots:v1`, and
  `src/backend/agent-store.ts:266` writes the imported agents.
- No script imports `agent-import-service`.

**Why it matters**

The import writes agents, skill revisions, routines, memories, and files. A defect can duplicate an
agent, drop a skill, or lose a memory. A user has one export file and no other way to move an agent
set to OpenBot.

**Fix**

Add `scripts/agent-import-e2e.ts`. It builds a zip with the export shape and runs the real
`AgentImportService` against a temporary profile. It checks the agent rows, the skill revisions, the
routine rows, the memory rows, the copied workspace files under `imported/`, and that a second import
of the same zip does not duplicate an agent. Cover the legacy `bots.json` path at
`src/backend/agent-store.ts:266` in the same script.

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
