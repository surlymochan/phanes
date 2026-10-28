# M10 #63: provider status messages and the hourly provider update check (errors 5, 7)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Error-handling findings 5 and 7 below: two provider status messages bypass the catalog; the hourly provider update check swallows every failure. Map the messages to @openbot/i18n catalog keys (every locale) and make the update-check failure visible (log with a code and, where the audit says so, a status the UI already shows). Do not change provider start timeouts or account-read logic (other runs changed them). Add or extend an E2E script case for each finding.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 5. Two provider status messages bypass the catalog

**Size: S**

Evidence:

- `src/backend/agent/provider-runtime.ts:3076` — `` `${providerLabel(client.provider)} stopped repeatedly. Restart OpenBot after checking the CLI.` ``
- `src/backend/agent/provider-runtime.ts:3096` — `` `${providerLabel(client.provider)} stopped. Retrying (${attempts + 1}/3)…` ``
- `src/backend/agent/provider-runtime.ts:3134-3136` — the same file uses `sourceText("error.provider.startStopped", …)` for the third message.

Why it matters: both strings reach the provider card in the UI. They are English only for the nine
other locales. The retry count in the text also makes the string change, so it can never match a
catalog template either.

Fix: add `error.provider.stoppedRepeatedly` and `error.provider.restarting` to
`packages/i18n/src/messages/en/error/provider.ts`, with a `{provider}` and an `{attempt}`
placeholder. Run `bun scripts/i18n-check.ts` after the change.

### 7. The hourly provider update check swallows every failure

**Size: S**

Evidence:

- `src/main/provider-runtime-manager.ts:353` — `const check = () => void runCauseEffect(this.checkForUpdates()).catch(() => undefined);`
- `src/main/provider-runtime-manager.ts:357-380` — `#runCheck` fills `#blocked` (the blocked-version list) and `#latest` (the newest release).

Why it matters: when the release feed cannot be read, the blocked-version list stays stale, the
"update available" badge never appears, and nothing is logged. The user cannot tell that the check
is broken from the app.

Fix: log the failure with `logger.warn`, and record the last failed check in the snapshot so the
provider card can show it.


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
- Run only narrow checks, one at a time: `bun run typecheck:node`, `bun run i18n:check` and the E2E script you add or change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
