# M10 #62: keyboard access to context menus (a11y findings 1 and 15)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Base commit: b50d5ce925 (head of PR nibiru-labs/openbot#42,
the upstream sync). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Scope

Accessibility findings 1 and 15 below: every right-click menu, Mark all read, and the sidebar free-area menu need a keyboard path (Shift+F10 / ContextMenu key and a visible menu button where the audit says so). Shared controls live in packages/ui (read packages/ui/AGENTS.md); keep existing visuals and behavior for mouse users.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

### 1. Every right-click menu is out of reach for a keyboard

**Evidence**

- `node_modules/@kobalte/core/dist/context-menu/DxRt6yAn.js:96-110`: the Kobalte
  `ContextMenuTrigger` handles only `onContextMenu`. It has no key handler for the `ContextMenu`
  key or `Shift+F10`, and it sets no `aria-haspopup`.
- `packages/ui/src/features/sidebar/SidebarAgentRow.tsx:84-107`: the agent row is a
  `ContextMenu.Trigger as="button"`. Its actions live in
  `packages/ui/src/features/sidebar/SidebarAgentContextMenu.tsx:26-80`: pin, unpin, move to another
  section, rename, duplicate, hide, restore and delete.
- `packages/ui/src/features/sidebar/SidebarChannelRow.tsx:51-99` and
  `packages/ui/src/features/sidebar/SidebarChannelContextMenu.tsx:18-49`: the same for a channel.
- `packages/ui/src/features/sidebar/SidebarSectionHeader.tsx:41-101`: rename section, move section
  up, move section down and delete section. The header button itself only collapses the section.
- `packages/ui/src/features/browser/BrowserTabStrip.tsx:389-450` and
  `packages/ui/src/features/browser/BrowserTabMenu.tsx:26-76`: duplicate tab, close left, close
  right and close others.
- `packages/ui/src/features/diagrams/DiagramBoard.tsx:437-667`.

**Why it matters**

A keyboard user, a switch device user and a screen reader user cannot rename, pin, duplicate,
hide, delete or move a chat. They cannot rename, move or delete a sidebar section. They cannot
duplicate a browser tab or close the tabs beside it. These are the main management actions of the
sidebar, so the feature is not usable without a pointer. This breaks WCAG 2.1.1 Keyboard (level A).

**Fix**

The repository already holds the answer. `packages/ui/src/features/servers/ServerRail.tsx:427-441`
turns the `ContextMenu` key and `Shift+F10` into a synthetic `contextmenu` event, and
`packages/ui/src/features/servers/ServerRail.tsx:408` and
`packages/ui/src/features/servers/ServerMenu.tsx:51-53,138` publish it with `aria-keyshortcuts`.

Move that helper into `packages/ui/src/complex.tsx`, next to the other `ContextMenu` adapters. Give
the `ContextMenu.Trigger` in `packages/ui/src/complex.tsx` an `onKeyDown` that dispatches the same
event, and set `aria-keyshortcuts="Shift+F10"` on the trigger. The six trigger sites then work
without a change.

**Size** M. One shared adapter, one helper, six call sites.

### 15. "Mark all read" and the sidebar free-area menu have no keyboard path

**Evidence**

- `packages/ui/src/features/sidebar/SidebarNav.tsx:88`: the trigger is a `ContextMenu.Trigger` with
  no `as` prop. Kobalte renders a `div`
  (`node_modules/@kobalte/core/dist/context-menu/DxRt6yAn.js:135-136`). It is not focusable and it
  has no role, so the `aria-label` on it is never read.
- `packages/ui/src/features/sidebar/SidebarNav.tsx:109-113`: "Mark all read" is only here.
- `src/renderer/src/features/sidebar/WorkspaceSidebar.tsx:153` and
  `src/renderer/src/features/web-client/WebWorkspace.tsx:1474`: the two call sites pass
  `onMarkAllRead`, and no other control calls it.

**Why it matters**

The user cannot mark all channels as read without a mouse. The `aria-label` on the trigger gives
a false promise of a name that no assistive technology will read. This breaks WCAG 2.1.1 Keyboard
(level A) and WCAG 4.1.2 Name, Role, Value (level A).

**Fix**

Give the trigger a keyboard path, as in finding 1. Put "Mark all read" and "Show deleted chats" in
a menu button in the sidebar header as well, so the actions have a focusable home.

**Size** S for the keyboard path. M for the extra menu button.

## Rules

- First confirm each finding against the current code. If upstream already fixed one, do not change it: report
  "already fixed" with the file and line that show it. That is a valid result.
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`.
- Do not change released Team API wire contracts, IPC contract names, or migrations.
- Stay inside this worktree. Do not use GUI apps or Computer Use. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: `bun run typecheck:renderer` and `bun run check:ui`, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
