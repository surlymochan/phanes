# M10 #66: E2E for publishing this computer to the account service (tests 14)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Test-coverage finding 14 below. First check existing scripts (grep scripts/ for host-service and publish) and report what they cover. Add one new E2E script for the uncovered path against a local fake account service started inside the script (no internet, no real Cloudflare). No product code change unless a test exposes a real bug; then report it. Temporary files only under $TMPDIR or .openbot-build/.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 14. Publishing this computer to the account service has no test

**Evidence**

- `src/main/host-service.ts:600` is `#publishActiveHost`, and `src/main/host-service.ts:888` explains
  the reconcile of the members.
- `src/backend/agent/hosted-site-coordinator.ts:99` is `HostedSiteCoordinator`, and
  `src/backend/agent/hosted-site-coordinator.ts:268` is the tool `publish_site`.
- `scripts/mobile-connected-devices-e2e.ts:58` adds a host to the account directory with direct
  database rows, instead of the publish call. It is a stand-in, not a test of the publish.

**Why it matters**

Publishing is what makes a computer a server that other members can join. A defect leaves the
members unable to reach the host, or leaves a stale membership after an account change.

**Fix**

Add `scripts/host-publish-e2e.ts`. It runs the real `HostService` with a fake account service. It
publishes and checks the membership and the directory row. It publishes again after an account
change and checks that the old membership is gone. It checks that a second account cannot publish the
same host.

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
