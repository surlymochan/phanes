# Fix the type errors in the two accessibility E2E scripts

Repository: OpenBot desktop. Read AGENTS.md. Base: your commit 25f0842df4 (the end of your accessibility work, findings 1-15).

## Problem

The pre-commit hook runs `bun run typecheck:node`, which covers `scripts/`. It fails on your two scripts. You only ran
`typecheck:renderer`, which does not cover `scripts/`:

- `scripts/m10-a11y-selection-tab-roles-e2e.ts(8,23)` and `scripts/m10-a11y-toolbar-arrows-e2e.ts(8,23)`: TS7016, no declaration
  file for module `jsdom`.
- `scripts/m10-a11y-toolbar-arrows-e2e.ts(46,3)`: TS5097, an import path ends with `.ts` but `allowImportingTsExtensions` is off.

## Task

1. Run `bun run typecheck:node` and fix every error in your scripts. No `@ts-ignore`, no `@ts-expect-error`, no `any`/`unknown`
   widening, no type assertion that bypasses the checker (AGENTS.md: these block auto-approval). Look at how other scripts under
   `scripts/` that use a DOM or jsdom handle it (grep `jsdom` in scripts/ and in `*.dom.test.ts`); follow that. If the repo has no
   typed way to use jsdom from a script, do not add a dependency: rewrite the check to use what the repo already provides, or move it
   to a protected vitest file only if AGENTS.md allows it for this area (it does not for markup), otherwise delete the script and say
   which behavior is no longer covered.
2. AGENTS.md says E2E tests assert behavior and data, not markup or classes. If a script only asserts attribute names, say so in your
   report and keep it only where it checks keyboard behavior (focus moves, arrow keys, Escape).
3. Run each remaining script (`bun scripts/<name>.ts`), `bun run typecheck:node`, `bun run typecheck:renderer`, `bun run check:ui`
   and `bunx biome check <changed files>`. All must pass.

Temporary files only under `$TMPDIR` or `.openbot-build/` in the worktree. Do not commit; do not push. Report each check result.
