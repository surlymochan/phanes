import { createHash } from "node:crypto";
import { Effect } from "effect";
import type { MailboxStore } from "../mailbox-store";

/** An A2A request after the transport has decoded and authenticated it. */
export interface NibiruInboundRequest {
  /** The authenticated caller. A key is scoped to it, so one caller's key never reads another's receipt. */
  principalId: string;
  idempotencyKey: string;
  taskId: string;
  recipientAgentId: string;
  text: string;
}

export class UnknownA2ARecipientError extends Error {
  readonly code = "unknown_recipient";
  constructor() {
    super("Unknown A2A recipient.");
  }
}

export function createNibiruInbound(options: {
  mailbox: Pick<MailboxStore, "enqueue">;
  hasAgent: (agentId: string) => boolean;
}) {
  return {
    accept(request: NibiruInboundRequest) {
      return Effect.gen(function* () {
        if (!request.idempotencyKey || !request.taskId || !request.principalId) {
          return yield* Effect.fail(new Error("A2A key and task ID are required."));
        }
        if (!options.hasAgent(request.recipientAgentId)) return yield* Effect.fail(new UnknownA2ARecipientError());
        // The key is scoped to (principal, recipient): the same external key sent by another caller or
        // to another agent is a new request, and cannot return a receipt that names someone else's delivery.
        const key = createHash("sha256")
          .update(JSON.stringify([request.principalId, request.recipientAgentId, request.idempotencyKey]))
          .digest("hex");
        return yield* options.mailbox.enqueue({
          a2a: { taskId: request.taskId },
          sender: { kind: "user" },
          recipientAgentIds: [request.recipientAgentId],
          text: request.text,
          idempotencyKey: `nibiru-a2a:${key}`,
        });
      });
    },
  };
}
