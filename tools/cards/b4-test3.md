# M10 #66: E2E for installing a plugin and opening openbot://plugins/<slug> (tests 12)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Test-coverage finding 12 below. First check existing scripts under scripts/ that touch deep links or plugin install and report what they cover. Add one new E2E script for the uncovered path. No product code change unless a test exposes a real bug; then report it instead of fixing it.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 12. The plugin install, the uninstall, and the plugin link have no test

**Evidence**

- `docs/plugin-distribution.md:225` describes the install, `docs/plugin-distribution.md:262` the
  uninstall, and `docs/plugin-distribution.md:326` the link `openbot://plugins/<slug>`.
- `docs/plugin-distribution.md:428` says that the check of the link forms, the page, the install, and
  the uninstall is a manual step with `bun run dev:automation`.
- `src/main/deep-link-router.ts:48` is `parseDeepLink`.
- `scripts/marketplace-page-e2e.ts:1` says that it installs nothing. No script imports
  `deep-link-router`.

**Why it matters**

The plugin link is a public share form. A defect can open the wrong page, or install a plugin in the
wrong agent, or leave a plugin that no longer un-installs.

**Fix**

Add `scripts/plugin-install-e2e.ts`. It runs the real deep link parser and the real plugin install
service with a local catalog file. It checks the link forms, including `openbot://plugins/../join`,
the install of the pinned skills into the chosen agent, the save of the app as a host-global MCP
server, the uninstall, and the refusal of an unknown slug.

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
