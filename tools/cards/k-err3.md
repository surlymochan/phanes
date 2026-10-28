# M10 #63: bounded hosted-server start retry and visible dead CDP sessions (errors 12, 14)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Error-handling findings 12 and 14 below. Your base already contains your fixes for findings 1-3. Preserve existing deadlines and retries where AGENTS.md requires it; bound only what the finding shows is unbounded, and make the failure visible with a catalog message or a log with a code. Add or extend an E2E script for each fix.

## Findings (from an audit written before the upstream sync; references may have moved)

### 12. The hosted server start retry runs forever and only logs

**Size: M**

Evidence:

- `src/main/hosted-server-start-retry.ts:13` — `const START_RETRY_DELAYS_MS = [30_000, 60_000, 2 * 60_000, 5 * 60_000, 10 * 60_000];`
- `src/main/hosted-server-start-retry.ts:80` — `Math.min(this.#startFailures, START_RETRY_DELAYS_MS.length - 1)`, so the delay stays at 10 minutes and `#startFailures` keeps counting.
- `src/main/application-services.ts:2504` — `onError: (message, error) => logger.warn(message, toLogValue(error));`

Why it matters: a hosted server whose sign-in keeps failing retries every 10 minutes for the life
of the app. The only trace is a log line, so the user sees a host in the "error" phase with no
reason and no code.

Fix: stop after a number of attempts, or after the phase has been `error` for a set time, and
surface the last failure through the host issue so it reaches the user.

### 14. The browser CDP release calls hide a dead session

**Size: M**

Evidence:

- `src/backend/browser-cdp.ts:753, 1737, 2079, 2092, 2116, 2158, 2352, 2586` — `Effect.ignore` on `Runtime.releaseObject`.
- `src/backend/browser-cdp.ts:1165, 1185, 1241` — `Effect.ignore` on `Input.setInterceptDrags`.
- `src/backend/browser-cdp.ts:1776, 1777` — `Effect.ignore` on `Network.setCacheDisabled` and `Network.disable`.
- `src/backend/browser-cdp-values.ts:9-13` — `send` fails with a `BrowserOperationError`, so `Effect.ignore` drops a real failure.

Why it matters: the first ignored failure is often the sign that the CDP connection is gone. After
it, the snapshot code keeps using a stale session and the browser panel shows an empty page
instead of saying that the browser is not connected.

Fix: on the first ignored failure, mark the capture session as closed and let the caller report
it. Keep the ignore for the calls after that.

## Rules

- First confirm each finding against the current code. If upstream already fixed one, do not change it: report
  "already fixed" with the file and line that show it. That is a valid result.
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`.
- Do not change released Team API wire contracts, IPC contract names, or migrations.
- Never read or print `.env*`, `.env.keys`, `.dev.vars`, auth or credential files; the risk guard stops the run. Read one file per command.
- Stay inside this worktree. Do not use GUI apps or Computer Use. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: the typecheck project for the code you change and the E2E scripts you add or change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
