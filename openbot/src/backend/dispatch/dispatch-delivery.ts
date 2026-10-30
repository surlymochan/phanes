import { createHash } from "node:crypto";
import { INPUT_LIMITS } from "@openbot/contracts/input-limits";
import type { ConversationMessage } from "@openbot/contracts/ipc";
import { sourceText } from "@openbot/i18n/source";
import { redactText } from "@openbot/logging";
import { Effect } from "effect";
import type { ConversationRuntime } from "../agent/conversation-runtime";
import type { DrainScheduler } from "../agent/drain-scheduler";
import type { MailboxSync } from "../agent/mailbox-sync";
import type { AgentStore } from "../agent-store";
import type { MailboxStore } from "../mailbox-store";
import { DispatchFailed } from "./dispatch-process";
import type { DispatchRecord } from "./dispatch-service";

export interface DispatchDeliveryOptions {
  store: AgentStore;
  mailbox: MailboxStore;
  conversation: ConversationRuntime;
  mailboxSync: MailboxSync;
  drain: DrainScheduler;
  redact(text: string): string;
}

// JSON can expand control characters to six characters each. Leave room for keys and failing-item separators.
const DISPATCH_RESULT_TEXT_BUDGET = 12_000;

/** Redacts the exact mailbox payload at the send boundary. */
export function dispatchResultMailboxText(record: DispatchRecord, redact: (text: string) => string): string {
  let remaining = DISPATCH_RESULT_TEXT_BUDGET;
  const safeText = (text: string) => {
    const safe = redact(redactText(text));
    const length = Math.min(safe.length, remaining);
    remaining -= length;
    if (length === safe.length) return safe;
    return length === 0 ? "" : `${safe.slice(0, length - 1)}…`;
  };
  const runId = safeText(record.runId);
  const host = safeText(record.host);
  const text = JSON.stringify({
    runId,
    host,
    status: record.status,
    branch: record.branch === null ? null : safeText(record.branch),
    commit: record.commit === null ? null : safeText(record.commit),
    ...(record.summary === undefined ? {} : { summary: safeText(record.summary) }),
    ...(record.verify === undefined
      ? {}
      : {
          verify:
            record.verify === null
              ? null
              : {
                  ...record.verify,
                  ...(record.verify.cmd === undefined ? {} : { cmd: safeText(record.verify.cmd) }),
                  ...(record.verify.tail === undefined ? {} : { tail: safeText(record.verify.tail) }),
                  ...(record.verify.failing === undefined
                    ? {}
                    : { failing: record.verify.failing.slice(0, 128).map(safeText) }),
                },
        }),
  });
  if (text.length <= INPUT_LIMITS.messageText) return text;
  return JSON.stringify({ runId, host, status: record.status });
}

/** Owns dispatch status messages and idempotent result delivery into the original chat. */
export class DispatchDelivery {
  readonly #options: DispatchDeliveryOptions;
  constructor(options: DispatchDeliveryOptions) {
    this.#options = options;
  }

  readonly publish = Effect.fn("DispatchDelivery.publish")(function* (
    this: DispatchDelivery,
    record: DispatchRecord,
    terminal: boolean,
  ) {
    const { store, mailbox, conversation, mailboxSync, drain } = this.#options;
    const agent = store.list().find((entry) => entry.id === record.agentId);
    // Removed agents must not be recreated by a late result.
    if (!agent) return;
    const key = createHash("sha256").update(`${record.host}:${record.runId}`).digest("hex").slice(0, 32);
    yield* Effect.try({
      try: () => {
        conversation.registerExecutionThread(record.agentId, record.threadId);
        const snapshot = structuredClone(conversation.ensureSnapshot(record.agentId, record.threadId));
        const id = `dispatch-${key}-${terminal ? "result" : "started"}`;
        if (snapshot.messages.some((message) => message.id === id)) return;
        const message: ConversationMessage = {
          id,
          author: "system",
          source: "system",
          status: "completed",
          itemType: "dispatch",
          createdAt: new Date().toISOString(),
          text: terminal
            ? sourceText("status.agent.dispatchFinished", {
                title: record.title,
                host: record.host,
                status: record.status,
              })
            : sourceText("status.agent.dispatchStarted", {
                title: record.title,
                host: record.host,
                runId: record.runId,
              }),
        };
        snapshot.messages.push(message);
        snapshot.revision = store.database.persistConversationChanges({
          agentId: record.agentId,
          threadId: record.threadId,
          activeTurnId: snapshot.activeTurnId,
          changedMessages: [message],
          eventType: "dispatch.updated",
          detail: { runId: record.runId, host: record.host, status: record.status },
        });
        conversation.setSnapshot(record.agentId, snapshot);
        conversation.publishConversation(snapshot);
      },
      catch: (cause) => new DispatchFailed({ cause }),
    });
    if (!terminal) return;
    yield* mailbox
      .enqueue({
        sender: { kind: "user" },
        dispatch: { runId: record.runId, host: record.host, threadId: record.threadId },
        recipientAgentIds: [record.agentId],
        text: dispatchResultMailboxText(record, this.#options.redact),
        draftIds: [],
        replyToMessageId: null,
        idempotencyKey: `dispatch:${key}`,
      })
      .pipe(Effect.mapError((failure) => new DispatchFailed({ cause: failure.cause })));
    mailboxSync.emitQueue(record.agentId);
    drain.scheduleDrain(record.agentId);
  }, Effect.uninterruptible);
}
