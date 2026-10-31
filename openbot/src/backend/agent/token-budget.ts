import type { AgentPauseReason, TokenBudgetPreference } from "@openbot/contracts/ipc";
import { Effect, Exit, Scope } from "effect";

/** The window of the hourly limit. */
const TOKEN_BUDGET_WINDOW_MS = 60 * 60_000;

/** The longest delay `setTimeout` keeps. A longer wait is armed again when it fires. */
const MAX_TIMER_MS = 2_147_483_647;

/** What an agent was held for, as the chat marker and the event say it. */
export interface TokenBudgetNotice {
  agentId: string;
  reason: Extract<AgentPauseReason, "hour-budget" | "turn-budget">;
  usedTokens: number;
  limitTokens: number;
  /** The turn that stopped, for `turn-budget`. */
  turnId: string | null;
}

export interface TokenBudgetHooks {
  limits(): TokenBudgetPreference;
  /** The tokens an agent used since this time, oldest first, as the database kept them. Read once per agent. */
  readSince(agentId: string, sinceMs: number): ReadonlyArray<{ at: number; tokens: number }>;
  /** An agent was held. Writes the marker and tells the renderers. Must not wait for the agent's queue. */
  held(notice: TokenBudgetNotice): Effect.Effect<void>;
  /** The window of an hourly hold fell under the limit, or the user raised the limit. Resolves its marker. */
  released(agentId: string): Effect.Effect<void>;
  /** Stops a turn after its current step, as the user's Stop does. */
  interrupt(agentId: string, turnId: string): Effect.Effect<void>;
  scheduleDrain(agentId: string): void;
}

interface Hold {
  reason: TokenBudgetNotice["reason"];
  timer: ReturnType<typeof setTimeout> | null;
}

interface AgentLedger {
  /** The samples came from the database, not from the reports of this run only. */
  seeded: boolean;
  samples: Array<{ at: number; tokens: number }>;
  /** Reports before this time do not count: the user continued then. */
  since: number;
  hold: Hold | null;
}

interface TurnLedger {
  agentId: string;
  tokens: number;
  /** The turn went past its limit and stops at the end of its next finished step. */
  armed: boolean;
  stopped: boolean;
}

/**
 * The token budget of each agent, and the holds it sets.
 *
 * Owns a ledger of the processed tokens each agent used in the last hour, and a count for each
 * running turn. Two limits apply, and 0 turns either off:
 *
 * - The hourly limit holds an agent: `mayDrain` is false, so no new turn starts. The turn that
 *   runs goes on. The hold ends when the user continues, when the window falls under the limit,
 *   or when the user raises the limit.
 * - The turn limit arms the turn. At the end of the turn's next finished step (`boundary`), the turn
 *   stops as the user's Stop does, and the agent is held in the same way until the user continues.
 *   The conversation, the plan and the provider session stay, so the turn's work is not lost.
 *
 * Continue ends a hold and starts a new window for the agent: the tokens it used before do not count
 * again. The ledger is kept in memory and read from the database once for each agent after a start.
 * A hold is not stored; the chat marker is the record, and the first report after a restart sets the
 * hold again when the window is still over the limit.
 *
 * A provider that reports usage once at the end of a turn (Claude and the ACP providers) is counted
 * then, so the turn limit cannot stop such a turn in the middle: its tokens count for the hour.
 */
export class TokenBudget {
  readonly #hooks: TokenBudgetHooks;
  readonly #agents = new Map<string, AgentLedger>();
  readonly #turns = new Map<string, TurnLedger>();
  /** Owns the timer runs, so `dispose` stops them. */
  #scope = Scope.makeUnsafe();

  constructor(hooks: TokenBudgetHooks) {
    this.#hooks = hooks;
  }

  /** False while an hourly or turn hold keeps this agent from starting a turn. */
  mayDrain(agentId: string): boolean {
    return !this.#agents.get(agentId)?.hold;
  }

  /**
   * A usage report added `tokens` to this agent. Sets the hourly hold when the window now reaches the
   * limit, and arms the turn when it reaches the turn limit.
   */
  readonly spent = Effect.fn("TokenBudget.spent")(function* (
    this: TokenBudget,
    agentId: string,
    turnId: string | null,
    tokens: number,
  ) {
    if (tokens <= 0) return;
    const now = Date.now();
    const known = this.#agents.has(agentId);
    const ledger = this.#ledger(agentId, now);
    // The database stored this report before it came here, so a ledger read from it holds the report.
    if (known || !ledger.seeded) ledger.samples.push({ at: now, tokens });
    if (turnId !== null) {
      const turn = this.#turns.get(turnId) ?? { agentId, tokens: 0, armed: false, stopped: false };
      turn.tokens += tokens;
      this.#turns.set(turnId, turn);
      const { perTurn } = this.#hooks.limits();
      if (perTurn > 0 && turn.tokens >= perTurn) turn.armed = true;
    }
    const { perHour } = this.#hooks.limits();
    const used = this.#window(agentId, now);
    if (perHour <= 0 || used < perHour || ledger.hold) return;
    ledger.hold = { reason: "hour-budget", timer: null };
    this.#armRelease(agentId, ledger);
    yield* this.#hooks.held({ agentId, reason: "hour-budget", usedTokens: used, limitTokens: perHour, turnId });
  }).bind(this);

  /**
   * A step of this turn finished. A turn past its limit stops here: the agent is held first, so the
   * queue does not start the next message as the stopped turn ends, then the turn is stopped.
   * Returns true when it stopped the turn.
   */
  readonly boundary = Effect.fn("TokenBudget.boundary")(function* (this: TokenBudget, agentId: string, turnId: string) {
    const turn = this.#turns.get(turnId);
    const { perTurn } = this.#hooks.limits();
    if (!turn?.armed || turn.stopped || perTurn <= 0) return false;
    turn.stopped = true;
    const ledger = this.#ledger(agentId, Date.now());
    // The turn stop replaces an hourly hold that the same report set: one marker says what happened.
    if (ledger.hold?.timer) clearTimeout(ledger.hold.timer);
    ledger.hold = { reason: "turn-budget", timer: null };
    yield* this.#hooks.held({
      agentId,
      reason: "turn-budget",
      usedTokens: turn.tokens,
      limitTokens: perTurn,
      turnId,
    });
    yield* this.#hooks.interrupt(agentId, turnId);
    return true;
  }).bind(this);

  /** The turn ended, so its count goes. */
  turnEnded(turnId: string): void {
    this.#turns.delete(turnId);
  }

  /** Ends the hold of this agent and starts a new window for it. False when no hold exists. */
  continueAgent(agentId: string): boolean {
    const ledger = this.#agents.get(agentId);
    if (!ledger) return false;
    ledger.since = Date.now();
    ledger.samples = [];
    const held = ledger.hold !== null;
    this.#clearHold(ledger);
    if (held) this.#hooks.scheduleDrain(agentId);
    return held;
  }

  /** The user changed a limit: a hold the new limits no longer justify ends. */
  readonly limitsChanged = Effect.fn("TokenBudget.limitsChanged")(function* (this: TokenBudget) {
    const { perHour, perTurn } = this.#hooks.limits();
    for (const [agentId, ledger] of this.#agents) {
      const hold = ledger.hold;
      if (!hold) continue;
      // A turn stop waits for the user, whatever the limit is now. Only an hourly hold ends by itself.
      if (hold.reason === "turn-budget") continue;
      if (perHour > 0 && this.#window(agentId) >= perHour) continue;
      yield* this.#release(agentId, ledger);
    }
    for (const turn of this.#turns.values()) if (perTurn <= 0) turn.armed = false;
  }).bind(this);

  /** Drops the agent's state when the agent is deleted. */
  forgetAgent(agentId: string): void {
    const ledger = this.#agents.get(agentId);
    if (ledger) this.#clearHold(ledger);
    this.#agents.delete(agentId);
    for (const [turnId, turn] of this.#turns) if (turn.agentId === agentId) this.#turns.delete(turnId);
  }

  readonly dispose = Effect.fn("TokenBudget.dispose")(function* (this: TokenBudget) {
    for (const ledger of this.#agents.values()) this.#clearHold(ledger);
    this.#agents.clear();
    this.#turns.clear();
    yield* Scope.close(this.#scope, Exit.void);
    this.#scope = Scope.makeUnsafe();
  }).bind(this);

  #ledger(agentId: string, now: number): AgentLedger {
    let ledger = this.#agents.get(agentId);
    if (!ledger) {
      ledger = { seeded: false, samples: [], since: 0, hold: null };
      // Seeded once: reports from before this start count for the hour they fall in.
      try {
        ledger.samples = this.#hooks
          .readSince(agentId, now - TOKEN_BUDGET_WINDOW_MS)
          .map((sample) => ({ at: sample.at, tokens: sample.tokens }));
        ledger.seeded = true;
      } catch {
        // A failed read counts from this start on. The limit then holds later, not never.
      }
      this.#agents.set(agentId, ledger);
    }
    return ledger;
  }

  #window(agentId: string, now = Date.now()): number {
    const ledger = this.#agents.get(agentId);
    if (!ledger) return 0;
    const from = Math.max(now - TOKEN_BUDGET_WINDOW_MS, ledger.since);
    ledger.samples = ledger.samples.filter((sample) => sample.at >= from);
    let total = 0;
    for (const sample of ledger.samples) total += sample.tokens;
    return total;
  }

  /** Arms the timer for the moment the window falls under the limit, if nothing more is spent. */
  #armRelease(agentId: string, ledger: AgentLedger): void {
    const hold = ledger.hold;
    if (hold?.reason !== "hour-budget") return;
    if (hold.timer) clearTimeout(hold.timer);
    const { perHour } = this.#hooks.limits();
    const now = Date.now();
    let total = this.#window(agentId, now);
    let delay = TOKEN_BUDGET_WINDOW_MS;
    for (const sample of ledger.samples) {
      total -= sample.tokens;
      if (total < perHour) {
        delay = sample.at + TOKEN_BUDGET_WINDOW_MS - now + 1_000;
        break;
      }
    }
    hold.timer = setTimeout(
      () => Effect.runFork(this.#fire(agentId, ledger).pipe(Effect.forkIn(this.#scope))),
      Math.max(0, Math.min(delay, MAX_TIMER_MS)),
    );
  }

  readonly #fire = Effect.fn("TokenBudget.fire")(function* (this: TokenBudget, agentId: string, ledger: AgentLedger) {
    if (this.#agents.get(agentId) !== ledger || ledger.hold?.reason !== "hour-budget") return;
    ledger.hold.timer = null;
    const { perHour } = this.#hooks.limits();
    if (perHour > 0 && this.#window(agentId) >= perHour) {
      this.#armRelease(agentId, ledger);
      return;
    }
    yield* this.#release(agentId, ledger);
  });

  readonly #release = Effect.fn("TokenBudget.release")(function* (
    this: TokenBudget,
    agentId: string,
    ledger: AgentLedger,
  ) {
    this.#clearHold(ledger);
    yield* this.#hooks.released(agentId);
    this.#hooks.scheduleDrain(agentId);
  });

  #clearHold(ledger: AgentLedger): void {
    if (ledger.hold?.timer) clearTimeout(ledger.hold.timer);
    ledger.hold = null;
  }
}
