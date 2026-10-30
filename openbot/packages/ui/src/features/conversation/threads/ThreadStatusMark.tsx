import type { AppTextKey } from "@openbot/i18n";
import { Clock3, X } from "@openbot/ui";
import { TypingDots } from "@openbot/ui/components/TypingDots";
import { SidebarWaitIcon } from "@openbot/ui/features/sidebar/SidebarAgentIndicator";
import type { SidebarWaitReason } from "@openbot/ui/features/sidebar/sidebar-types";
import { type TextValue, useText } from "@openbot/ui/text";
import { Show } from "solid-js";
import type { ThreadStatus } from "./thread-types";

const WAIT_TEXT = {
  question: "sidebar.state.waitingQuestion",
  approval: "sidebar.state.waitingApproval",
  takeover: "sidebar.state.waitingTakeover",
} as const satisfies Record<SidebarWaitReason, AppTextKey>;

/** What a thread does now, for a screen reader. An idle thread has no label. */
function threadStatusLabel(status: ThreadStatus, t: TextValue["t"]): string {
  switch (status.kind) {
    case "working":
      return t("sidebar.state.working");
    case "queued":
      return t("conversation.threads.state.queued");
    case "waiting":
      return t(WAIT_TEXT[status.reason]);
    case "failed":
      return t("conversation.threads.state.failed");
    default:
      return "";
  }
}

/** A thread that needs the user wears the tint of its badge. */
export function threadAttention(status: ThreadStatus): "waiting" | "failed" | undefined {
  if (status.kind === "waiting" || status.kind === "failed") return status.kind;
  return undefined;
}

function waitReason(status: ThreadStatus): SidebarWaitReason | undefined {
  return status.kind === "waiting" ? status.reason : undefined;
}

/**
 * The icon of a status, with the colors of the sidebar badges. When the state ends, the mark keeps its
 * last icon and narrows to nothing, so the tab does not jump to its new width.
 */
function StatusIcon(props: { status: ThreadStatus }) {
  let last: ThreadStatus = { kind: "idle" };
  const shown = () => {
    if (props.status.kind !== "idle") last = props.status;
    return last;
  };
  const open = () => (props.status.kind === "idle" ? undefined : "");
  return (
    <span class="agent-threads-tab-mark-slot" data-open={open()} aria-hidden="true">
      <span class="agent-threads-tab-mark" data-kind={shown().kind}>
        <Show when={shown().kind === "working"}>
          <span class="agent-threads-state agent-threads-state-working">
            <TypingDots class="agent-row-thinking-dots" />
          </span>
        </Show>
        <Show when={waitReason(shown())}>
          {(reason) => (
            <span class="agent-threads-state agent-threads-state-icon">
              <SidebarWaitIcon reason={reason()} />
            </span>
          )}
        </Show>
        <Show when={shown().kind === "queued"}>
          <span class="agent-threads-state agent-threads-state-icon">
            <Clock3 />
          </span>
        </Show>
        <Show when={shown().kind === "failed"}>
          <span class="agent-threads-state agent-threads-state-icon">
            <X />
          </span>
        </Show>
      </span>
    </span>
  );
}

/** The state of a thread, as on a sidebar row, with its label for a screen reader. */
export function ThreadStatusMark(props: { status: ThreadStatus }) {
  const { t } = useText();
  return (
    <>
      <StatusIcon status={props.status} />
      <Show when={threadStatusLabel(props.status, t)}>{(label) => <span class="sr-only">{label()}</span>}</Show>
    </>
  );
}
