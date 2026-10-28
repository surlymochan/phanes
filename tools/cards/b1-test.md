# M10 #66: E2E for channel chat Stop, Resume, Reassign, Archive, Restore (tests 3)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Test-coverage finding 3 below. First check the existing channel scripts (scripts/channel-*-e2e.ts, scripts/channel-e2e-support.ts) and report what they already cover. Add one new E2E script for the uncovered controls, reusing channel-e2e-support.ts. No product code change unless a test exposes a real bug; then report it instead of fixing it.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 3. The channel chat controls have no test

**Evidence**

- `README.md:27` promises shared desktop channel chats with Stop, Resume, Reassign, Archive, and
  Restore.
- `src/backend/channel-service.ts:110` is `ChannelService`. `src/backend/channel-service.ts:339`
  handles archive and restore. `src/backend/channel-service.ts:498` handles reassign.
  `src/backend/channel-service.ts:183` holds a retired channel.
- `src/backend/channel-store.ts:112` reads the archived IDs.
- No script imports `channel-service`.

**Why it matters**

The shared channel chat is a headline feature. Archive must stop new work and restore must bring the
channel back, and a reassign must move one task and keep the rest. A defect leaves a channel that
cannot be resumed, or a task that runs under the wrong agent.

**Fix**

Add `scripts/channel-service-e2e.ts`. It runs the real `ChannelService` on a temporary profile with a
fake provider and a fake mailbox. It creates a channel with two members, archives it and checks that
a new task is refused, restores it and checks that a task runs again, stops a run and resumes it, and
reassigns one task and checks that the other task keeps its owner. It writes
`.openbot-build/channel-service-e2e/report.json`.

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
