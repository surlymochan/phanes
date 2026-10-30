import { Button, Plus } from "@openbot/ui";
import { useText } from "@openbot/ui/text";
import { createSignal, onSettled, Show } from "solid-js";
import { ThreadMenu } from "./ThreadMenu";
import { ThreadTabs } from "./ThreadTabs";
import type { ThreadHeaderProps, ThreadRowMode } from "./thread-types";

/** Below this free width a full tab does not fit, and the tabs become one menu button. */
const COMPACT_BELOW = 200;
/** Below this free width the menu button has no space for a title. */
const MINIMAL_BELOW = 72;

function rowMode(free: number): ThreadRowMode {
  if (free < MINIMAL_BELOW) return "minimal";
  if (free < COMPACT_BELOW) return "compact";
  return "tabs";
}

function NewThreadButton(props: { disabled: boolean; onNew: () => void }) {
  const { t } = useText();
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      class="agent-threads-icon-button no-drag"
      aria-label={t("conversation.threads.new")}
      title={t("conversation.threads.new")}
      disabled={props.disabled}
      onClick={() => props.onNew()}
    >
      <Plus aria-hidden="true" />
    </Button>
  );
}

/**
 * The tabs, "+" and the thread list. In a narrow header the tabs and the list button become one
 * button with the open thread, and "+" follows it. The heading group takes the free width of the
 * header whatever it holds, so its width decides the mode and the mode does not change it again.
 * The agent's own chat has no tab: the agent's name before the row opens it. With no side thread
 * tab, only "+" and the thread list show.
 */
function ThreadRow(props: ThreadHeaderProps) {
  let row: HTMLDivElement | undefined;
  const [measured, setMode] = createSignal<ThreadRowMode>("tabs");
  // With no tab to show, the compact button would only repeat the agent's name.
  const mode = (): ThreadRowMode => (props.tabs.length === 0 && measured() === "compact" ? "tabs" : measured());
  onSettled(() => {
    const group = row?.parentElement;
    if (!row || !group) return;
    const measure = () => {
      if (!row) return;
      const buttons = row.querySelectorAll(".agent-threads-icon-button");
      const button = buttons[0]?.getBoundingClientRect().width ?? 0;
      // The free width without "+" and the list button, which the full row has.
      const free = group.getBoundingClientRect().right - row.getBoundingClientRect().left - 2 * (button + 3);
      setMode(rowMode(free));
    };
    const observer = new ResizeObserver(measure);
    observer.observe(group);
    measure();
    return () => observer.disconnect();
  });
  return (
    <div ref={row} class="agent-threads-header" data-mode={mode()}>
      <Show when={mode() === "tabs"}>
        <Show when={props.tabs.length > 0}>
          <ThreadTabs {...props} />
        </Show>
        <NewThreadButton disabled={!props.canStartNew} onNew={props.onNew} />
      </Show>
      <ThreadMenu {...props} mode={mode()} />
      <Show when={mode() !== "tabs"}>
        <NewThreadButton disabled={!props.canStartNew} onNew={props.onNew} />
      </Show>
    </div>
  );
}

/**
 * The threads of an agent, in the chat header. An agent with only its own chat shows only a "New
 * thread" button, so its header stays almost the same. With a side thread, a menu of the side
 * threads shows, and the side threads with a tab show as tabs.
 */
export function ThreadHeader(props: ThreadHeaderProps) {
  const single = () => props.tabs.length === 0 && props.threads.length === 0 && props.archived.length === 0;
  return (
    <Show when={!single()} fallback={<NewThreadButton disabled={!props.canStartNew} onNew={props.onNew} />}>
      <ThreadRow {...props} />
    </Show>
  );
}
