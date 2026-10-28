# Make the renderer performance changes pass `bun run typecheck:node`

Repository: OpenBot desktop. Read AGENTS.md. Base: `9fe21e7cbd` (a temporary WIP commit on top of main `fd5719fc7f` that holds eight executor
commits of renderer performance work; the formal commit failed the pre-commit type check).

## Errors from `bun run typecheck:node` (this project covers `scripts/`, `src/main`, `src/backend`, `src/preload`)

- `TS6142 ... was resolved to '.../packages/ui/src/text.tsx', but '--jsx' is not set` in `packages/ui/src/features/conversation/chat-day-markers.ts`,
  `packages/ui/src/features/conversation/chat-timestamp.ts` and `src/renderer/src/app-message-projection.ts` (it imports `@openbot/ui/text`
  and `@openbot/ui/features/conversation/TaskList`, both `.tsx`). The node project pulls these files in because a script imports renderer
  code (`scripts/renderer-conversation-perf-e2e.ts`). A `.ts` module that a script imports must not import a `.tsx` module.
- `scripts/renderer-conversation-perf-e2e.ts(245,56)` and `(372,54)`: TS2345, the attachment fixtures miss `kind`, `previewKind`, `previewUrl` of `AttachmentSummary`.
- `scripts/sidebar-virtual-perf-e2e.ts(132,3)`: TS2322, `Record<string, Spread | null>` is not `Record<string, Spread>`.

## Task

1. Make `bun run typecheck:node`, `bun run typecheck:renderer`, `bun run typecheck:storybook` and `bun run check:ui` pass. Use real fixes: move pure logic
   (no JSX) into a `.ts` module that imports no `.tsx` (for example the part of the projection or the text helpers that the script needs), keep the `.tsx` files
   importing from it, and type the fixtures correctly. Do NOT add `@ts-ignore`, `@ts-expect-error`, `any`/`unknown` widening, a bypassing type assertion, or a
   `tsconfig` change that adds `jsx` to the node project. Do not change what the renderer shows.
2. Run every performance script you touch (`bun scripts/renderer-conversation-perf-e2e.ts`, `bun scripts/sidebar-virtual-perf-e2e.ts`, and any other new
   `*perf*e2e.ts` in `scripts/`) and keep their JSON reports under `.openbot-build/`. Report the median numbers.
3. `bunx biome check <changed files>`.

Do not use Computer Use. Temporary files only under `$TMPDIR` or `.openbot-build/` in the worktree. Do not commit; do not push. Report each check result.
