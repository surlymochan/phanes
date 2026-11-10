import { createHash } from "node:crypto";
import { Effect } from "effect";
import type { MailboxStore } from "../mailbox-store";

/** An A2A request after the transport has decoded and authenticated it. */
export interface NibiruInboundRequest {
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

export function createNibiruInbound(options: { mailbox: MailboxStore; hasAgent: (agentId: string) => boolean }) {
  return {
    accept(request: NibiruInboundRequest) {
      return Effect.gen(function* () {
        if (!request.idempotencyKey || !request.taskId) {
          return yield* Effect.fail(new Error("A2A key and task ID are required."));
        }
        if (!options.hasAgent(request.recipientAgentId)) return yield* Effect.fail(new UnknownA2ARecipientError());
        const key = createHash("sha256").update(request.idempotencyKey).digest("hex");
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
