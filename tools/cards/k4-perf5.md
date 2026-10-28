# M10 #61: @openbot/ui barrel keeps every primitive in the main chunk (perf 5), as a re-runnable codemod

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Renderer performance finding 5 below. Other branches are changing many renderer files right now, so do NOT hand-edit imports. Instead write scripts/ui-subpath-imports-codemod.ts: an idempotent codemod that rewrites barrel imports from @openbot/ui to the explicit subpaths that packages/ui already exports (check packages/ui/package.json exports; add missing export entries only if needed), following packages/ui/AGENTS.md. Run it on this tree, then measure the renderer main chunk size before/after with the existing build tooling for the renderer only (one build, not a full build of all apps) and report both numbers. Running it twice must produce no further change. Keep behavior identical.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 5. The `@openbot/ui` barrel keeps every primitive in the main chunk

**Evidence**

- `packages/ui/src/index.ts:1-32` — the barrel re-exports `./complex` and `./dynamic-island`
  among 32 modules.
- `packages/ui/src/complex.tsx:1-11` — `complex.tsx` imports eleven Kobalte primitives
  (alert-dialog, combobox, context-menu, dialog, dropdown-menu, listbox, popover,
  radio-group, select, tabs, tooltip).
- `packages/ui/src/complex.tsx:153` — `const MenuFocusContext = createContext(…)` runs at
  module scope, so the module has a side effect and cannot be dropped.
- `packages/ui/package.json` — the package has no `"sideEffects": false`, so the bundler
  keeps the module and its Kobalte imports.
- `packages/ui/src/dynamic-island.tsx` — 1212 lines of notch motion that only the macOS
  notch window and the settings preview need.

**Why it matters**

Every renderer file imports primitives from the barrel (`import { Button } from "@openbot/ui"`
in `src/renderer/src/features/conversation/ConversationTimeline.tsx:4`, and so on), so the
whole barrel graph is reachable from the entry chunk. The dynamic island and the unused
Kobalte primitives are paid for at every start.

**Fix**

Keep the barrel for the small primitives, but move `complex` and `dynamic-island` out of it.
Both already have package subpaths (`packages/ui/package.json` exports `"./complex"` and
`"./dynamic-island"`), so the change is to drop the two `export *` lines and point the
callers at the subpath. Add `"sideEffects": false` to `packages/ui/package.json` so the
bundler may drop what is unused.

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
- Run only narrow checks, one at a time: `bun run typecheck:renderer`, `bun run check:ui`, the codemod run twice (second run: no diff), and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
