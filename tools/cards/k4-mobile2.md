# M10 #65: mobile file preview for other files, unread divider and day separators (mobile 4, 15)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Mobile parity findings 4 and 15 below, in apps/mobile (read apps/mobile/AGENTS.md). Client-side only: do NOT add or change any Team API route, capability, contract or IPC endpoint; use what the desktop and the existing mobile client already call. If a finding cannot be done without a new route, report it as not fixed with the reason. Do not edit the composer (another run owns it). User text from @openbot/i18n keys. Add or extend a mobile script under scripts/ with a case for each finding.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 4. A file that is not an image or a video has no preview

- Desktop: `packages/ui/src/features/conversation/FilePreviewPanel.tsx:75` opens a panel for a file.
  It shows a Markdown file (`:340`), a PDF (`:374`), media (`:378`) and a spreadsheet (`:386`).
- Mobile: `apps/mobile/src/features/chat/components/chat-attachment.tsx:45` draws an image or a
  video. Every other file opens the share sheet. The mobile app never reads `previewKind` for a
  chat attachment.
- The host already knows the kind. `packages/contracts/src/ipc-attachments.ts:39` has
  `canPreviewAttachment`.
- Why it matters: an agent that answers with a report, a table or a PDF gives the user a file that
  the phone cannot read. The user must open the same file on the computer.
- Fix: add a preview screen to `apps/mobile/src/app/(app)/` that opens with the attachment id. Show
  a text file as text, a Markdown file as Markdown, and a PDF in a WebView. The app has no audio
  player yet, so an audio file needs one. Read the bytes with `downloadAttachment`
  (`apps/mobile/src/features/chat/components/attachment-preview.tsx:89`), the same read that the
  share sheet uses.
- Size: **M**.

### 15. The chat has no unread divider and no day separators

- Desktop: `src/renderer/src/features/conversation/ConversationTimeline.tsx:281` draws the banner
  with the number of unread messages, `:455` draws the divider in the transcript, and `:361` draws
  the separator of a new day.
- Mobile: the app marks the messages as read
  (`apps/mobile/src/features/chat/components/chat-view.tsx:498`), but it draws no divider and no
  banner. The mobile app has no day separator in the chat.
- Why it matters: the user cannot see where the unread messages start, and cannot see that a part of
  the chat is from another day.
- Fix: add a divider row to the list of the mobile app at the read boundary, and a separator row
  when the day of the message changes. The rule of the desktop is in
  `packages/ui/src/features/conversation/chat-day-markers.ts:46` and can be reused as it is a pure
  function of two timestamps.
- Size: **S**.


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
- Run only narrow checks, one at a time: `bun run --cwd apps/mobile typecheck` and the mobile script you add or change, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
