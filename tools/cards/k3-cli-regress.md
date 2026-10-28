# Fix two failing checks of the Local scripts CLI E2E on the errors branch

Repository: OpenBot desktop. Read AGENTS.md and src/main/AGENTS.md. Base: branch `pr47` (head `07f48b11cf`), the errors PR on top of main `fd5719fc7f`.
It contains your earlier work (findings 1-3 and 12-15) and other executors' work on error messages.

## Problem

CI step `bun run test:automation-cli` (`scripts/automation-cli-e2e.ts`) fails two checks on this branch and passes on `fd5719fc7f`:

- `a refused update exits 1 with its reason` (`agent set-account Ada default`) expects stderr to include `Wait until the agent is idle.`
- `new-chat while the agent works exits 1 with its reason` (`agent new-chat Ada`), same kind of reason.

These are refusals from a domain operation that the user must read (an agent that is working cannot change account or start a new chat). The
errors work made a route or the dispatcher forward only text that matches a catalog key and answer anything else without the message.
`Wait until the agent is idle.` is either not recognized as catalog text, or the Local scripts automation path now goes through that filter.

## Task

1. Run `bun run test:automation-cli` on the base `fd5719fc7f` (git worktree or checkout; expect pass) and on `pr47` (expect the two failures). Find which commit of
   the branch causes it (`git log fd5719fc7f..pr47`, it is one commit, so bisect by file: look at the Team API route / dispatcher / error mapping files it changed).
2. Fix it so that: a refusal whose message is catalog text (`sourceText(key)`) is forwarded to the caller with its reason, a failure that is NOT catalog text
   still answers without the raw internal message (the intent of finding 3, and `an unexpected update failure exits 1 without its detail` must keep passing).
   If `Wait until the agent is idle.` is not yet a catalog text, make it one (key in `@openbot/i18n` for every locale) and throw it as `sourceText(key)`.
   Do not change released Team API wire meaning (status codes and response shapes stay).
3. Run and pass: `bun run test:automation-cli`, `bun run test:team-api-routes`, `bun run test:team-legacy-clients`, `bun run typecheck:node`, `bun run i18n:check`,
   `bunx biome check <changed files>`.

Never write or remove anything in `/tmp` or outside the worktree. Do not use Computer Use. Do not commit; do not push. Report the cause (file and line) and each check result.
