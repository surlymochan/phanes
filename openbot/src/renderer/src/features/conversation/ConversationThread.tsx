import type { AgentMessage } from "@openbot/ui/data";
import { AgentThreadView } from "@openbot/ui/features/conversation/threads/AgentThreadView";
import { createProjection, Show } from "solid-js";
import { toAgentMessages } from "../../app-message-projection";
import { useConversationViewScope } from "./conversation-scope";

/** The open side thread of the agent, in the place of the agent's own chat and its message box. */
export function ConversationThread() {
  const { props, openExternalMessageUrl, previewAttachment, attachmentAction } = useConversationViewScope();
  /** Reconciled by id, so a stream chunk redraws only the message that changed. */
  const messages = createProjection<AgentMessage[]>(
    () => {
      const conversation = props.threads?.conversation;
      return conversation ? toAgentMessages(conversation.messages, props.agent?.id) : [];
    },
    [],
    { key: "id" },
  );
  const view = () => {
    const agent = props.agent;
    const threads = props.threads;
    return agent && threads ? { agent, threads } : undefined;
  };
  return (
    <Show when={view()}>
      {(current) => (
        <AgentThreadView
          task={current().threads.open ?? null}
          limit={current().threads.limit}
          agent={current().agent}
          agents={props.agents}
          messages={messages}
          onSend={(text) => current().threads.onSend(text)}
          onStop={() => current().threads.onStop()}
          onOpenLink={(url) => void openExternalMessageUrl(url)}
          onPreview={(attachment) => void previewAttachment(attachment)}
          onAttachmentAction={attachmentAction}
          approval={current().threads.approval}
          onRespondApproval={(decision) => current().threads.onRespondApproval(decision)}
        />
      )}
    </Show>
  );
}
