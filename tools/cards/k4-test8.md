# M10 #66: E2E for checking, downloading and relaunching for an update (tests 8)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Test-coverage finding 8 below. First check existing scripts (grep scripts/ for update-service and provider-update-after-app-upgrade) and report what they cover. Add one new E2E script that drives the real update service against a local fake update feed served inside the script (no network), covering check -> download -> ready-to-relaunch, and a failed download. Do not actually relaunch the test process; assert the relaunch request. No product code change unless a test exposes a real bug; then report it.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 8. The application update has no test

**Evidence**

- `README.md:566` says that installed builds check GitHub Releases for updates and expose download and
  restart controls in the account popover.
- `src/main/update-service.ts:165` is `UpdateService`. `src/main/host-update-coordinator.ts` and
  `src/main/host-update-relaunch.ts` own the host side.
- No script imports `update-service`.

**Why it matters**

Every release depends on this path. A defect leaves a user on an old build with no way to update.
The service also waits for an idle agent and cancels a running download, and both paths are hard to
get right.

**Fix**

Add `scripts/update-service-e2e.ts`. It runs the real `UpdateService` with a fake updater that
answers like `electron-updater`. The file names the subset it needs at `src/main/update-service.ts:35`.
The script checks: a check with no update, a check with an update, a download that fails, a download
that succeeds, a relaunch that waits for an idle agent, and a cancel. It writes
`.openbot-build/update-service-e2e/report.json`.

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
