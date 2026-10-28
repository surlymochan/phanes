# M10 #66: E2E for creating an agent and editing its profile (tests 4)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Base commit: b50d5ce925 (head of PR nibiru-labs/openbot#42,
the upstream sync). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Test-coverage finding 4 below. Check the existing scripts first (scripts/agent-profile-accounts-e2e.ts, scripts/agent-update-agents-e2e.ts, scripts/team-api-routes-e2e.ts). Add one new E2E script for the uncovered path: create an agent and edit its profile through the real backend, then read it back. No product code change unless a test exposes a real bug; then report it instead of fixing it.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 4. Creating an agent and editing its profile have no test

**Evidence**

- `README.md:21` is the first item of "What works": prompt-driven agent creation and editing, with
  editable instructions, avatar, and section review before saving.
- `src/backend/agent/profile-tools.ts:118` holds the tool names `create_agent` and `update_profile`.
- `src/backend/agent/profile-generation.ts` builds the profile from the request, and
  `src/backend/agent/profile-save.ts` saves it.
- No script calls `create_agent` or `update_profile`.

**Why it matters**

This is the first flow a new user meets. The agent creates the agent and gives it the first task. A
defect leaves a half-made agent, a lost instruction, or an agent that a restart forgets.

**Fix**

Add `scripts/agent-profile-tools-e2e.ts`. It runs the real tool router on a temporary profile with a
fake provider. It creates an agent, reads it back, updates the profile, restarts the agent store, and
checks that the change is still there. It checks that an invalid profile is refused with a message
from `sourceText`. It writes `.openbot-build/agent-profile-tools-e2e/report.json`.

**Size: M**

---

## Rules

- First confirm each finding against the current code. If upstream already fixed one, do not change it: report
  "already fixed" with the file and line that show it. That is a valid result.
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`.
- Do not change released Team API wire contracts, IPC contract names, or migrations.
- Stay inside this worktree. Do not use GUI apps or Computer Use. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: `bun run typecheck:node` and `bun scripts/<your new script>.ts`, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
