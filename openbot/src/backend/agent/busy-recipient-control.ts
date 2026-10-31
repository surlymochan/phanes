import { sourceText } from "@openbot/i18n/source";
import type { Logger } from "@openbot/logging";
import { Effect } from "effect";
import type { MailboxStore } from "../mailbox-store";
import type { BusyRecipientControl, BusyRecipientOutcome } from "./openbot-tool-router";

/** One sender does not stop the same teammate again within this time. */
const INTERRUPT_COOLDOWN_MS = 60_000;
/** A teammate is stopped by others at most `INTERRUPT_LIMIT` times in this window. */
const INTERRUPT_WINDOW_MS = 5 * 60_000;
const INTERRUPT_LIMIT = 3;

export interface BusyRecipientControlHooks {
  /** The running turn of the agent's own chat, or null when it is idle. */
  activeTurnId(agentId: string): string | null;
  steer(agentId: string, deliveryId: string, turnId: string): Effect.Effect<void, unknown>;
  runNow(agentId: string, deliveryId: string): Effect.Effect<void, unknown>;
  agentName(agentId: string): string;
  /** Masks the MCP and custom-endpoint secrets that a provider error can carry. */
  redact(text: string): string;
}

/**
 * `send_message` with `busy` set: one agent steers a teammate's running work, or stops it for a new
 * message. Any agent may do this for any teammate. Each use is logged with the sender, recipient,
 * mode and result, and interrupts are limited so agents cannot stop each other in a loop:
 *
 * - a cooldown for each sender and recipient pair;
 * - a limit on how often others stop one recipient in a window;
 * - no interrupt from a turn that itself started because a teammate interrupted the sender.
 *
 * A refused or failed request leaves the message queued, and the sender reads why.
 */
export class BusyRecipientControls {
  readonly #mailbox: MailboxStore;
  readonly #hooks: BusyRecipientControlHooks;
  readonly #logger: Logger;
  readonly #now: () => number;
  /** `sender\0recipient` -> the time of the last interrupt. */
  readonly #lastInterrupt = new Map<string, number>();
  /** recipient -> the times others interrupted it, newest last. Each entry is its own object, so a rollback removes exactly its own. */
  readonly #interruptsOf = new Map<string, Array<{ at: number }>>();
  /** The deliveries that started a turn by an interrupt. A turn that runs one cannot interrupt. */
  readonly #interruptDeliveries = new Set<string>();

  constructor(options: {
    mailbox: MailboxStore;
    hooks: BusyRecipientControlHooks;
    logger: Logger;
    now?: () => number;
  }) {
    this.#mailbox = options.mailbox;
    this.#hooks = options.hooks;
    this.#logger = options.logger;
    this.#now = options.now ?? Date.now;
  }

  readonly control = Effect.fn("BusyRecipientControls.control")(function* (
    this: BusyRecipientControls,
    input: BusyRecipientControl,
  ) {
    const outcome = yield* input.mode === "steer" ? this.#steer(input) : this.#interrupt(input);
    // The audit record: who changed whose work, how, and what happened.
    this.#logger.info("Agent busy message control.", {
      sender: input.senderAgentId,
      senderName: this.#hooks.agentName(input.senderAgentId),
      recipient: input.recipientAgentId,
      recipientName: this.#hooks.agentName(input.recipientAgentId),
      deliveryId: input.deliveryId,
      mode: input.mode,
      result: outcome.result,
      ...(outcome.reason ? { reason: outcome.reason } : {}),
    });
    return outcome;
  });

  #steer(input: BusyRecipientControl): Effect.Effect<BusyRecipientOutcome> {
    const turnId = this.#hooks.activeTurnId(input.recipientAgentId);
    if (!turnId) return Effect.succeed({ agentId: input.recipientAgentId, result: "queued" });
    return this.#hooks.steer(input.recipientAgentId, input.deliveryId, turnId).pipe(
      Effect.as<BusyRecipientOutcome>({ agentId: input.recipientAgentId, result: "steered" }),
      Effect.catch((cause) => Effect.succeed(queued(input.recipientAgentId, this.#hooks.redact(failureText(cause))))),
    );
  }

  /**
   * The check and the reservation run in one synchronous step, before the stop is sent. Requests
   * from several fibers therefore see each other's reservation. A stop that fails gives its
   * reservation back, so a refused stop does not use up the cooldown or the limit.
   */
  #interrupt(input: BusyRecipientControl): Effect.Effect<BusyRecipientOutcome> {
    return Effect.suspend(() => {
      const recipient = input.recipientAgentId;
      const turnId = this.#hooks.activeTurnId(recipient);
      if (!turnId) return Effect.succeed<BusyRecipientOutcome>({ agentId: recipient, result: "queued" });
      const refusal = this.#refusal(input);
      if (refusal) return Effect.succeed(queued(recipient, refusal));
      const reservation = this.#reserve(input);
      return this.#hooks.runNow(recipient, input.deliveryId).pipe(
        Effect.as<BusyRecipientOutcome>({ agentId: recipient, result: "interrupted" }),
        Effect.catch((cause) =>
          Effect.sync(() => {
            this.#release(reservation);
            return queued(recipient, this.#hooks.redact(failureText(cause)));
          }),
        ),
      );
    });
  }

  #reserve(input: BusyRecipientControl): Reservation {
    const now = this.#now();
    const pair = pairKey(input.senderAgentId, input.recipientAgentId);
    const reservation: Reservation = {
      pair,
      recipient: input.recipientAgentId,
      previousLast: this.#lastInterrupt.get(pair),
      entry: { at: now },
      deliveryId: input.deliveryId,
    };
    this.#lastInterrupt.set(pair, now);
    this.#interruptsOf.set(input.recipientAgentId, [
      ...this.#recentInterrupts(input.recipientAgentId),
      reservation.entry,
    ]);
    // Set before the stop: the turn the stop starts can run this message and start its own tool call.
    this.#interruptDeliveries.add(input.deliveryId);
    return reservation;
  }

  #release(reservation: Reservation): void {
    if (reservation.previousLast === undefined) this.#lastInterrupt.delete(reservation.pair);
    else this.#lastInterrupt.set(reservation.pair, reservation.previousLast);
    this.#interruptsOf.set(
      reservation.recipient,
      (this.#interruptsOf.get(reservation.recipient) ?? []).filter((entry) => entry !== reservation.entry),
    );
    this.#interruptDeliveries.delete(reservation.deliveryId);
  }

  #refusal(input: BusyRecipientControl): string | null {
    const now = this.#now();
    const last = this.#lastInterrupt.get(pairKey(input.senderAgentId, input.recipientAgentId));
    if (last !== undefined && now - last < INTERRUPT_COOLDOWN_MS)
      return sourceText("error.backend.agentControlCooldown");
    if (this.#recentInterrupts(input.recipientAgentId).length >= INTERRUPT_LIMIT)
      return sourceText("error.backend.agentControlLimit");
    const started = this.#mailbox.findDeliveriesByTurn(input.senderAgentId, input.senderTurnId);
    if (started.some(({ delivery }) => this.#interruptDeliveries.has(delivery.id)))
      return sourceText("error.backend.agentControlChain");
    return null;
  }

  #recentInterrupts(agentId: string): Array<{ at: number }> {
    const since = this.#now() - INTERRUPT_WINDOW_MS;
    return (this.#interruptsOf.get(agentId) ?? []).filter((entry) => entry.at > since);
  }
}

/** What one interrupt reserved, so a failed stop can give it back. */
interface Reservation {
  pair: string;
  recipient: string;
  previousLast: number | undefined;
  entry: { at: number };
  deliveryId: string;
}

function pairKey(senderAgentId: string, recipientAgentId: string): string {
  return `${senderAgentId}\0${recipientAgentId}`;
}

function queued(agentId: string, reason: string): BusyRecipientOutcome {
  return { agentId, result: "queued", reason };
}

function failureText(cause: unknown): string {
  const inner = typeof cause === "object" && cause !== null && "cause" in cause ? cause.cause : cause;
  return inner instanceof Error ? inner.message : String(inner);
}
