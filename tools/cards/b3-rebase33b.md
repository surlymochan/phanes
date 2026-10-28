# Re-apply PR #33 (iOS self-build guide) and PR #36 (PRIVACY note on coding dispatch) on the new main

Repository: OpenBot desktop (Electron, Bun, TypeScript). Read AGENTS.md and the AGENTS.md of every directory you change.
Base: `fd5719fc7f`, the new main. It contains the merge of 429 upstream commits, so the code, docs and scripts
around these files changed a lot since these PRs were written.

## Task

Re-apply each PR below onto this base so that it is correct for the new code. For every PR:

1. Read its diff (commands below). Understand its intent.
2. Apply it with `git apply --3way`, or by hand where it does not apply.
3. Re-verify against the CURRENT code every factual claim and every command it adds or changes. Fix what is no
   longer true, drop what upstream already did, and say so. Do not copy a stale claim.
4. Keep the PR's scope. Do not add unrelated changes.

### PR nibiru-labs/openbot#33  (branch `docs/ios-self-build`)

Files this PR changes: docs/ios-self-build.md

Get its change: `git fetch https://github.com/nibiru-labs/openbot.git docs/ios-self-build` then
`git diff 44c1e550da FETCH_HEAD -- docs/ios-self-build.md` (44c1e550da is the old main it was written against;
ignore any other commit on that branch, those are already merged or belong to other PRs).
### PR nibiru-labs/openbot#36  (branch `docs/privacy-dispatch`)

Files this PR changes: PRIVACY.md, changelog.d/docs-privacy-dispatch.md

Get its change: `git fetch https://github.com/nibiru-labs/openbot.git docs/privacy-dispatch` then
`git diff 44c1e550da FETCH_HEAD -- PRIVACY.md changelog.d/docs-privacy-dispatch.md` (44c1e550da is the old main it was written against;
ignore any other commit on that branch, those are already merged or belong to other PRs).

## Rules

- Smallest change. No new dependency. Interface text comes from `@openbot/i18n` keys. No migrations, no released
  wire or IPC contract change.
- Tests are E2E scripts under `scripts/`; do not add unit tests. Wait on state, not time.
- To clean up a temporary directory, make it inside the worktree under `.openbot-build/` and remove it with a literal relative path, for example `rm -rf .openbot-build/probe`. Never run `rm -rf "$VAR"` or `cd / && rm ...`: the guard cannot see what the variable points to and stops the run.
- Never read or print `.env*`, auth or credential files. Temporary files only under `$TMPDIR` or `.openbot-build/`
  in the worktree; never write or remove anything in `/tmp` or outside the worktree. Do not use Computer Use.
- Do not commit (the runner commits) and do not push. Add a release note `changelog.d/<short-name>.md` only if the PR
  did not carry the `no-changelog` label (PR #36 carries a note; keep it).

## Checks

`git diff --check`; every command the guide names must exist (apps/mobile/package.json scripts), and `bunx biome check <changed files>` (docs: also `git diff --check`).

## Report

For each PR: what you changed, which of its claims you found stale and how you fixed them, and each check result.
