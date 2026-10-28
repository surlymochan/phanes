# M10 #64/#63: browser tab sweep overlap (reliability 15) and error codes in Team API error bodies (errors 10)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Two findings below; fix BOTH before you finish. (1) Reliability 15: the browser tab sweep can run twice at the same time (src/backend/browser-host.ts; your base dd538d4a78 already has your earlier reliability fixes). (2) Errors 10: most Team API routes send an error body without a code. Released Team API v1-v3 wire meaning must not change (AGENTS.md non-negotiable): keep every status code and every existing field and its meaning; you may ADD one optional field with the public error code to error bodies, and old clients must keep working unchanged. Prove both with scripts/team-api-routes-e2e.ts and scripts/team-api-legacy-clients-e2e.ts (legacy clients must still pass unchanged) plus an E2E case for the sweep.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 15. The browser tab sweep can run twice at the same time

Size: M.

Evidence:

- `src/backend/browser-host.ts:387` — `}, IDLE_TAB_SWEEP_MS);`
- `src/backend/browser-host.ts:383` — `this.#idleTabSweep = setInterval(() => {`
- `src/backend/browser-host.ts:384` — `void runCauseEffect(this.#sleepIdleTabs()).catch((error) =>`
- `src/backend/browser-host.ts:221` — `const IDLE_TAB_SWEEP_MS = 60_000;`

Why it matters: the interval starts a new sweep and does not wait for the last one. A sweep that
takes more than a minute, which a slow page load can cause, overlaps with the next one. Two
sweeps then read the same tab and can put it to sleep twice. The second sleep then works on a tab
that the first one already unloaded.

Fix: keep a flag that says that a sweep runs. Skip the tick when the flag is set, and clear the
flag when the sweep ends. A `Semaphore` with one permit does the same.


### 10. The Team API error body has no code for most routes

**Size: L — this touches a released wire shape, so it needs a developer decision**

Evidence:

- `src/main/team-api-server.ts:868` — `const code = error instanceof RemoteScreenError ? error.code : undefined;`
- `src/main/team-api-server.ts:876` — only the remote-screen route sends a `code`.
- `src/main/remote-server-http.ts:111-112` — the client reads `value.code` and puts it into `RemoteRequestError`.

Why it matters: a client can only tell "not signed in" from "no permission" from "server fault" by
matching English text. It cannot react to a code. The `code` field already exists in the envelope,
so the host only has to fill it.

Fix: fill the `code` for every route with the failure kind, for example `agent_install_failed`.
Adding a value to an optional field keeps the released shape valid. This is a wire change, so it
needs the developer decision that `packages/contracts/AGENTS.md` asks for.


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
- Run only narrow checks, one at a time: `bun run typecheck:node`, `bun run test:team-api-routes`, `bun run test:team-legacy-clients` and the sweep E2E you add, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
