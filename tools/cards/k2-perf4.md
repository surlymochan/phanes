# M10 #61: renderer performance findings 4 and 12

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Renderer performance findings 4 and 12 below (every message body owns a ResizeObserver that forces a layout read; every markdown text block runs five regex scans and builds two maps). Your base a897d697d7 has your fixes for findings 1, 3, 6 and 7. Keep output identical. Extend scripts/renderer-conversation-perf-e2e.ts with a measurement for each fix and report the median before/after over at least 5 runs; revert a change that is not clearly faster.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 4. Every message body owns a `ResizeObserver` that forces a layout read

**Evidence**

- `packages/ui/src/features/conversation/MessageRendering.tsx:345-349` — every
  `MessageBody` calls `createSmoothHeightResize`, including settled messages and the
  reader's own messages.
- `packages/ui/src/features/conversation/createSmoothHeightResize.ts:26-45` — the observer
  callback calls `content.getBoundingClientRect()` *before* it checks
  `options.enabled?.() === false`, so the forced layout happens even when the animation is
  off.
- `packages/ui/src/features/conversation/CodePreview.tsx:60` — a code preview adds a second
  observer, with no `enabled` guard at all.

**Why it matters**

A chat with 30 visible agent messages keeps 30 live `ResizeObserver`s. Any layout change in
the transcript wakes them, and each one reads a bounding box, which forces a synchronous
layout of the whole document. Scrolling a chat with code blocks is the worst case.

**Fix**

Check `enabled()` before reading the box, and attach the observer only while the height
animation is on. `CodePreview` should pass an `enabled` guard too.

**Size** M

### 12. Every markdown text block runs five regex scans and builds two maps

**Evidence**

- `packages/ui/src/features/conversation/RichMessageText.tsx:58-66` — `citationsByNumber`
  and `attachmentsById` are built per component instance, and `parts` runs
  `richMessageParts`.
- `packages/ui/src/features/conversation/RichMessageText.tsx:366-404` — `richMessageParts`
  runs `semanticTagParts`, `referencedMessageParts`, `linkedMessageParts`,
  `taggedMessageParts` and `citedMessageParts` over the body.
- `packages/ui/src/features/conversation/MarkdownMessageText.tsx:780-793` and `:442-460` —
  `RichText` mounts one `RichMessageText` per text token, so one per paragraph.

**Why it matters**

A 40-paragraph reply mounts 40 `RichMessageText` instances, each with its own two maps and
its own five regex scans over its own text. The maps do not depend on the paragraph, so
they are rebuilt for every paragraph of the same message.

**Fix**

Build the citation map and the attachment map once per message and pass them down, the way
`MarkdownContentProps` already passes the rest of the content. Keep the per-token work to
the text itself.

**Size** M


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
- Run only narrow checks, one at a time: `bun run typecheck:renderer` and `bun scripts/renderer-conversation-perf-e2e.ts`, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
