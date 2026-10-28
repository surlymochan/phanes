# M10 #65: mobile paused-agent marker with Continue, and readable turn process notes (mobile 2-3)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Mobile parity findings 2 and 3 below, in apps/mobile (Expo / React Native; read apps/mobile/AGENTS.md). Reuse the existing Team API calls and contracts that the desktop uses for pause/continue and for turn process notes; do not add or change a wire protocol. Mobile shares brand tokens and contracts, not SolidJS components. User text from @openbot/i18n keys. Extend the existing mobile E2E or regression script that covers chat (look under scripts/ for mobile-*-e2e.ts) with a case for each finding.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 2. A paused agent has no marker and no Continue action

- Desktop: `src/renderer/src/app-message-projection.ts:301` makes the `agent-pause` marker, and
  `packages/ui/src/features/conversation/ChatActionMarker.tsx:491` draws the Continue button.
- Mobile: `apps/mobile/src/features/chat/model/chat-messages.ts:362` drops every message of the
  `system` author. The host writes the pause as a system message with readable text
  (`src/backend/agent/agent-pauses.ts:174`). The phone shows nothing.
- The host has no route for the action. `packages/contracts/src/ipc-endpoints.ts:1146` reads: "Local
  only: a joined server's Team API has no route to continue a paused agent."
- Why it matters: the agent stops and waits for the user. On the phone the chat looks idle and the
  agent stays silent. The user cannot continue it from the phone.
- Fix: add a `pause` kind to `ChatMessage` and draw the marker with the readable text of the host
  message. Add a Team API route for the continue action, next to the other agent routes in
  `src/main/team-api/`, and a helper in `packages/team-client/src/team-api-requests.ts`. The route
  keeps the check of the local handler in `src/main/ipc/agent-handlers.ts:641`. The desktop answers
  a joined server with `queueControlRemoteUnsupported` (`:643`), so the phone must hide the action
  for a server without the new route.
- Size: **M**.

### 3. The process notes of a turn cannot be read

- Desktop: `packages/ui/src/features/conversation/ChatMessageRow.tsx:97` draws the
  `ResponseProcess` panel of `packages/ui/src/features/conversation/ResponseProcess.tsx:7`. The panel
  lists the notes of the turn and stays in the chat after the turn ends.
- Mobile: `apps/mobile/src/features/chat/components/chat-message-list.tsx:101` removes the
  `thinking` kind from the rows, and `apps/mobile/src/features/chat/components/chat-message-list.tsx:922`
  filters those messages out. Only the last note shows in the activity row while the agent works
  (`apps/mobile/src/features/chat/components/chat-message-list.tsx:1100`).
- Why it matters: the user reads how the agent worked on the desktop. On the phone the notes are
  gone as soon as the reply arrives.
- Fix: keep the thinking rows in the list. Draw them as a closed panel that opens on a tap, with the
  notes of the turn. The mobile already has the data: `projectChatMessages` collects the steps in
  `apps/mobile/src/features/chat/model/chat-messages.ts:373`.
- Size: **M**.


## Rules

- First confirm each finding against the current code. If upstream already fixed or covered it, do not change it:
  report "already fixed" with the file and line that show it. That is a valid result.
- Do every finding in scope before you finish; report each one as fixed / already fixed / not fixed (why).
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`. Running this repository's own
  E2E scripts, Electron ones included, is allowed.
- Do not change released Team API wire meaning, IPC contract names, or migrations. No database migration.
- Never read or print `.env*`, `.env.keys`, `.dev.vars`, auth or credential files. Read one file per command.
- Stay inside this worktree. Do not use Computer Use or drive other desktop apps. Do not commit (the runner
  commits) and do not push.
- Run only narrow checks, one at a time: `bun run --cwd apps/mobile typecheck` and the mobile E2E script you change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
