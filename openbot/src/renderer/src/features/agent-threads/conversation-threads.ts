import type { AgentApproval } from "@openbot/contracts/ipc";
import { isAgentTaskActive, taskApproval } from "@openbot/team-client/agent-tasks";
import { activeThreadId, threadLists } from "@openbot/ui/features/conversation/threads/thread-items";
import { DRAFT_THREAD_ID, MAIN_THREAD_ID } from "@openbot/ui/features/conversation/threads/thread-types";
import type { AgentTasks } from "../agent-tasks/agent-tasks-state";
import type { ConversationThreads } from "../conversation/conversation-types";
import type { AgentThreadTabsState } from "./agent-thread-tabs";

export interface ConversationThreadsInput {
  agent: { id: string; updatedAt: string };
  tasks: AgentTasks;
  tabs: AgentThreadTabsState;
  /** The approvals that wait on the server. A side thread's turn waits for the one of its own thread. */
  approvals: readonly AgentApproval[];
  newThreadTitle: string;
  onRespondApproval: (approval: AgentApproval, decision: "accept" | "decline") => Promise<boolean>;
}

/** The threads of one agent, for the conversation. Read it in a memo: it reads the thread state. */
export function conversationThreads(input: ConversationThreadsInput): ConversationThreads {
  const { agent, tasks, tabs, approvals } = input;
  const agentId = agent.id;
  const snapshot = tasks.snapshot(agentId);
  const list = snapshot?.tasks ?? [];
  const lists = threadLists({
    mainUpdatedAt: agent.updatedAt,
    tasks: list,
    open: tabs.openIds(agentId),
    draft: tabs.hasDraft(agentId),
    draftTitle: input.newThreadTitle,
    hidden: tabs.hidden(),
    waitsForApproval: (task) => taskApproval(task, approvals) !== undefined,
  });
  const activeId = activeThreadId(lists.tabs, tabs.activeId(agentId));
  const open =
    activeId === MAIN_THREAD_ID
      ? undefined
      : activeId === DRAFT_THREAD_ID
        ? null
        : list.find((task) => task.id === activeId);
  const approval = open ? taskApproval(open, approvals) : undefined;
  const editable = tasks.editable();
  return {
    header: {
      ...lists,
      activeId,
      canStartNew: true,
      onOpen: (id) => tabs.open(agentId, id),
      onNew: () => tabs.startNew(agentId),
      onClose: (id) => tabs.close(agentId, id),
      onCloseOthers: (id) => tabs.closeOthers(agentId, id),
      onCloseToTheRight: (id) => tabs.closeToTheRight(agentId, id),
      onMove: (id, index) => tabs.move(agentId, id, index),
      onArchive: (id) => void tabs.archive(agentId, id),
      onRestore: (id) => void tabs.restore(agentId, id),
      onRename: editable ? (id, title) => void tabs.rename(agentId, id, title) : undefined,
      onDelete: editable ? (id) => void tabs.remove(agentId, id) : undefined,
    },
    open,
    limit: snapshot?.limit ?? 1,
    running: list.filter(isAgentTaskActive).length,
    conversation: open ? tasks.conversation(open.id) : undefined,
    approval,
    onSend: (text) => (open ? tabs.send(agentId, open.id, text) : tabs.sendDraft(agentId, text)),
    onStop: () => {
      if (open) tasks.stopTask(agentId, open.id);
    },
    onRespondApproval: (decision) => (approval ? input.onRespondApproval(approval, decision) : Promise.resolve(false)),
    // Only this computer moves a queued message to a new thread.
    onRunQueuedMessage: tasks.local() ? (deliveryId) => void tabs.runQueuedMessage(agentId, deliveryId) : undefined,
  };
}
