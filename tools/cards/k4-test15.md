# M10 #66: E2E for signing in and sending a message in the browser client (tests 15)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Test-coverage finding 15 below. First check existing scripts (scripts/web-channel-chat-e2e.ts, scripts/web-client-threads-e2e.ts, scripts/web-side-panel-e2e.ts) and report what they cover. Add one new E2E script only for the uncovered sign-in + send path, reusing their harness. No product code change unless a test exposes a real bug; then report it.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 15. The browser client has no flow test

**Evidence**

- `docs/web-client.md:227` is the release gate. It is a list of manual checks.
- `docs/web-client.md:255` records the local web verification of 2026-09-21.
- `scripts/web-client-perf-e2e.ts` measures the size of the bundles.
  `scripts/web-client-conversation-trim-e2e.ts` covers one memory case.
- No script signs in to `/app` and sends a message.

**Why it matters**

The browser client is the second surface of the product. Its sign-in, send, stop, and sign-out path
has no automated check, so each release needs a manual pass.

**Fix**

Add `scripts/web-client-flow-e2e.ts`. It starts the local account Worker with
`AUTH_EXPOSE_DEVELOPMENT_CODE=true` and a real host over loopback. It drives the real web workspace
through a headless browser. It signs in, sends a message, reads the reply, stops a run, and signs
out. The script must never write a code or a cookie to the log.

**Size: L**

---

## Secondary findings

### B1. Most E2E scripts never run

96 files in `scripts/` end in `-e2e.ts` or `-smoke.ts`. 56 of them have no `test:` entry in
`package.json` and no step in `.github/workflows/ci.yml` or `.github/workflows/release.yml`. The
list includes
`scripts/routine-backoff-e2e.ts`, `scripts/stuck-running-deliveries-e2e.ts`,
`scripts/mailbox-reconcile-e2e.ts`, `scripts/provider-loss-scope-e2e.ts`,
`scripts/team-session-end-e2e.ts`, `scripts/usage-limit-account-e2e.ts`, and
`scripts/response-process-e2e.ts`.

Many of these need a running dev app, so they cannot all run in CI. Still, a test that never runs
gives no protection. Give each one a `test:` entry, and mark the ones that need a dev app as manual
in the README, so that the state of the suite is clear.

**Size: M**

### B2. The check design notes are behind the workflow

`docs/development-checks.md:103` lists eight scripts for the "Team API and reverse actions E2E" job.
`.github/workflows/ci.yml:302` runs eleven. The notes do not name `test:zero-filled-settings`,
`test:steer-interrupt-pause`, or `test:token-budget`. Update the table when the workflow changes.

**Size: S**

## What I did not confirm

- I did not run any test, build, lint, or type check of the product code. The task is read-only, and
  `AGENTS.md` says to leave broad checks to CI. Every statement in this document comes from reading
  the source. I ran only `bun scripts/check-doc-links.ts`, which passed, because this document is a
  Markdown file.
- I did not read the whole of `apps/mobile`. The mobile app uses native controls, so the scripts in
  `scripts/` cannot drive them. `scripts/mobile-share-e2e.ts` and
  `scripts/mobile-connected-devices-e2e.ts` cover the parts that a script can reach.
- I did not confirm that every flow in `docs/` has a manual check somewhere. I only confirmed that
  no script exists for the 15 flows above.
- The account service email code sign-in has an indirect check in
  `scripts/mobile-connected-devices-e2e.ts`, which uses the real `AuthService`. I did not find a
  script that checks the cookie session, the logout, and the revoked cookie on their own. I did not
  list this as a finding because the sign-in path itself is covered.
- I did not check the plugins in `apps/slack-app/` and `apps/discord-app/`. The Slack install needs a
  public tunnel, so it cannot run in CI.

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
