import type { TooltipRootOptions } from "@kobalte/core/tooltip";
import type { JSX } from "@solidjs/web";
import { createEffect, createSignal } from "solid-js";
import { Tooltip } from "./complex";
import { type MotionPlace, useMenuMotion } from "./menu-motion";

const LABEL_OPEN_DELAY = 150;
const LABEL_CLOSE_DELAY = 0;

export interface MotionTooltipProps {
  /**
   * What the panel shows. Without `class`, it is a label on a menu surface. With `class`, the
   * elements in it are the items that fade while the panel moves to the next trigger.
   */
  content: JSX.Element;
  /** The surface of a panel that is not a label, such as a card. */
  class?: string;
  placement?: TooltipRootOptions["placement"];
  openDelay?: number;
  closeDelay?: number;
  /** Keeps the panel closed, such as while the trigger's menu is open. */
  suppressed?: boolean;
  children: JSX.Element;
}

/** The panel. It closes with the portal, so the menu motion can play its exit. */
function MotionTooltipContent(props: {
  place: () => MotionPlace | undefined;
  class: string | undefined;
  children: JSX.Element;
}) {
  const motion = useMenuMotion(props.place);
  return (
    <Tooltip.Content
      ref={motion}
      class={props.class ? `ui-motion-tooltip ${props.class}` : "ui-motion-tooltip ui-motion-tooltip-label"}
    >
      {/* The label is an element, so that it can fade while the panel moves. */}
      {props.class ? props.children : <span>{props.children}</span>}
    </Tooltip.Content>
  );
}

/**
 * A tooltip that opens on hover and on keyboard focus, and opens and closes with the motion of a
 * submenu. The tooltips whose triggers have one parent element share one panel: when the pointer
 * moves to the next trigger, the panel moves to it, its size changes to the next content, and the
 * content crossfades. A label and a card can follow each other in this way.
 */
export function MotionTooltip(props: MotionTooltipProps) {
  const [open, setOpen] = createSignal(false);
  // While the panel is suppressed, Kobalte does not close it, so it would open again when the
  // suppression ends, also with the pointer away.
  createEffect(
    () => props.suppressed,
    (suppressed) => {
      if (suppressed) setOpen(false);
    },
  );
  let trigger: HTMLElement | undefined;
  const place = (): MotionPlace | undefined => {
    const group = trigger?.parentElement;
    return trigger && group ? { trigger, group } : undefined;
  };
  return (
    <Tooltip.Root
      open={open() && !props.suppressed}
      onOpenChange={setOpen}
      placement={props.placement ?? "right"}
      gutter={10}
      openDelay={props.openDelay ?? LABEL_OPEN_DELAY}
      closeDelay={props.closeDelay ?? LABEL_CLOSE_DELAY}
      skipDelayDuration={300}
    >
      <Tooltip.Trigger
        as="div"
        ref={(element: HTMLElement) => {
          trigger = element;
        }}
        class="ui-motion-tooltip-trigger"
        onFocusIn={(event: FocusEvent) => {
          if (event.target instanceof Element && event.target.matches(":focus-visible")) setOpen(true);
        }}
        onFocusOut={() => setOpen(false)}
      >
        {props.children}
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <MotionTooltipContent place={place} class={props.class}>
          {props.content}
        </MotionTooltipContent>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}
