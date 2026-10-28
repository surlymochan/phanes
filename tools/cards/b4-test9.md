# M10 #66: E2E for downloading a provider runtime (tests 9)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Test-coverage finding 9 below. First check existing scripts (grep scripts/ for provider-runtime-manager and runtime download) and report what they cover. Add one new E2E script for the uncovered path using a local HTTP fixture server inside the script (no internet). No product code change unless a test exposes a real bug; then report it. Temporary files only under $TMPDIR or .openbot-build/ in the worktree; never touch /tmp.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 9. The provider runtime download has no test

**Evidence**

- `README.md:117` says that OpenBot can download a supported provider runtime when the user selects
  `Download` in onboarding, Settings, agent setup, or the model picker.
- `src/main/provider-runtime-manager.ts:189` is `ProviderRuntimeManager`. It reads the hashes from
  `native-runtime.lock.json`.
- The only scripts that name the manager are `scripts/seed-dev-state.ts:48` and
  `scripts/pin-remote-desktop-runtime.ts:64`. Neither drives a download.

**Why it matters**

A new user has no provider CLI. The download reads a hash for every file and unpacks an archive. A
defect in the hash check or in the unpack leaves the user with no provider and no message.

**Fix**

Add `scripts/provider-runtime-install-e2e.ts`. It runs the real `ProviderRuntimeManager` with a local
folder as the source, so the script needs no network. It checks the refusal of a wrong hash, the
install of a good archive, the activation of the new runtime, the case where the user has its own CLI,
and the update path that `scripts/provider-update-busy-e2e.ts` covers only during a turn.

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
