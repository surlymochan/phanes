# M10 #64: fix backend reliability findings 4-5

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Backend reliability findings 4 and 5 below (the shared database queue has no limit; the settle of an unconfirmed turn is a detached fiber with a five minute sleep). Your base already contains your fixes for findings 1-3. Remember AGENTS.md: the database host must not import Electron; Effect cannot interrupt blocked SQLite; preserve existing deadlines. Add or extend an E2E script that shows each fix.

## Findings (from an audit written before the upstream sync; references may have moved)

### 4. The queue of the shared database has no limit

Size: S.

Evidence:

- `src/backend/agent-data/agent-database-supervisor.ts:54` — `readonly #queue: PendingRequest[] = [];`
- `src/backend/agent-data/agent-database-supervisor.ts:85` — `this.#queue.push(entry);`
- `src/backend/agent-data/agent-database-supervisor.ts:112` — `if (Date.now() - next.queuedAt > this.#queueWaitMs) {`

Why it matters: `send` never refuses a request. The queue grows with the size of the burst. The
"busy" answer only comes when the queue is drained, one statement at a time. A burst of agent
statements therefore holds every request and its closure in memory, and the caller waits far
longer than `queueWaitMs`. The rule of the repository asks that the work of one invocation belongs
to that invocation.

Fix: check the length of the queue and the age of the new entry in `send`. Answer the "busy"
failure at once when the queue is over its limit. A small limit, such as 64 entries, is enough.

### 5. The settle of an unconfirmed turn is a detached fiber with a five minute sleep

Size: S.

Evidence:

- `src/backend/agent/drain-scheduler.ts:46` — `const TURN_START_GRACE_MS = 5 * 60_000;`
- `src/backend/agent/drain-scheduler.ts:821` — `yield* Effect.forkDetach(`
- `src/backend/agent/drain-scheduler.ts:823` — `Effect.delay(TURN_START_GRACE_MS),`
- `node_modules/effect/dist/internal/effect.js:2439` — `forkDetach` makes a daemon fiber that no scope owns
- `node_modules/effect/dist/internal/effect.js:2890` — the sleep of the clock uses a `setTimeout` without `unref`

Why it matters: the fiber belongs to no scope. No shutdown path cancels it. Its sleep keeps a timer
in the event loop for five minutes. A server or a command line process then waits for that timer
before it can exit. The rule of the repository asks that background work belongs to its invocation.

Fix: fork the settle into a scope that the drain scheduler owns, and cancel the fork when the
agent service stops. The grace timer can then be a timer that the same scope clears.

## Rules

- First confirm each finding against the current code. If upstream already fixed one, do not change it: report
  "already fixed" with the file and line that show it. That is a valid result.
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`.
- Do not change released Team API wire contracts, IPC contract names, or migrations.
- Stay inside this worktree. Do not use GUI apps or Computer Use. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: the typecheck project for the code you change and the E2E scripts you add or change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
