# Combine four mobile commits on the new main and resolve their conflicts

Repository: OpenBot (mobile app in apps/mobile, Expo / React Native; read apps/mobile/AGENTS.md and AGENTS.md). Base: `fd5719fc7f` (new main).

Four commits were written independently on the same old base. They change the phone chat and its text, so they conflict. They are all in this
repository (names are branches):

1. `3cb28d9f69` (yours): day separators and an unread divider (mobile finding 15).
2. `04d119a5d3` (yours, on top of 1): dropped system markers projected, marketplace suggestion card (finding 7).
3. `mob-k3` = `714b06d175`: agent prompt blocks (confirm, quick replies, choice, form) answered through `respondToPrompt`, and in-chat search (findings 1, 14).
4. `mob-b3a` = `2b480013d5`: Stop and send, grouped messages, routine card in the chat.

## Task

1. On this base apply them in this order with `git cherry-pick -n <sha>`. Commits 1 and 2 are one chain, so apply 1 then 2. When a conflict appears, resolve it so that
   BOTH features keep working: both behaviors in `agent-chat-view.tsx` and `channel-chat-screen.tsx`, and the UNION of all i18n keys in every locale file
   (`packages/i18n/src/messages/*/mobile/chat.ts` and the others that conflict; no key may be lost and none may be duplicated). Keep every release note
   fragment under `apps/mobile/changelog.d/`.
2. HARD RULE: client only. The result must not change any file under `packages/contracts`, `packages/team-client`, `src/main` or `src/backend`. Check with
   `git diff --stat fd5719fc7f -- packages/contracts packages/team-client src/main src/backend` (must be empty).
3. Find the scripts the four commits added or changed (`git show --stat <sha> -- scripts` for each) and run every one with `bun scripts/<name>.ts`. All must pass.
4. Run `bun run --cwd apps/mobile typecheck`, `bun run typecheck:node`, `bun run i18n:check` and `bunx biome check <changed files>`. All must pass.

Never write or remove anything in `/tmp` or outside the worktree. Do not use Computer Use. Do not commit (the runner commits); do not push.
Report the conflicts you resolved (file and how) and each check result.
