# Combine two Team API capabilities and their phone UI on the new main

Repository: OpenBot. Read AGENTS.md, packages/contracts/AGENTS.md, src/main/AGENTS.md, src/preload/AGENTS.md and apps/mobile/AGENTS.md.
Base: `fd5719fc7f` (new main). A released wire contract is permanent: both capabilities are additive and optional, and nothing released may change meaning.

Two chains of commits were written independently on the same old base. All commits are in this repository.

1. Pause and continue (`agent-pause-continue-v1`, `POST /v1/agents/:id/pause/continue`, a paused-agent marker with a Continue action and readable turn process notes on the phone):
   `6769e66993` then `98f259bdad` then `364e2b6844`. The last two are fixes from a protocol review (chat authorization gate, frozen optional codec and side-route registration, strict client decoding, HTTP and signed WebRTC E2E).
2. Context usage (`context-usage-v1`, a context usage meter and composer skill picker work on the phone): `cb6db55189` then `c0eeecfab0`. The second is the review fix (frozen wire categories, fail-closed client decoder, explicit send projection, Team API E2E).

They conflict (for example `src/main/team-api/dependencies.ts`, the protocol lists in `packages/contracts/src/team-protocol`, `packages/contracts/package.json` exports, `packages/team-client`, and the phone chat files and `@openbot/i18n` locale files).

## Task

1. Apply them in the order above with `git cherry-pick -n <sha>`, one after the other. Resolve each conflict so that BOTH capabilities and BOTH phone features work: keep both entries in
   every registry, export map and capability list; take the UNION of i18n keys in every locale (no key lost, none duplicated); keep both mobile release note fragments.
2. Keep both reviews' fixes: pause/continue needs **chat** access; both codecs frozen and strict; context usage returns counts, category labels and compaction metadata only.
   Do not widen any authorization, do not change an existing route or codec, keep the preload `FromMain` decoders and the host `FromHost` decoders separate (src/main/AGENTS.md).
3. Run and pass: `bun run test:team-api-routes`, `bun run test:team-legacy-clients` (must pass unchanged), every script that the five commits added or changed under `scripts/`,
   `bun run typecheck:node`, `bun run --cwd apps/mobile typecheck`, `bun run i18n:check`, `bunx biome check <changed files>`.

Never write or remove anything in `/tmp` or outside the worktree. Do not use Computer Use. Do not commit; do not push. Report each conflict (file, how resolved) and each check result.
