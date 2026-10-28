# M10 #66: E2E for signing in to a server with openbot login (tests 11)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Test-coverage finding 11 below. First check existing scripts (grep scripts/ for server-mode, openbot login, central-auth) and report what they cover. Add one new E2E script for the uncovered path against a local fake server started inside the script (no internet). No product code change unless a test exposes a real bug; then report it. Temporary files only under $TMPDIR or .openbot-build/.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 11. The server-mode control socket has no test

**Evidence**

- `docs/self-hosted-server.md:68` lists the commands. `sudo openbot login` and `openbot name` work
  only through the socket.
- `src/main/server-mode.ts:202` is the route switch. It holds `/v1/login/start`, `/v1/login/verify`,
  `/v1/login/claim`, `/v1/name`, `/v1/mobile-connect`, and `/v1/logout`.
  `src/main/server-mode.ts:191` applies the 4 KiB body limit.
- `scripts/hosting/openbot:4` names the file in a comment only.

**Why it matters**

On a headless server this socket is the only way to sign in. A defect leaves a server that nobody can
sign in to, and the only recovery is a new account.

**Fix**

Add `scripts/server-mode-cli-e2e.ts`. It runs the real socket over a Unix socket in a temporary
directory with a fake account service. It checks each route, the body limit, the answer format, and
that the answer never carries the session token, the sign-in code, or the claim.

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
