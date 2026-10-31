import { Button, ChevronRight } from "@openbot/ui";
import { createSignal, createUniqueId, For, Show, untrack } from "solid-js";
import type { AgentMessage } from "../../data";
import { useText } from "../../text";

/** The provider's process notes for one turn. Text stays collapsed until the reader opens it. */
export function ResponseProcess(props: { message: AgentMessage; running: boolean; defaultOpen?: boolean }) {
  const { t } = useText();
  const panelId = createUniqueId();
  const [open, setOpen] = createSignal(untrack(() => props.defaultOpen ?? false));
  const label = () => {
    if (props.running) return t("chat.process.running");
    if (props.message.status === "failed") return t("chat.process.failed");
    if (props.message.status === "interrupted") return t("chat.process.stopped");
    return t("chat.process.completed");
  };
  const items = () =>
    (props.message.items ?? [props.message.body])
      .map((text, index) => ({ text, id: props.message.itemIds?.[index] }))
      .filter((item) => item.text.trim());
  return (
    <section
      class="response-process"
      data-chat-search-message={props.message.id}
      data-chat-search-group={props.message.itemIds?.join(" ")}
      data-running={props.running ? "" : undefined}
    >
      <Button
        type="button"
        variant="ghost"
        class="response-process-toggle"
        aria-expanded={open() ? "true" : "false"}
        aria-controls={panelId}
        data-cuelume-tap={open() ? "close" : "open"}
        onClick={() => setOpen((value) => !value)}
      >
        <ChevronRight class="response-process-chevron" aria-hidden="true" />
        <span>{label()}</span>
      </Button>
      <section id={panelId} class="response-process-notes" aria-label={label()} hidden={!open()}>
        <Show
          when={items().length > 0}
          fallback={<p>{t(props.running ? "chat.process.waiting" : "chat.process.unavailable")}</p>}
        >
          <For each={items()} keyed={false}>
            {(item) => <p data-chat-search-item={item().id}>{item().text}</p>}
          </For>
        </Show>
      </section>
    </section>
  );
}
