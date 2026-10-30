// @vitest-environment node
import type { ConversationSnapshot, QueueDelivery } from "@openbot/contracts/ipc";
import { describe, expect, it } from "vitest";
import { deliveryPromptInput, teammatePrompts } from "./delivery-content";

// Failure mode: a teammate escalation sent with expectsReply false told an idle recipient only to
// "continue with what you were doing", so it ended each turn with no answer and no action.
function noAnswerPrompt(): string {
  const delivery: QueueDelivery = {
    id: "delivery-1",
    messageId: "message-1",
    recipientAgentId: "agent-coordinator",
    sender: { kind: "agent", agentId: "agent-supervisor" },
    text: "Correct the tracker and set a deadline before the next inspection.",
    attachments: [],
    replyToMessageId: null,
    expectsReply: false,
    status: "running",
    position: null,
    turnId: null,
    error: null,
    createdAt: "2026-10-20T21:18:53.093Z",
  };
  const snapshot: ConversationSnapshot = {
    agentId: "agent-coordinator",
    threadId: null,
    activeTurnId: null,
    revision: 0,
    messages: [],
  };
  const [item] = deliveryPromptInput(
    { delivery, managedAttachments: [] },
    {
      agentNames: new Map([["agent-supervisor", "Supervisor"]]),
      snapshot,
      routineRun: null,
    },
  );
  if (item?.type !== "text") throw new Error("The prompt has no text.");
  return item.text;
}

describe("a teammate message that wants no answer", () => {
  it("tells the recipient to do an action that the message asks for", () => {
    const text = noAnswerPrompt();
    expect(text).toContain("When it does and the action is yours, do it in this turn.");
    expect(text).toContain("When you cannot do it, tell the sender why");
    expect(text).not.toContain("Do not send a reply, an acknowledgement, or a result for it.");
  });

  it("still reads back as a message that wants no answer", () => {
    expect(teammatePrompts(noAnswerPrompt())).toEqual([
      {
        senderAgentId: "agent-supervisor",
        messageId: "message-1",
        replyToMessageId: null,
        expectsReply: false,
        text: "Correct the tracker and set a deadline before the next inspection.",
      },
    ]);
  });
});
