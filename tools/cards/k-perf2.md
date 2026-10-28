# M10 #61: make the streaming parse change pay off, or revert it (perf finding 2)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and packages/ui/AGENTS.md first.
Base: your commit 0592c3a977 (perf findings 1 and 2 on top of PR nibiru-labs/openbot#42).

## Problem

Your own benchmark (`scripts/renderer-conversation-perf-e2e.ts`) reports the streaming block parse speedup as a
median of 0.56x (slower) on one run and 1.17x on another. A performance change that is slower or unstable is not
acceptable. The change also moved about 290 lines out of `packages/ui/src/features/conversation/DataTable.tsx`.

## Do one of these

A. Make finding 2 a clear win: on a realistic long streaming reply (at least 20,000 words with headings, lists,
   code fences and tables, revealed in word steps), the incremental parse must be at least 2x faster than a full
   re-parse at the median over at least 5 runs, and its output must equal the full parse character for character
   at every step. Keep the DataTable move only if it is needed for this; otherwise restore DataTable.tsx.
B. If A is not reachable, revert every finding-2 change (message-content-blocks.ts, MessageRendering.tsx,
   DataTable.tsx) to the base of PR #42 (`git show b50d5ce925:<path>`), keep finding 1, and update the script and
   the release note so they only claim finding 1.

## Checks

`bun run typecheck:renderer`, `bun scripts/renderer-conversation-perf-e2e.ts` (report the medians per run), and
`bunx biome check <changed files>`. Stay inside this worktree. Do not use GUI apps. Do not commit; do not push.
Report which option you took and the numbers.
