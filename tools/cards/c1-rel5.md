# M10 #64: verify finding 8, then fix reliability findings 9-11

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

1. Finding 8 (script origins bounded in last-seen order) is already changed in your base 460935ffb4 but has no
   test. Add an E2E case for browser-origin eviction and run it. Running this repository's own Electron E2E scripts
   (for example the existing browser E2E fixtures under scripts/) IS allowed; only Computer Use and driving other
   desktop apps are forbidden.
2. Then fix backend reliability findings 9, 10 and 11 below, each with an E2E case.
Do not stop after one item; report each as fixed / already fixed / not fixed with the reason.

## Findings 9-11 (from an audit written before the upstream sync; references may have moved)

### 9. The removal of a provider temp folder belongs to no scope

Size: S.

Evidence:

- `src/backend/provider-temp.ts:51` — `const dispose = () => void Effect.runFork(removeDirectory(directory));`
- `src/backend/provider-temp.ts:60` — `child.once("close", dispose);`

Why it matters: the removal runs on the default runtime and is never awaited. No shutdown path
waits for it. The removal can then be lost when the app quits, and the folder of a 310 MB unpack
stays on the disk until the next launch sweeps it. The rule of the repository asks that an owned
resource is tied to a finalizer.

Fix: give `spawnWithProviderTemp` a scope, or return the removal Effect to the caller. The caller
then awaits the removal in its own finalizer. The sweep at the start stays as the safety net.

### 10. The local MCP bridge closes without ending the tool calls that run in it

Size: S.

Evidence:

- `src/backend/local-mcp-bridge.ts:88` — `readonly #routes = new Map<string, BridgeRoute>();`
- `src/backend/local-mcp-bridge.ts:130` — `this.#routes.clear();`
- `src/backend/local-mcp-bridge.ts:78` — `running: Map<RequestId, AbortController>;`

Why it matters: `close` drops the routes but does not abort the `AbortController` of a call that
still runs. The three clients that use the bridge call `rejectAll` before they close it, so the
call ends today. The bridge itself does not hold that rule. A call that arrives between those two
steps waits for the user for as long as the provider lives, and the turn that holds it never ends.

Fix: keep one list of every `AbortController` that the bridge handed out. Abort them all in
`close`, before the routes are dropped.

### 11. The interrupted turns of Codex are kept without a limit

Size: S.

Evidence:

- `src/backend/app-server-client.ts:61` — `readonly #interruptedTurns = new Set<string>();`
- `src/backend/app-server-client.ts:266` — `this.#interruptedTurns.add(JSON.stringify([params.threadId, params.turnId]));`
- `src/backend/app-server-client.ts:341` — the delete, only in the handler of `turn/completed`
- `src/backend/app-server-client.ts:364` — `if (threadId === undefined) this.#interruptedTurns.clear();`

Why it matters: a turn that is interrupted and never reports completion keeps its key for the life
of the client. Each key holds a JSON string. The set also grows when the Codex app server drops a
`turn/completed`.

Fix: delete the key when the turn ends by any path. Use a bounded map, and drop the oldest entry
when the map is over its limit.


## Rules

- First confirm each finding against the current code. If upstream already fixed one, do not change it: report
  "already fixed" with the file and line that show it. That is a valid result.
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`.
- Do not change released Team API wire contracts, IPC contract names, or migrations.
- Never read or print `.env*`, `.env.keys`, `.dev.vars`, auth or credential files; the risk guard stops the run. Read one file per command.
- Stay inside this worktree. Do not use Computer Use or drive other desktop apps; running this repository's Electron E2E scripts is allowed. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: the typecheck project for the code you change and the E2E scripts you add or change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
