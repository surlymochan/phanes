import { randomUUID } from "node:crypto";
import { sortConversationMessages } from "@openbot/contracts/conversation-order";
import {
  AGENT_PAUSE_ITEM_TYPE,
  type AgentEvent,
  type ContinueAgentPauseInput,
  type ConversationMessage,
  type ConversationPause,
} from "@openbot/contracts/ipc";
import { sourceText } from "@openbot/i18n/source";
import { Effect, Schema } from "effect";
import type { AgentStore } from "../agent-store";
import { causeHelpers } from "../effect-boundary";
import type { MailboxStore } from "../mailbox-store";
import type { ConversationRuntime } from "./conversation-runtime";
import { agentProviderAccount as accountOf, providerForAgent } from "./thread-items";
import type { TokenBudget, TokenBudgetNotice } from "./token-budget";
import type { UsageLimitKey } from "./usage-limit-gate";

/**
 * The message that restarts the work of a stopped turn. The provider session keeps the plan, the
 * files and the history, so a short prompt is enough. It is a prompt for the agent, not interface text.
 */
const RESUME_PROMPT = "Continue the work you stopped. Read your last plan, then carry on from the last finished step.";

export interface AgentPausesHooks {
  emit(event: AgentEvent): void;
  emitError(code: string, error: unknown, agentId?: string): void;
  /** The resume message is queued: show it in the chat and the queue, and start it when the agent may. */
  queued(agentId: string): void;
}

export interface AgentPausesOptions {
  store: AgentStore;
  mailbox: MailboxStore;
  conversation: ConversationRuntime;
  budget: TokenBudget;
  hooks: AgentPausesHooks;
}

/**
 * The stops that wait for the user or for the plan: the marker in the agent's chat that says why,
 * and the Continue that ends them.
 *
 * Owns the `agent-pause` markers (the record; it holds everything Continue needs, so a restart
 * loses nothing) and the agents that a spent plan stopped mid-work, which continue by themselves
 * when the plan limit ends. It never imports the agent service facade.
 *
 * A turn stop (`turn-budget`, `plan-limit`) asks for a resume: Continue queues one short message
 * that carries on in the same provider session. An hourly hold stopped no turn, so Continue only
 * lets the queue start again.
 */
export class AgentPauses {
  readonly #store: AgentStore;
  readonly #mailbox: MailboxStore;
  readonly #conversation: ConversationRuntime;
  readonly #budget: TokenBudget;
  readonly #hooks: AgentPausesHooks;
  /** Agents a spent plan stopped, with the marker that says so. */
  readonly #planStops = new Map<string, string>();

  constructor(options: AgentPausesOptions) {
    this.#store = options.store;
    this.#mailbox = options.mailbox;
    this.#conversation = options.conversation;
    this.#budget = options.budget;
    this.#hooks = options.hooks;
  }

  /** A token limit held this agent. Writes the marker and tells the renderers. */
  readonly budgetHeld = (notice: TokenBudgetNotice): Effect.Effect<void> =>
    Effect.sync(() => {
      const marker = this.#appendMessage(notice.agentId, {
        reason: notice.reason,
        usedTokens: notice.usedTokens,
        limitTokens: notice.limitTokens,
        resetsAt: null,
        resume: notice.reason === "turn-budget",
        resolved: false,
      });
      // Without its marker the user could never continue, so the agent is not held.
      if (!marker) {
        this.#budget.continueAgent(notice.agentId);
        return;
      }
      this.#hooks.emit({
        type: "agent-paused",
        agentId: notice.agentId,
        threadId: marker.threadId,
        reason: notice.reason,
        usedTokens: notice.usedTokens,
        limitTokens: notice.limitTokens,
      });
    });

  /** The window fell under the limit, or the user raised it: the hourly markers are over. */
  readonly budgetReleased = (agentId: string): Effect.Effect<void> =>
    Effect.sync(() => {
      this.#resolve(agentId, null, (pause) => pause.reason === "hour-budget");
    });

  /**
   * The plan refused a turn that had done work. The turn ends as interrupted instead of failed, and
   * the agent continues when the plan limit ends.
   */
  planStopped(agentId: string, resetsAt: number | null): void {
    // One marker for an agent whose turn is stopped again before it continued.
    if (this.#planStops.has(agentId)) return;
    const marker = this.#appendMessage(agentId, {
      reason: "plan-limit",
      usedTokens: 0,
      limitTokens: 0,
      resetsAt,
      resume: true,
      resolved: false,
    });
    if (marker) this.#planStops.set(agentId, marker.id);
  }

  /** The plan limit of this provider account ended: each agent it stopped carries on. */
  readonly planLimitEnded = (limit: UsageLimitKey): Effect.Effect<void> =>
    Effect.gen({ self: this }, function* () {
      for (const agent of this.#store.list()) {
        if (!this.#planStops.has(agent.id)) continue;
        if (providerForAgent(agent) !== limit.provider || accountOf(agent) !== limit.accountId) continue;
        yield* this.continueAgent({ agentId: agent.id, messageId: this.#planStops.get(agent.id) ?? "" }).pipe(
          Effect.catch((failure) =>
            Effect.sync(() => this.#hooks.emitError("plan_limit_resume_failed", failure.cause, agent.id)),
          ),
        );
      }
    });

  /**
   * The user's Continue, or the end of a plan limit. Ends the holds of the agent, marks its markers
   * done, and queues the resume message when a turn stopped. Safe to repeat: a marker already done
   * changes nothing and queues nothing.
   */
  readonly continueAgent = Effect.fn("AgentPauses.continueAgent")(function* (
    this: AgentPauses,
    input: ContinueAgentPauseInput,
  ) {
    yield* this.#store.existing(input.agentId).pipe(toAgentPauseFailed);
    const resolved = yield* agentPauseStep(() => this.#resolve(input.agentId, input.messageId, () => true));
    this.#planStops.delete(input.agentId);
    // The end of a plan limit says nothing about the token budget, so only a budget marker starts a new window.
    if (resolved.some((marker) => marker.pause.reason !== "plan-limit")) this.#budget.continueAgent(input.agentId);
    const resume = resolved.find((marker) => marker.pause.resume);
    if (!resume) return;
    yield* this.#mailbox
      .enqueue({
        sender: { kind: "user" },
        recipientAgentIds: [input.agentId],
        text: RESUME_PROMPT,
        replyToMessageId: null,
        draftIds: [],
        // One resume for each marker, however often Continue runs.
        idempotencyKey: `pause-resume:${resume.id}`,
      })
      .pipe(toAgentPauseFailed);
    this.#hooks.queued(input.agentId);
  }).bind(this);

  forgetAgent(agentId: string): void {
    this.#planStops.delete(agentId);
  }

  #appendMessage(agentId: string, pause: ConversationPause): { id: string; threadId: string } | null {
    const id = `agent-pause-${randomUUID()}`;
    try {
      return this.#conversation.withConversationTransaction(agentId, ({ threadId, snapshot }) => {
        const message: ConversationMessage = {
          id,
          author: "system",
          source: "system",
          text: pauseText(pause),
          createdAt: new Date().toISOString(),
          status: "completed",
          itemType: AGENT_PAUSE_ITEM_TYPE,
          pause,
        };
        snapshot.messages.push(message);
        sortConversationMessages(snapshot.messages);
        snapshot.revision = this.#store.database.appendConversationMessage({
          agentId,
          threadId,
          activeTurnId: snapshot.activeTurnId,
          message,
          eventType: "agent.paused",
          detail: { reason: pause.reason },
        });
        return { result: { id, threadId }, snapshot };
      });
    } catch (error) {
      // The hold is in place; only its note is lost.
      this.#hooks.emitError("agent_pause_marker_failed", error, agentId);
      return null;
    }
  }

  /**
   * Marks the unfinished markers of the agent that `matches` done, and the marker `messageId`
   * names, which may have left the cached snapshot. Returns the markers it changed.
   */
  #resolve(
    agentId: string,
    messageId: string | null,
    matches: (pause: ConversationPause) => boolean,
  ): Array<{ id: string; pause: ConversationPause }> {
    return this.#conversation.withConversationTransaction(agentId, ({ threadId, snapshot }) => {
      if (messageId && !snapshot.messages.some((message) => message.id === messageId)) {
        const [stored] = this.#store.database.readConversationMessages(agentId, threadId, [messageId]);
        if (stored) snapshot.messages.push(stored);
      }
      const changed: ConversationMessage[] = [];
      for (const message of snapshot.messages) {
        const pause = message.pause;
        if (message.itemType !== AGENT_PAUSE_ITEM_TYPE || !pause || pause.resolved) continue;
        if (message.id !== messageId && !matches(pause)) continue;
        message.pause = { ...pause, resolved: true };
        changed.push(message);
      }
      if (changed.length === 0) return { result: [], snapshot };
      sortConversationMessages(snapshot.messages);
      snapshot.revision = this.#store.database.persistConversationChanges({
        agentId,
        threadId,
        activeTurnId: snapshot.activeTurnId,
        changedMessages: changed,
        eventType: "agent.pause-resolved",
      });
      return {
        result: changed.flatMap((message) => (message.pause ? [{ id: message.id, pause: message.pause }] : [])),
        snapshot,
      };
    });
  }
}

/** A count as a reader says it: 15M, 5.2M, 800K. */
function tokenCount(value: number): string {
  if (value >= 1_000_000) return `${Number((value / 1_000_000).toFixed(1))}M`;
  if (value >= 1_000) return `${Number((value / 1_000).toFixed(1))}K`;
  return String(value);
}

/** The readable sentence of a marker, for a client that does not know its item type. */
function pauseText(pause: ConversationPause): string {
  if (pause.reason === "plan-limit") return sourceText("status.agent.pausedPlanLimit");
  return sourceText(
    pause.reason === "hour-budget" ? "status.agent.pausedHourBudget" : "status.agent.pausedTurnBudget",
    {
      used: tokenCount(pause.usedTokens),
      limit: tokenCount(pause.limitTokens),
    },
  );
}

class AgentPauseFailed extends Schema.TaggedError<AgentPauseFailed>()("AgentPauseFailed", {
  cause: Schema.Defect(),
}) {}

const { sync: agentPauseStep, rewrap: toAgentPauseFailed } = causeHelpers(AgentPauseFailed);
