# M10 #63: Team API routes must not forward raw internal messages (errors 3)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Error-handling finding 3 below. Your base already contains your fix for findings 1-2 (typed error tag survives runCauseEffect). Released Team API v1-v3 wire meaning must not change: keep status codes and response shapes; replace raw internal text with catalog messages and codes only where the client already reads a message string. Add or extend a Team API E2E case (scripts/team-api-routes-e2e.ts) that shows no raw internal message reaches the client.

## Findings (from an audit written before the upstream sync; references may have moved)

### 3. Team API routes forward raw internal messages to released clients

**Size: M per route, L for the whole set**

Evidence (the catch-all sites):

- `src/main/team-api/route-agent-install.ts:61` — `if (error instanceof Error) throw new HttpError(409, error.message);`
- `src/main/team-api/route-host-admin.ts:33`
- `src/main/team-api/route-shared-tables.ts:33`
- `src/main/team-api/route-skills-admin.ts:44`
- `src/main/team-api/route-hosted-sites.ts:35`
- `src/main/team-api/route-providers.ts:199` — the same, with `redactText`

About 30 sites in `src/main/team-api/` use `error.message` for an `HttpError` body.

The dispatcher does not sanitize these. `src/main/team-api-server.ts:864-867` keeps the message
whenever the error is `expected`, and an `HttpError` always is:

```ts
const message =
  expected || (error instanceof Error && isFixedSourceText(error.message))
    ? error.message
    : sourceText("error.team.requestFailed");
```

Why it matters: the Team API sends `{ error: message }` to clients of every released version. A
message that is not a catalog template stays English for every reader, and a raw cause can carry a
local path or an internal detail. The `AgentMarketplaceFailure` in
`src/main/agent-marketplace-service.ts:414-416` has only a `cause`, so an install that fails
because the network is down sends `fetch failed` to the client.

Fix: map each failure to a `sourceText("error.team.*")` key at the route, and keep the raw text in
the log. Change one route at a time, with the message of the catalog key equal to the current
English, so an older client sees the same text.

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
