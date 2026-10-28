# M10 #64: fix backend reliability findings 6-8

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Backend reliability findings 6, 7 and 8 below (slow provider start budget lost at every restart; dispatch run records grow without limit; script origins kept for the life of the app). Your base already contains your fixes for findings 1-5. Bound what grows; persist only what the finding needs, without a database migration (migrations are out of scope; report if one is required). Add or extend an E2E script for each fix.

## Findings (from an audit written before the upstream sync; references may have moved)

### 6. The long start budget of a slow provider is lost at every restart of the app

Size: M.

Evidence:

- `src/backend/agent/provider-runtime.ts:100` — the comment names the Windows Gemini server that unpacks about 310 MB
- `src/backend/agent/provider-runtime.ts:418` — `readonly #slowStarts = new Set<AgentProvider>();`
- `src/backend/agent/provider-runtime.ts:963` — `yield* this.#connect(` at the launch
- `src/backend/agent/provider-runtime.ts:2725` — `options.startTimeoutMs ?? (requestedProviders.length === 1 && this.#slowStarts.has(provider) ? LONG_START_TIMEOUT_MS : undefined)`

Why it matters: the set of the slow providers lives in memory only. A restart of the app loses it.
The launch connects every provider at once, so `requestedProviders.length` is not one and the long
budget never applies at a launch. The provider that needs a long start therefore fails at every
launch with an error that the user sees. The retry after that failure gets the long budget, so the
provider recovers, but the user sees the error first. On the computer that the comment names, this
happens at every start of the app.

Fix: keep the record of a slow provider across restarts. The provider store or a small file in the
user data folder can hold it. Then the first start of the day gets the long budget.

### 7. The records of the dispatch runs grow without a limit

Size: M.

Evidence:

- `src/backend/dispatch/dispatch-service.ts:220` — `#runs: DispatchRecord[] = [];`
- `src/backend/dispatch/dispatch-service.ts:369` — `const saved = yield* Effect.result(this.#save([...this.#runs, record], output.exitCode));`

Why it matters: every dispatch adds a record, and the records are never removed. The whole array
is written to `openbot-dispatch-v1.json` on each change. The file therefore grows with each
dispatch, and each write costs the size of the whole file. The array also holds every record in
memory for the life of the app.

Fix: keep a bounded number of records. Move the old ones to a second file, or drop the records
that reached a terminal state and were delivered.

### 8. The origins where an agent ran a script are kept for the life of the app

Size: S.

Evidence:

- `src/backend/browser-host.ts:338` — `readonly #agentScriptedOrigins = new Set<string>();`
- `src/backend/browser-host.ts:2631` — `if (origin) this.#agentScriptedOrigins.add(origin);`
- `src/backend/browser-host.ts:1067` — the only read of the set

Why it matters: the set has no removal. Every origin where an agent ran `evaluate` stays for the
life of the process. A long session that visits many sites grows the set without a limit.

Fix: keep the set with a bound, and drop the oldest origin when the set is over the bound. A last
seen order is enough for the read at `src/backend/browser-host.ts:1067`.

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
