# Fix the ratcheted TypeScript errors in three new E2E scripts

Repository: OpenBot desktop. Read AGENTS.md. Base: branch `pr51` (head `b8176f1c2b`), the E2E coverage pull request on top of main `fd5719fc7f`.

## Problem

CI step `bun run types:ratchet` (script `scripts/type-ratchet.ts`) fails on this branch:

- `exactOptionalPropertyTypes`: `scripts/agent-profile-tools-e2e.ts` has 2 errors, `scripts/telegram-messaging-e2e.ts` has 4, `scripts/update-service-e2e.ts` has 1. The baseline allows 0.

`exactOptionalPropertyTypes` means an optional property (`x?: T`) cannot be set to `undefined` explicitly. Typical fixes: build the object without the key when the value is
undefined (conditional spread `...(value === undefined ? {} : { key: value })`), or only set the key when defined.

## Task

1. Run `bun scripts/type-ratchet.ts`. It prints the failing options. Run `bun scripts/type-checker.ts --noEmit -p tsconfig.node.json --exactOptionalPropertyTypes` to see each error with its line.
2. Fix every error in the three scripts with real code changes. No `@ts-ignore`, no `@ts-expect-error`, no `any`/`unknown` widening, no type assertion that bypasses the checker, and no change to the baseline file.
3. Run `bun scripts/type-ratchet.ts` (must pass), `bun run typecheck:node`, each of the three scripts (`bun scripts/<name>.ts`, all must still pass) and `bunx biome check <changed files>`.

Never write or remove anything in `/tmp` or outside the worktree. Do not use Computer Use. Do not commit; do not push. Report each check result.
