import type { JSX } from "@solidjs/web";
import { onCleanup, onSettled } from "solid-js";
import { prefersReducedMotion } from "./utils";

// Kobalte removes the content of a menu or a popover at once when it closes, so a CSS exit
// animation cannot play. When the content closes, an inert copy takes its place and plays the exit
// that the stylesheet gives to content without data-expanded.
//
// When the pointer moves from one submenu trigger to the next, one submenu panel stays on screen:
// the copy of the old submenu waits for the new one, then both move from the old place to the new
// place, the size follows the new items, and the old items fade out while the new items fade in.
// Tooltips in one group of triggers, such as the buttons of a rail, move in the same way, also
// from a label to a card.

const SWAP_MS = 200;
const SWAP_EASE = "cubic-bezier(0.32, 0.72, 0, 1)";
const SWAP_BLUR = "blur(2px)";
// When the size of the panel changes by more than this factor, such as from a label to a card, the
// panel clips much of the content while it moves. The old items then leave in the first part of the
// move, and the new items come in when the panel is almost at its new size. The offsets are parts
// of the eased move, not of its time.
const LARGE_RESIZE = 1.5;
const LARGE_RESIZE_OUT_END = 0.3;
const LARGE_RESIZE_IN_START = 0.75;
// Kobalte opens a submenu 100 ms after the pointer reaches its trigger.
const HANDOFF_WAIT_MS = 180;
// Kobalte can open the next tooltip before the old one closes. The old one can still hand off to it
// within this time.
const LATE_HANDOFF_MS = 50;
const EXIT_FALLBACK_MS = 400;
const PLACEMENT_FRAMES = 6;

interface HeldSubmenu {
  copy: HTMLElement;
  panel: HTMLElement;
  /** The menu or the group of triggers whose next panel takes the place of this one. */
  group: Element;
  /** Whether the panels are tooltips, whose content can change much from one to the next. */
  own: boolean;
  /** Where the panel was, when it closed while it moved to its own place. */
  moving?: MovingState | undefined;
  /** Where the copy is without the hold: the place that the panel moved to. */
  natural?: DOMRect;
  /** The widths of the items of the copy at that place. */
  itemWidths?: string[];
  timer: ReturnType<typeof setTimeout>;
}

/** The trigger of content that has no sub-trigger, such as a tooltip, and the group of its trigger. */
export interface MotionPlace {
  trigger: Element;
  group: Element;
}

let held: HeldSubmenu | undefined;

/** A panel that opened when no copy waited. A copy that comes soon after still moves into it. */
let unmatched: { panel: HTMLElement; group: Element; since: number } | undefined;

/** The open panels that give their own group, such as tooltips. */
const openInGroup = new Map<HTMLElement, Element>();

interface MovingState {
  rect: DOMRect;
  /** The opacity of the items, which fade in while the panel moves. */
  itemOpacity: number;
  /** The surface that is nearer to the panel's surface at this point of the move. */
  surface: Keyframe;
}

/** The move of a panel that took the place of the one before. */
const moves = new WeakMap<
  HTMLElement,
  { from: DOMRect; to: DOMRect; animation: Animation; large: boolean; surfaces: [Keyframe, Keyframe] }
>();

/**
 * Where a panel is in its move. Content can leave the page before its cleanup runs, so the place
 * comes from the progress of the move and not from a measure.
 */
function movingState(panel: HTMLElement): MovingState | undefined {
  const move = moves.get(panel);
  const progress = move?.animation.effect?.getComputedTiming().progress;
  if (!move || move.animation.playState === "finished" || progress == null) return undefined;
  const mix = (from: number, to: number) => from + (to - from) * progress;
  return {
    rect: new DOMRect(
      mix(move.from.left, move.to.left),
      mix(move.from.top, move.to.top),
      mix(move.from.width, move.to.width),
      mix(move.from.height, move.to.height),
    ),
    itemOpacity: move.large ? Math.max(0, (progress - LARGE_RESIZE_IN_START) / (1 - LARGE_RESIZE_IN_START)) : progress,
    surface: move.surfaces[progress < 0.5 ? 0 : 1],
  };
}

/** The scroll offsets of a copy, by the index of the element in the copy. A clone does not keep them. */
const scrollOffsets = new WeakMap<HTMLElement, Array<[index: number, top: number, left: number]>>();

/** Puts a copy on the page, and gives back the scroll offsets that the insertion resets. */
function insertCopy(copy: HTMLElement, parent: Node): void {
  parent.appendChild(copy);
  const offsets = scrollOffsets.get(copy);
  if (!offsets) return;
  const elements = copy.querySelectorAll("*");
  for (const [index, top, left] of offsets) {
    const element = elements[index];
    if (!element) continue;
    element.scrollTop = top;
    element.scrollLeft = left;
  }
}

function motionAllowed(element: HTMLElement): boolean {
  return typeof element.animate === "function" && !prefersReducedMotion();
}

function positionerOf(panel: HTMLElement): HTMLElement | null {
  return panel.closest<HTMLElement>("[data-popper-positioner]");
}

function triggerOf(panel: HTMLElement): HTMLElement | null {
  const id = panel.getAttribute("aria-labelledby");
  return id ? document.getElementById(id) : null;
}

/** The menu that holds a submenu trigger, or null for the trigger of a top-level menu. */
function parentMenuOf(trigger: HTMLElement | null): Element | null {
  return trigger?.closest(".ui-action-menu") ?? null;
}

function playExit(copy: HTMLElement, panel: HTMLElement): void {
  delete panel.dataset.menuHold;
  // Content with no exit animation in the stylesheet goes at once.
  if (panel.getAnimations().length === 0) {
    copy.remove();
    return;
  }
  const fallback = setTimeout(() => copy.remove(), EXIT_FALLBACK_MS);
  panel.addEventListener(
    "animationend",
    () => {
      clearTimeout(fallback);
      copy.remove();
    },
    { once: true },
  );
}

function releaseHeld(): void {
  if (!held) return;
  clearTimeout(held.timer);
  playExit(held.copy, held.panel);
  held = undefined;
}

/**
 * Whether the copy waits for the next panel. A submenu waits only when the pointer is already on
 * the next trigger. A tooltip also waits while the pointer crosses the space between two triggers.
 */
function nextPanelExpected(panel: HTMLElement, own: MotionPlace | undefined): Element | null {
  if (own) {
    const waits = own.group.isConnected && own.group.matches(":hover") && !own.trigger.matches(":hover");
    return waits ? own.group : null;
  }
  const trigger = triggerOf(panel);
  const parentMenu = parentMenuOf(trigger);
  const nextTriggerHovered =
    trigger !== null &&
    parentMenu?.isConnected === true &&
    Array.from(parentMenu.querySelectorAll("[aria-expanded]")).some(
      (element) => element !== trigger && element.matches(":hover"),
    );
  return nextTriggerHovered ? parentMenu : null;
}

/** Puts an inert copy of closed content in its place and plays its exit. */
function leave(panel: HTMLElement, place: Node | null, own: MotionPlace | undefined): void {
  const positioner = positionerOf(panel);
  if (!positioner) return;
  // The copy is made now, while the content is still whole.
  const copy = positioner.cloneNode(true);
  if (!(copy instanceof HTMLElement)) return;
  const offsets: Array<[number, number, number]> = [];
  for (const [index, element] of Array.from(positioner.querySelectorAll("*")).entries()) {
    if (element.scrollTop !== 0 || element.scrollLeft !== 0)
      offsets.push([index, element.scrollTop, element.scrollLeft]);
  }
  if (offsets.length > 0) scrollOffsets.set(copy, offsets);
  const copyPanel = panel.id ? copy.querySelector<HTMLElement>(`#${CSS.escape(panel.id)}`) : null;
  if (!copyPanel) return;
  copy.inert = true;
  copy.setAttribute("aria-hidden", "true");
  copy.style.pointerEvents = "none";
  // An SVG keeps its ids: a shape it draws through `url(#id)`, such as an avatar's mask, paints as
  // its bare box without them. The copy goes in only once the original has left the page.
  for (const element of copy.querySelectorAll("[id]"))
    if (!(element instanceof SVGElement)) element.removeAttribute("id");
  copyPanel.removeAttribute("data-expanded");
  delete copyPanel.dataset.menuSwapIn;

  const group = nextPanelExpected(panel, own);
  if (group) {
    releaseHeld();
    copyPanel.dataset.menuHold = "";
    held = {
      copy,
      panel: copyPanel,
      group,
      own: own !== undefined,
      moving: movingState(panel),
      timer: setTimeout(releaseHeld, HANDOFF_WAIT_MS),
    };
  }

  // The cleanup can run before the content leaves the page. Solid removes it in the same pass, so
  // the copy goes in after that pass and before the next paint.
  queueMicrotask(() => {
    if (positioner.isConnected) {
      if (held?.copy === copy) releaseHeld();
      return;
    }
    if (held?.copy === copy) {
      insertCopy(copy, document.body);
      holdInPlace(held);
      // A panel that does not match keeps its entry, so that its own timer shows it.
      const next = unmatched;
      if (next?.group === held.group && next.panel.isConnected && performance.now() - next.since < LATE_HANDOFF_MS) {
        unmatched = undefined;
        handOff(held, next.panel);
      }
      return;
    }
    // Content without a portal goes back into its parent, so that its position stays right.
    insertCopy(copy, place?.isConnected ? place : document.body);
    playExit(copy, copyPanel);
  });
}

const translate = (to: DOMRect, from: DOMRect) => `translate(${to.left - from.left}px, ${to.top - from.top}px)`;
const size = (rect: DOMRect) => ({ width: `${rect.width}px`, height: `${rect.height}px` });

/**
 * The width of each item of a panel at its own size. While the size of a tooltip changes, the items
 * keep these widths, so that content such as a card does not wrap again on each frame.
 */
const itemWidths = (panel: HTMLElement) =>
  Array.from(panel.children, (item) => `${item.getBoundingClientRect().width}px`);

/** The keyframe part that keeps the width of an item. Only the items of a tooltip keep it. */
const itemWidth = (from: HeldSubmenu, width: string | undefined): Keyframe => (from.own ? { width } : {});

/**
 * A copy shows its content at rest. When the panel closed while it moved, the copy waits where the
 * panel was, with its items as faint as they were, so the next panel moves on from there.
 */
function holdInPlace(from: HeldSubmenu): void {
  const natural = from.panel.getBoundingClientRect();
  from.natural = natural;
  const widths = itemWidths(from.panel);
  from.itemWidths = widths;
  const moving = from.moving;
  if (!moving) return;
  const hold: KeyframeAnimationOptions = { duration: 0, fill: "forwards" };
  from.panel.animate([{ transform: translate(moving.rect, natural), ...size(moving.rect), ...moving.surface }], hold);
  for (const [index, item] of Array.from(from.panel.children).entries())
    item.animate([{ opacity: moving.itemOpacity, ...itemWidth(from, widths[index]) }], hold);
}

/** Whether the size of a panel changes so much that its content shows clipped while it moves. */
function largeResize(before: DOMRect, after: DOMRect): boolean {
  const factor = (a: number, b: number) => (a > 0 && b > 0 ? Math.max(a / b, b / a) : Number.POSITIVE_INFINITY);
  return Math.max(factor(before.width, after.width), factor(before.height, after.height)) > LARGE_RESIZE;
}

/**
 * The surface of a panel. Two panels with different surfaces, such as a label and a card, change
 * from one to the other while they move.
 */
function surfaceOf(panel: HTMLElement): Keyframe {
  const style = getComputedStyle(panel);
  return { backgroundColor: style.backgroundColor, boxShadow: style.boxShadow, borderRadius: style.borderRadius };
}

/** Kobalte places the positioner after the first frame. Runs `then` once it has a place. */
function whenPlaced(panel: HTMLElement, then: () => void, frame = 0): void {
  const positioner = positionerOf(panel);
  if ((positioner && positioner.style.transform !== "") || frame >= PLACEMENT_FRAMES) {
    then();
    return;
  }
  requestAnimationFrame(() => whenPlaced(panel, then, frame + 1));
}

function handOff(from: HeldSubmenu, panel: HTMLElement): void {
  clearTimeout(from.timer);
  held = undefined;
  panel.dataset.menuHandoff = "";
  whenPlaced(panel, () => {
    if (!panel.isConnected) {
      playExit(from.copy, from.panel);
      return;
    }
    // The swap state comes before the handoff state goes: without either, the open animation starts
    // and the panel measures at its first-frame scale. It stays while the submenu is open, because
    // removing it would start the open animation again.
    panel.dataset.menuSwapIn = "";
    delete panel.dataset.menuHandoff;
    const natural = from.natural ?? from.panel.getBoundingClientRect();
    const before = from.moving?.rect ?? natural;
    const after = panel.getBoundingClientRect();
    const widths = itemWidths(panel);
    const oldWidths = from.itemWidths ?? itemWidths(from.panel);
    const timing: KeyframeAnimationOptions = { duration: SWAP_MS, easing: SWAP_EASE };

    // The old copy stays on top: its items leave over the new panel. Its surface is read before
    // the swap state makes it transparent.
    insertCopy(from.copy, document.body);
    const oldSurface = surfaceOf(from.panel);
    const newSurface = surfaceOf(panel);
    // The hold keeps a surface, which would cover the transparent surface of the swap state.
    for (const animation of from.panel.getAnimations()) animation.cancel();
    from.panel.dataset.menuSwapOut = "";
    const large = from.own && largeResize(before, after);

    const move = panel.animate(
      [
        { transform: translate(before, after), ...size(before), ...oldSurface },
        { transform: "none", ...size(after), ...newSurface },
      ],
      timing,
    );
    moves.set(panel, { from: before, to: after, animation: move, large, surfaces: [oldSurface, newSurface] });
    // The panel clips its content while its size changes, and can scroll again after the move.
    panel.dataset.menuMoving = "";
    const moved = () => delete panel.dataset.menuMoving;
    move.onfinish = moved;
    move.oncancel = moved;
    // Kobalte places the panel again when its size changes. A panel aligned to the end or the center
    // of its trigger then moves on each frame and stops at a place for a height it does not have.
    // The positioner keeps the height of the panel at its new place.
    const height = `${after.height}px`;
    positionerOf(panel)?.animate([{ height }, { height }], timing);
    for (const [index, item] of Array.from(panel.children).entries()) {
      const width = itemWidth(from, widths[index]);
      const hidden = { opacity: 0, filter: SWAP_BLUR, ...width };
      item.animate(
        [
          hidden,
          ...(large ? [{ ...hidden, offset: LARGE_RESIZE_IN_START }] : []),
          { opacity: 1, filter: "blur(0px)", ...width },
        ],
        timing,
      );
    }

    from.panel.animate(
      [
        { transform: translate(before, natural), ...size(before) },
        { transform: translate(after, natural), ...size(after) },
      ],
      { ...timing, fill: "forwards" },
    ).onfinish = () => from.copy.remove();
    for (const [index, item] of Array.from(from.panel.children).entries()) {
      const width = itemWidth(from, oldWidths[index]);
      item.animate(
        [
          { opacity: from.moving?.itemOpacity ?? 1, filter: "blur(0px)", ...width },
          { opacity: 0, filter: SWAP_BLUR, ...width, ...(large ? { offset: LARGE_RESIZE_OUT_END } : {}) },
          ...(large ? [{ opacity: 0, filter: SWAP_BLUR, ...width }] : []),
        ],
        { ...timing, fill: "forwards" },
      );
    }
  });
}

/**
 * Gives menu content its exit animation, and the move from one submenu to the next. Call it in a
 * component that closes with the content, such as a wrapper inside a portal. The returned ref goes
 * on the content element. Content without a sub-trigger, such as a tooltip, gives `own`, so that it
 * moves to the next trigger of its group in the same way.
 */
export function useMenuMotion(own?: () => MotionPlace | undefined): (element: HTMLElement) => void {
  let panel: HTMLElement | undefined;
  let place: Node | null = null;
  onCleanup(() => {
    if (panel) openInGroup.delete(panel);
    if (panel && motionAllowed(panel)) leave(panel, place, own?.());
  });
  return (element) => {
    panel = element;
    if (!motionAllowed(element)) return;
    // When the next panel opens before the one in the same group closes, it stays hidden until the
    // old one hands off to it, so that it does not show at its own place for one frame first.
    const ownGroup = own?.()?.group;
    if (ownGroup) {
      if (Array.from(openInGroup.values()).includes(ownGroup)) element.dataset.menuHandoff = "";
      openInGroup.set(element, ownGroup);
    }
    requestAnimationFrame(() => {
      if (!element.isConnected) return;
      place = positionerOf(element)?.parentNode ?? null;
      const from = held;
      const group = own ? own()?.group : parentMenuOf(triggerOf(element));
      if (from && group === from.group) {
        handOff(from, element);
        return;
      }
      if (!group) return;
      unmatched = { panel: element, group, since: performance.now() };
      // No copy came: the panel opens in the usual way.
      setTimeout(() => {
        if (unmatched?.panel !== element) return;
        unmatched = undefined;
        delete element.dataset.menuHandoff;
      }, LATE_HANDOFF_MS);
    });
  };
}

/**
 * The exit animation for content that has no portal, such as a popover in place. Put it among the
 * children of the content, because the children close with the content and the wrapper does not.
 */
export function ContentExitMotion(props: { panel: () => HTMLElement | undefined }): JSX.Element {
  const attach = useMenuMotion();
  onSettled(() => {
    const panel = props.panel();
    if (panel) attach(panel);
  });
  return undefined;
}
