# M10 #66: E2E for creating, revising and installing a local skill (tests 6)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Base commit: b50d5ce925 (head of PR nibiru-labs/openbot#42,
the upstream sync). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Test-coverage finding 6 below. First list the existing scripts under scripts/ that already touch local skills (grep for local-skill-library and skill). Add one new E2E script for the uncovered path: create a local skill, revise it, install it for an agent, and read it back through the real code. No product code change unless a test exposes a real bug; then report it instead of fixing it.

## Findings (from an audit written before the upstream sync; references may have moved)

## 6. The local skill library has no test

**Evidence**

- `README.md:278` describes the flow: an agent prepares a folder with `SKILL.md` and calls
  `create_skill`, and later `list_local_skills`, `read_local_skill`, `revise_skill`, and
  `install_local_skill`.
- `src/main/local-skill-library.ts:13` is `LocalSkillLibrary`. `src/main/local-skill-tools.ts:32`
  reports a failed install after a saved revision.
- The only script that names the module is `scripts/transfer-upload-cleanup-e2e.ts:18`, and it imports
  only the `LocalSkillFailure` type. It does not drive the library.

**Why it matters**

A local skill revision is immutable, and an installed copy does not follow a new revision. A defect
can overwrite a revision, install a stale copy, or lose the file lock of a modified installed skill.

**Fix**

Add `scripts/local-skill-library-e2e.ts`. It runs the real `LocalSkillLibrary` on a temporary
directory. It saves a first revision, saves a second one, and checks that the first revision is still
readable. It checks that a stale expected revision is refused. It installs a revision for an agent and
checks the files in `.agents/skills/` and `.claude/skills/`. It changes an installed file and checks
that the next update refuses to overwrite it. It writes
`.openbot-build/local-skill-library-e2e/report.json`.

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
- Never read or print `.env*`, `.env.keys`, `.dev.vars`, auth or credential files; the risk guard stops the run. Read one file per command.
- Stay inside this worktree. Do not use GUI apps or Computer Use. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: `bun run typecheck:node` and `bun scripts/<your new script>.ts`, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
