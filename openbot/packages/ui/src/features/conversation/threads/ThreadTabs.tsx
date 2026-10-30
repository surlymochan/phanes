import { INPUT_LIMITS } from "@openbot/contracts/input-limits";
import {
  Archive,
  ArrowRightToLine,
  CircleX,
  ContextMenu,
  Input,
  PencilLine,
  SlidingTabs,
  Trash2,
  X,
} from "@openbot/ui";
import { useText } from "@openbot/ui/text";
import { createSignal, For, flush, onSettled, Show } from "solid-js";
import { ThreadStatusMark, threadAttention } from "./ThreadStatusMark";
import { MAIN_THREAD_ID, type ThreadHeaderProps, type ThreadItem, threadBusy } from "./thread-types";

/** The width of the soft edge where more tabs are out of view, as in the CSS. */
const FADE = 24;
/** A tab starts to move after the pointer moves this far, so a click stays a click. */
const DRAG_START = 4;
/** While a tab moves near an end of the tabs, the tabs scroll. */
const DRAG_EDGE = 40;
const DRAG_SCROLL_MAX = 12;
/** The actions with which Kobalte selects a tab. */
const CHOICE_EVENTS = ["pointerdown", "click", "keydown"];
/** A short title gets a field wide enough to type a longer one. */
const RENAME_MIN_WIDTH = 160;
/** For this time after the field opens, focus that leaves it comes back. */
const RENAME_FOCUS_GUARD = 400;

/** A tab whose title the user edits, and where its field shows over the tabs. */
interface Rename {
  id: string;
  title: string;
  left: number;
  width: number;
}

/** A tab that the pointer holds. */
interface TabDrag {
  id: string;
  pointerId: number;
  startX: number;
  startScroll: number;
  /** The pointer now. */
  x: number;
  /** False until the pointer moves past `DRAG_START`. */
  moving: boolean;
  /** The place of the tab when the pointer lets it go. */
  target: number;
  frame: number;
}

/** A side thread that the user can rename, when the host can rename threads. */
function renamable(props: ThreadHeaderProps, thread: ThreadItem | undefined): boolean {
  return thread?.kind === "side" && props.onRename !== undefined;
}

function ThreadTab(props: {
  thread: ThreadItem;
  /** The right-click menu of this tab is open. */
  menuOpen: boolean;
  onClose: () => void;
  onMove: (step: -1 | 1) => void;
  onRename: () => void;
}) {
  const { t } = useText();
  // The close control and dragging are for the pointer. A keyboard user closes the focused tab with
  // Delete, moves it with Alt+Shift+Left or Right, and renames it with F2, because a tab list can
  // hold only tabs. Shift+F10 opens the tab menu.
  const stop = (event: Event) => {
    event.preventDefault();
    event.stopPropagation();
  };
  const menuMark = () => (props.menuOpen ? "" : undefined);
  const closeLabel = () => t("conversation.threads.closeTab");
  return (
    <SlidingTabs.Trigger
      value={props.thread.id}
      class="agent-threads-tab no-drag"
      data-thread-id={props.thread.id}
      data-attention={threadAttention(props.thread.status)}
      data-menu-open={menuMark()}
      title={props.thread.title}
      onDblClick={(event: MouseEvent) => {
        if (event.target instanceof Element && event.target.closest(".agent-threads-tab-close")) return;
        props.onRename();
      }}
      onAuxClick={(event: MouseEvent) => {
        if (event.button === 1) props.onClose();
      }}
      onKeyDown={(event: KeyboardEvent) => {
        if (event.key === "Delete" || event.key === "Backspace") {
          event.preventDefault();
          props.onClose();
        } else if (event.key === "F2") {
          event.preventDefault();
          props.onRename();
        } else if (event.altKey && event.shiftKey && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
          // The tab list must not also move the selection.
          stop(event);
          props.onMove(event.key === "ArrowLeft" ? -1 : 1);
        }
      }}
    >
      <span class="agent-threads-tab-title">{props.thread.title}</span>
      <ThreadStatusMark status={props.thread.status} />
      <span
        class="agent-threads-tab-close"
        aria-hidden="true"
        title={closeLabel()}
        onPointerDown={stop}
        onMouseDown={stop}
        onClick={(event) => {
          stop(event);
          props.onClose();
        }}
      >
        <X />
      </span>
    </SlidingTabs.Trigger>
  );
}

/**
 * The open threads as tabs. The selection fill is measured here, not by Kobalte: Kobalte measures it
 * only when the selected tab changes, so a tab that opens, closes or gets its title moves the tabs
 * under a fill that stays in place.
 */
export function ThreadTabs(props: ThreadHeaderProps) {
  const { t } = useText();
  let list: HTMLDivElement | undefined;
  let area: HTMLDivElement | undefined;
  /** The tab of the last right-click. It stays after the menu closes, for the action that closed it. */
  const [menuTarget, setMenuTarget] = createSignal<string | null>(null);
  const [menuOpen, setMenuOpen] = createSignal(false);
  const [renaming, setRenaming] = createSignal<Rename | null>(null);
  /** When the field opened. The app can move focus to the composer just after a tab opens. */
  let renameAt = Number.NEGATIVE_INFINITY;
  const [fill, setFill] = createSignal({ x: 0, width: 0 });
  const [ready, setReady] = createSignal(false);
  /** The tab that the pointer moves, and how far it is from its place. */
  const [dragged, setDragged] = createSignal<{ id: string; x: number } | null>(null);
  /** The fill moves with the selected tab while the pointer moves it. */
  const fillX = () => {
    const drag = dragged();
    return drag?.id === props.activeId ? fill().x + drag.x : fill().x;
  };
  const fillStyle = () => `transform: translateX(${fillX()}px); width: ${fill().width}px;`;
  let move: (id: string, step: -1 | 1) => void = () => {};
  /**
   * Kobalte selects the first tab while the open tab is not in its list yet, which is the case for a
   * tab that opens as it is added. So only a pointer or key action on the tabs opens another tab.
   */
  let choosing = false;
  const initializing = () => (ready() ? undefined : "");

  onSettled(() => {
    const element = list;
    if (!element) return;
    let drag: TabDrag | null = null;
    const edges = () => {
      const max = element.scrollWidth - element.clientWidth;
      element.toggleAttribute("data-more-before", element.scrollLeft > 1);
      element.toggleAttribute("data-more-after", element.scrollLeft < max - 1);
    };
    const measure = () => {
      const tab = element.querySelector<HTMLElement>('.agent-threads-tab[aria-selected="true"]');
      // The agent's own chat has no tab: while it is open, no tab is selected and the fill hides.
      if (!tab) {
        setFill((current) => ({ x: current.x, width: 0 }));
        return;
      }
      setFill({ x: tab.offsetLeft, width: tab.offsetWidth });
      // While a tab moves, the pointer scrolls the tabs.
      if (drag?.moving) return;
      // Only the tabs scroll. `scrollIntoView` also scrolls the app panels.
      // The margin is as wide as the soft edge, so the selected tab stays clear of it.
      const start = tab.offsetLeft - FADE;
      const end = tab.offsetLeft + tab.offsetWidth + FADE - element.clientWidth;
      if (start < element.scrollLeft) element.scrollLeft = start;
      else if (end > element.scrollLeft) element.scrollLeft = end;
      edges();
    };
    let keyAt = Number.NEGATIVE_INFINITY;
    const focusSelected = () => element.querySelector<HTMLElement>('.agent-threads-tab[aria-selected="true"]')?.focus();
    const resize = new ResizeObserver(measure);
    resize.observe(element);
    const observeTabs = () => {
      for (const tab of element.querySelectorAll(".agent-threads-tab")) resize.observe(tab);
    };
    const changes = new MutationObserver(() => {
      observeTabs();
      measure();
      // Delete removes the focused tab. Focus goes to the tab that opens in its place.
      if (document.activeElement === document.body && performance.now() - keyAt < 500) focusSelected();
    });
    changes.observe(element, { subtree: true, childList: true, attributes: true, attributeFilter: ["aria-selected"] });
    observeTabs();
    measure();
    const frame = requestAnimationFrame(() => setReady(true));

    // The app moves focus to the composer when the chat changes. After an arrow key or Delete on the
    // tabs, focus stays on the tabs, so the next key also works.
    const onKey = (event: KeyboardEvent) => {
      keyAt = event.key === "Tab" ? Number.NEGATIVE_INFINITY : performance.now();
    };
    const keepFocus = (event: FocusEvent) => {
      if (performance.now() - keyAt > 500) return;
      if (event.target instanceof Node && element.contains(event.target)) return;
      if (event.target instanceof Element && event.target.closest(".agent-threads-rename")) return;
      focusSelected();
    };
    const onPointer = () => {
      keyAt = Number.NEGATIVE_INFINITY;
    };
    const tabElements = () => [...element.querySelectorAll<HTMLElement>(".agent-threads-tab")];

    /** Puts a tab in a new place. Each tab slides from where it shows now to its new place. */
    const reorder = (id: string, index: number) => {
      const focused = document.activeElement;
      const before = new Map(tabElements().map((tab) => [tab, tab.getBoundingClientRect().left]));
      for (const tab of before.keys()) {
        tab.style.transition = "none";
        tab.style.transform = "";
      }
      setDragged(null);
      props.onMove(id, index);
      flush();
      measure();
      for (const [tab, left] of before) {
        const offset = left - tab.getBoundingClientRect().left;
        if (Math.abs(offset) > 0.5) tab.style.transform = `translateX(${offset}px)`;
      }
      // Start the slide from the old places.
      element.getBoundingClientRect();
      for (const tab of before.keys()) {
        tab.style.transition = "";
        tab.style.transform = "";
      }
      if (focused instanceof HTMLElement && focused !== document.activeElement) focused.focus();
    };
    move = (id, step) => {
      const ids = tabElements().map((tab) => tab.dataset.threadId);
      const index = ids.indexOf(id) + step;
      if (index >= 0 && index < ids.length) reorder(id, index);
    };

    /** Moves the dragged tab with the pointer. The other tabs make room where it will go. */
    const follow = (current: TabDrag) => {
      const items = tabElements().map((tab) => ({ tab, left: tab.offsetLeft, width: tab.offsetWidth }));
      const index = items.findIndex((item) => item.tab.dataset.threadId === current.id);
      const self = items[index];
      const first = items[0];
      const last = items.at(-1);
      if (!self || !first || !last) return;
      const offset = Math.min(
        Math.max(current.x - current.startX + element.scrollLeft - current.startScroll, first.left - self.left),
        last.left + last.width - self.left - self.width,
      );
      const center = self.left + self.width / 2 + offset;
      current.target = items.filter((item, i) => i !== index && item.left + item.width / 2 < center).length;
      const room = self.width + Number.parseFloat(getComputedStyle(element).columnGap || "0");
      items.forEach((item, i) => {
        if (i === index) {
          item.tab.style.transform = `translateX(${offset}px)`;
          return;
        }
        const shift = i > index && i <= current.target ? -room : i < index && i >= current.target ? room : 0;
        item.tab.style.transform = shift === 0 ? "" : `translateX(${shift}px)`;
      });
      setDragged({ id: current.id, x: offset });
    };
    const autoScroll = () => {
      const current = drag;
      if (!current?.moving) return;
      const box = element.getBoundingClientRect();
      const speed =
        current.x < box.left + DRAG_EDGE
          ? -(box.left + DRAG_EDGE - current.x) / 3
          : current.x > box.right - DRAG_EDGE
            ? (current.x - box.right + DRAG_EDGE) / 3
            : 0;
      if (speed !== 0) {
        element.scrollLeft += Math.max(-DRAG_SCROLL_MAX, Math.min(DRAG_SCROLL_MAX, speed));
        follow(current);
      }
      current.frame = requestAnimationFrame(autoScroll);
    };
    const onDragMove = (event: PointerEvent) => {
      const current = drag;
      if (!current || event.pointerId !== current.pointerId) return;
      current.x = event.clientX;
      if (!current.moving) {
        if (Math.abs(current.x - current.startX) < DRAG_START) return;
        current.moving = true;
        element.setAttribute("data-dragging", "");
        element.querySelector(`[data-thread-id="${CSS.escape(current.id)}"]`)?.setAttribute("data-dragged", "");
        current.frame = requestAnimationFrame(autoScroll);
      }
      follow(current);
    };
    const onDragEnd = (event: PointerEvent) => {
      const current = drag;
      if (!current || event.pointerId !== current.pointerId) return;
      drag = null;
      window.removeEventListener("pointermove", onDragMove);
      window.removeEventListener("pointerup", onDragEnd);
      window.removeEventListener("pointercancel", onDragEnd);
      if (!current.moving) return;
      cancelAnimationFrame(current.frame);
      element.removeAttribute("data-dragging");
      element.querySelector("[data-dragged]")?.removeAttribute("data-dragged");
      reorder(
        current.id,
        event.type === "pointercancel"
          ? tabElements().findIndex((tab) => tab.dataset.threadId === current.id)
          : current.target,
      );
    };
    const onDragStart = (event: PointerEvent) => {
      if (event.button !== 0 || !event.isPrimary || drag) return;
      if (!(event.target instanceof Element) || event.target.closest(".agent-threads-tab-close")) return;
      const id = event.target.closest<HTMLElement>(".agent-threads-tab")?.dataset.threadId;
      if (!id || tabElements().length < 2) return;
      drag = {
        id,
        pointerId: event.pointerId,
        startX: event.clientX,
        startScroll: element.scrollLeft,
        x: event.clientX,
        moving: false,
        target: 0,
        frame: 0,
      };
      window.addEventListener("pointermove", onDragMove);
      window.addEventListener("pointerup", onDragEnd);
      window.addEventListener("pointercancel", onDragEnd);
    };

    /** A mouse wheel scrolls the tabs sideways. A trackpad already scrolls them. */
    const onWheel = (event: WheelEvent) => {
      if (element.scrollWidth <= element.clientWidth) return;
      if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
      event.preventDefault();
      element.scrollLeft += event.deltaMode === WheelEvent.DOM_DELTA_LINE ? event.deltaY * 16 : event.deltaY;
    };

    // Kobalte answers the action at the document, so the action ends at the window.
    const endChoice = () => {
      choosing = false;
    };
    // A tab control that stops the event, such as close or move, keeps it from the window: the
    // timer ends the action after the event in that case.
    const choose = (event: Event) => {
      choosing = true;
      window.addEventListener(event.type, endChoice, { once: true });
      setTimeout(endChoice);
    };
    for (const type of CHOICE_EVENTS) element.addEventListener(type, choose, { capture: true });
    element.addEventListener("keydown", onKey);
    element.addEventListener("scroll", edges, { passive: true });
    element.addEventListener("pointerdown", onDragStart);
    element.addEventListener("wheel", onWheel, { passive: false });
    document.addEventListener("focusin", keepFocus);
    document.addEventListener("pointerdown", onPointer, true);
    return () => {
      cancelAnimationFrame(frame);
      resize.disconnect();
      changes.disconnect();
      for (const type of CHOICE_EVENTS) {
        element.removeEventListener(type, choose, { capture: true });
        window.removeEventListener(type, endChoice);
      }
      element.removeEventListener("keydown", onKey);
      element.removeEventListener("scroll", edges);
      element.removeEventListener("pointerdown", onDragStart);
      element.removeEventListener("wheel", onWheel);
      window.removeEventListener("pointermove", onDragMove);
      window.removeEventListener("pointerup", onDragEnd);
      window.removeEventListener("pointercancel", onDragEnd);
      if (drag) cancelAnimationFrame(drag.frame);
      document.removeEventListener("focusin", keepFocus);
      document.removeEventListener("pointerdown", onPointer, true);
    };
  });

  const tabOf = (id: string) =>
    list?.querySelector<HTMLElement>(`.agent-threads-tab[data-thread-id="${CSS.escape(id)}"]`);
  const threadOf = (id: string | null) => props.tabs.find((thread) => thread.id === id);

  /** Opens a text field over the tab, with the tab's title. */
  const startRename = (id: string) => {
    const tab = tabOf(id);
    const thread = threadOf(id);
    if (!tab || !area || !thread || !renamable(props, thread)) return;
    const box = tab.getBoundingClientRect();
    const base = area.getBoundingClientRect();
    const width = Math.max(box.width, RENAME_MIN_WIDTH);
    renameAt = performance.now();
    setRenaming({ id, title: thread.title, width, left: Math.max(0, box.left - base.left) });
  };
  /** Keeps a title that is not empty. Enter and Escape put focus back on the tab. */
  const finishRename = (value: string | null, refocus: boolean) => {
    const edit = renaming();
    if (!edit) return;
    setRenaming(null);
    const title = value?.trim();
    if (title && title !== edit.title) props.onRename?.(edit.id, title);
    if (refocus) requestAnimationFrame(() => tabOf(edit.id)?.focus());
  };
  const renameStyle = (edit: Rename) => `left: ${edit.left}px; width: ${edit.width}px;`;

  /** A right-click outside a tab opens no menu. */
  const pickTarget = (event: MouseEvent) => {
    const id =
      event.target instanceof Element
        ? event.target.closest<HTMLElement>(".agent-threads-tab")?.dataset.threadId
        : undefined;
    if (!id) {
      event.preventDefault();
      return;
    }
    setMenuTarget(id);
  };
  /** A long press opens the menu on touch screens. */
  const pickTouchTarget = (event: PointerEvent) => {
    if (event.pointerType === "mouse" || !(event.target instanceof Element)) return;
    const id = event.target.closest<HTMLElement>(".agent-threads-tab")?.dataset.threadId;
    if (id) setMenuTarget(id);
  };
  const target = () => threadOf(menuTarget()) ?? threadOf(props.activeId);
  const targetId = () => target()?.id ?? props.activeId;
  const targetIndex = () => props.tabs.findIndex((thread) => thread.id === targetId());
  const hasOthers = () => props.tabs.some((thread) => thread.id !== targetId());
  const hasTabsToTheRight = () => targetIndex() < props.tabs.length - 1;
  const isSide = () => target()?.kind === "side";
  const targetBusy = () => {
    const thread = target();
    return thread ? threadBusy(thread.status) : false;
  };
  const canRename = () => renamable(props, target());
  const canDelete = () => isSide() && props.onDelete !== undefined;
  const isMenuTarget = (id: string) => menuOpen() && menuTarget() === id;
  const openChosen = (id: string) => {
    if (choosing) props.onOpen(id);
  };
  /** The last tab leaves with the tab list. Focus then goes to the agent's name, which opens the agent's chat. */
  const closeTab = (id: string) => {
    const heading = area?.closest(".conversation-heading-group");
    props.onClose(id);
    requestAnimationFrame(() => {
      if (area?.isConnected || document.activeElement !== document.body) return;
      heading?.querySelector<HTMLElement>(".conversation-title")?.focus();
    });
  };

  return (
    <div ref={area} class="agent-threads-tabs-area">
      <ContextMenu.Root onOpenChange={setMenuOpen}>
        <ContextMenu.Trigger
          class="agent-threads-tabs-trigger"
          onContextMenu={pickTarget}
          onPointerDown={pickTouchTarget}
        >
          <SlidingTabs.Root class="agent-threads-tabs-root" value={props.activeId} onChange={openChosen}>
            <SlidingTabs.List ref={list} class="agent-threads-tabs" aria-label={t("conversation.threads.open")}>
              <span
                class="ui-sliding-tabs-pill agent-threads-fill"
                aria-hidden="true"
                data-initializing={initializing()}
                style={fillStyle()}
              />
              {/*
                The agent's own chat has no tab, but Kobalte selects the first tab while the selected value
                is not one of its triggers. This trigger never shows, so the agent's chat stays selected.
              */}
              <SlidingTabs.Trigger
                value={MAIN_THREAD_ID}
                class="agent-threads-main-key"
                disabled
                tabindex={-1}
                aria-hidden="true"
              />
              {/* Keyed by id, so a tab keeps its element, focus and drag when its title or state changes. */}
              <For each={props.tabs} keyed={(thread) => thread.id}>
                {(thread) => (
                  <ThreadTab
                    thread={thread()}
                    menuOpen={isMenuTarget(thread().id)}
                    onClose={() => closeTab(thread().id)}
                    onMove={(step) => move(thread().id, step)}
                    onRename={() => startRename(thread().id)}
                  />
                )}
              </For>
            </SlidingTabs.List>
          </SlidingTabs.Root>
        </ContextMenu.Trigger>
        <ContextMenu.Portal>
          <ContextMenu.Content class="agent-threads-tab-menu" aria-label={t("conversation.threads.actions")}>
            <Show when={canRename()}>
              <ContextMenu.Item onSelectAfterClose={() => startRename(targetId())}>
                <PencilLine class="agent-context-icon size-4" aria-hidden="true" />
                <span>{t("common.rename")}</span>
              </ContextMenu.Item>
              <ContextMenu.Separator />
            </Show>
            <ContextMenu.Item onSelect={() => props.onClose(targetId())}>
              <X class="agent-context-icon size-4" aria-hidden="true" />
              <span>{t("conversation.threads.closeTab")}</span>
            </ContextMenu.Item>
            <ContextMenu.Item disabled={!hasOthers()} onSelect={() => props.onCloseOthers(targetId())}>
              <CircleX class="agent-context-icon size-4" aria-hidden="true" />
              <span>{t("conversation.threads.closeOthers")}</span>
            </ContextMenu.Item>
            <ContextMenu.Item disabled={!hasTabsToTheRight()} onSelect={() => props.onCloseToTheRight(targetId())}>
              <ArrowRightToLine class="agent-context-icon size-4" aria-hidden="true" />
              <span>{t("conversation.threads.closeRight")}</span>
            </ContextMenu.Item>
            {/*
              The agent's own chat and a new thread have no archive or delete. A running thread stops first.
              The archive of a delete cancels the queued messages, and Undo cannot bring them back, so a
              queued thread has no delete.
            */}
            <Show when={isSide()}>
              <ContextMenu.Separator />
              <ContextMenu.Item disabled={targetBusy()} onSelect={() => props.onArchive(targetId())}>
                <Archive class="agent-context-icon size-4" aria-hidden="true" />
                <span>{t("conversation.threads.archive")}</span>
              </ContextMenu.Item>
              <Show when={canDelete()}>
                <ContextMenu.Item
                  class="ui-action-menu-danger"
                  disabled={targetBusy() || target()?.status.kind === "queued"}
                  onSelect={() => props.onDelete?.(targetId())}
                >
                  <Trash2 class="agent-context-icon size-4" aria-hidden="true" />
                  <span>{t("common.delete")}</span>
                </ContextMenu.Item>
              </Show>
            </Show>
          </ContextMenu.Content>
        </ContextMenu.Portal>
      </ContextMenu.Root>
      <Show when={renaming()}>
        {(edit) => (
          <Input
            ref={(field: HTMLInputElement) =>
              requestAnimationFrame(() => {
                field.focus();
                field.select();
              })
            }
            class="agent-threads-rename"
            size="sm"
            aria-label={t("conversation.threads.name")}
            maxlength={INPUT_LIMITS.agentTaskTitle}
            defaultValue={edit().title}
            style={renameStyle(edit())}
            onKeyDown={(event: KeyboardEvent & { currentTarget: HTMLInputElement }) => {
              if (event.key === "Enter") {
                event.preventDefault();
                finishRename(event.currentTarget.value, true);
              } else if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                finishRename(null, true);
              }
            }}
            onBlur={(event: FocusEvent & { currentTarget: HTMLInputElement }) => {
              const field = event.currentTarget;
              if (performance.now() - renameAt < RENAME_FOCUS_GUARD) {
                requestAnimationFrame(() => field.focus());
                return;
              }
              finishRename(field.value, false);
            }}
          />
        )}
      </Show>
    </div>
  );
}
