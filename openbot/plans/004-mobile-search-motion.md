# 004 — Fade the mobile search results and notices in and out

- **Status**: DONE
- **Commit**: 2f9848045 (with the uncommitted mobile search work of branch `t3code/mobile-search-results`)
- **Severity**: MEDIUM
- **Category**: Missed opportunities, Accessibility, Purpose & frequency
- **Estimated scope**: 1 new shared component, 4 changed files, 1 setting with its text in 9 languages

## Problem

The search sheet (`apps/mobile/src/features/search/screens/search-agents-screen.tsx`) swaps whole states
with no transition. `view.state` selects one of five branches. When it changes, the results, "Searching…",
"Could not load all results", the hint and "No matching results" replace each other in one frame:

```tsx
// apps/mobile/src/features/search/screens/search-agents-screen.tsx — current
{view.state === "results" ? (
  <>{/* sections, then a footer: loading, failed or Show more */}</>
) : view.state === "loading" ? (
  <SearchNotice title={t("mobile.search.loading")} />
) : view.state === "error" ? ( /* … */ ) : view.state === "hint" ? ( /* … */ ) : ( /* empty */ )}
```

Two more seams make it worse:

- Each new query replaces the host results with an empty list until the host answers, so typing shows
  results → "Searching…" → results.
- A fast host answer shows "Searching…" for one or two frames.

The search has no motion setting, so the Animations settings cannot turn the change off.

## Target

Three motion modes, from settings:

| Mode | When | Swap in | Swap out | Rows and sections |
| --- | --- | --- | --- | --- |
| `blur` | `searchTransitions` on, motion not reduced | opacity 0→1, translateY 3→0, blur veil 1→0, 240ms | opacity 1→0, blur veil 0→1, 180ms | in: opacity 0→1, translateY 6→0, scale 0.98→1, 220ms; out: opacity 1→0, scale 1→0.98, 160ms; move: `LinearTransition` 240ms; a section that comes in also has the blur veil 1→0, 240ms |
| `fade` | `searchTransitions` on, motion reduced (system or the Animations switch) | opacity 0→1, 160ms | opacity 1→0, 120ms | in: opacity 0→1, 160ms; out: opacity 1→0, 120ms; no move |
| `none` | `searchTransitions` off | instant | instant | none |

Decision (2026-10-12, developer, after a device check): rows and sections get enter, exit and move
motion. This replaces the first version of this plan, which gave them only a 160ms fade in because
typing changes them often. Rows get no blur: many rows can come in at the same time, and each blur
view is a render pass.

- Easing for every timing: `Easing.bezier(0.23, 1, 0.32, 1)`, the strong ease-out that
  `apps/mobile/src/shared/components/blur-reveal.tsx:15` and `chat-activity-row.tsx` already use.
- Every timing uses `reduceMotion: ReduceMotion.Never`. The mode already decides what reduced motion
  removes: a fade carries the state change, so it stays.
- Blur veil, iOS only: `expo-blur` `BlurView`, `intensity={16}`, `tint="systemUltraThinMaterial"`.
  Android gets the same opacity and movement with no veil (decision of 2026-10-12, after a device
  check): its `BlurTargetView` moves the children into a native view of its own, which stops the
  layout animations of the rows, and a `BlurView` that mounts paints its tint and the window
  background for a frame. Do not use `BlurTargetView` here.
  React Native 0.86 on iOS draws `filter: blur()` only behind the experimental
  `enableSwiftUIBasedFilters` flag, so do not use the style filter. Mount the veil only while a layer
  moves; an idle `BlurView` still costs a render pass.
- The leaving layer stays absolutely positioned at the top of the swap container, below the new
  layer, until its animation ends. The new layer is in the flow, so the container takes its height at
  once.
- "Searching…" waits 200ms before it comes in. An answer inside that time never shows it.
- The host searches keep the previous results while the next query loads, when the next query
  continues or shortens the previous one on the same server (a `placeholderData` function). A new word
  after a cleared field starts with no results, not with the results of the earlier search.
- The swap layer wraps its content in `LayoutAnimationConfig skipEntering skipExiting`: items do not
  come in with a layer that comes in, and do not go out after a layer that has gone.
- `LayoutAnimationConfig skipEntering` around each swap layer and around the first render of the
  sheet: a row does not fade in again inside a layer that is already fading in, or while the sheet opens.

## Repo conventions to follow

- Motion preferences live in `apps/mobile/src/features/settings/model/motion.ts`. Add
  `searchTransitions: boolean` to `MotionPreferences`, `DEFAULTS` (true) and `MOTION_PREFERENCE_NAMES`.
  `decodeMotionPreferences` keeps the default for a stored value that does not have the field, so no
  storage migration is necessary.
- Each animation has a switch in `apps/mobile/src/features/settings/screens/animations-settings-screen.tsx`
  with a label and a footer key in `packages/i18n/src/messages/<locale>/mobile/settings.ts`.
- `useReducedMotion()` from `@/shared/lib/motion` is true for system Reduce Motion and for the
  Animations switch off.
- Exemplar of a retained exit with a blur veil: `apps/mobile/src/shared/components/blur-reveal.tsx`.
- Exemplar of layout animation worklets with these timings: `chat-activity-row.tsx` (`activityEntering`).
- The React Compiler is on (`apps/mobile/app.json` `reactCompiler`). Do not read refs during render.
  Keep the previous children with the "state from the previous render" pattern.

## Steps

1. `motion.ts`: add `searchTransitions` as above.
2. `animations-settings-screen.tsx`: add the switch after `textReveal`, with
   `mobile.settings.animations.searchTransitions` ("Animate search results") and
   `mobile.settings.animations.searchTransitionsFooter`. Add the text in all 9 languages.
3. New `apps/mobile/src/shared/components/blur-swap.tsx`: `BlurSwap({ swapKey, mode, enterDelay, children })`
   and the `SwapMode` type. It keeps `{ key, node, leaving, enters }` layers in state. A new `swapKey`
   marks the current layers as leaving and adds a new layer that enters (in `none` mode, it drops
   the old layers). The same key with new children replaces the node of the last layer. A leaving
   layer removes itself when its animation ends. The first layer does not enter.
4. `search-agents-screen.tsx`:
   - Compute the mode from `useMotionPreferences((state) => state.searchTransitions)` and `useReducedMotion()`.
   - Wrap the state branches in `<BlurSwap swapKey={`${filter}:${view.state}`}>`.
   - Wrap the footer of the results in its own `BlurSwap`, keyed by `loading`, `failed`, `more`
     or `none`, with `enterDelay` 200 for `loading`.
   - Give both infinite queries a `placeholderData` function that returns the previous data only when
     the previous key has the same scope and its query is a prefix of the new one, or the reverse.
     While the typed query and the debounced query differ, keep the shown items with status
     `loading`; do not pass an empty list.
5. `search-agents-screen.tsx`: give each section and each row `entering`, `exiting` and `layout` from
   `swapItemMotion(mode)` in `blur-swap.tsx`, wrap the section content in `BlurEnter`, and give the
   footer a `layout` transition so it moves with the sections.

## Boundaries

- Do NOT touch `blur-reveal.tsx` or its consumers.
- Do NOT animate the search field, the sheet or the filter button.
- Do NOT add dependencies.

## Verification

- **Mechanical**: Biome on the changed files; `bun run test:desktop -- apps/mobile/src/features/search/model/mobile-search.test.ts --maxWorkers=1`;
  `bun run i18n:check`. The pre-commit hook runs the mobile typecheck.
- **Feel check** (device):
  - Type a word that has no match: "No matching results" comes in with a short blur. It does not
    flash "Searching…" first when the host answers fast.
  - Type in Messages: the old results stay until the new ones replace them. The list does not empty
    and fill again on each pause.
  - Change the filter: the list blurs out while the new one blurs in. No frame shows both lists sharp
    at full opacity.
  - Turn off Animations, then Reduce Motion: the swaps only fade. Turn off "Animate search results":
    the swaps are instant.
  - Record the screen and step frame by frame: the veil is on only during the 240ms or 180ms of a swap.
- **Done when**: no state change in the search sheet replaces content in one frame in `blur` or `fade`
  mode, and `none` mode has no animation.
