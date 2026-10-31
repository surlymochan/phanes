import { isDynamicRecord, isOneOf } from "./runtime-values";

/**
 * The most tokens a limit can hold. A larger value is a typing error, not a budget, and it keeps
 * every count a safe integer.
 */
export const TOKEN_BUDGET_MAX_TOKENS = 1_000_000_000_000;

/**
 * The token limits of one agent, as they are stored and as they cross IPC. Each is a count of
 * processed tokens: input, cached input, cache writes and output. 0 means no limit.
 *
 * - `perHour`: the tokens one agent may use in any 60 minutes. At the limit the agent starts no new
 *   turn until the user continues or the window falls under the limit again. A running turn goes on.
 * - `perTurn`: the tokens one turn may use. Past the limit the turn stops at the end of its current
 *   step, and the agent waits for the user to continue.
 */
export interface TokenBudgetPreference {
  perHour: number;
  perTurn: number;
}

export const DEFAULT_TOKEN_BUDGET: TokenBudgetPreference = { perHour: 15_000_000, perTurn: 5_000_000 };

function isTokenLimit(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= TOKEN_BUDGET_MAX_TOKENS;
}

export function isTokenBudgetPreference(value: unknown): value is TokenBudgetPreference {
  return isDynamicRecord(value) && isTokenLimit(value.perHour) && isTokenLimit(value.perTurn);
}

/** Why an agent stopped or waits for the user: a limit of the token budget, or the provider plan. */
export const AGENT_PAUSE_REASONS = ["hour-budget", "turn-budget", "plan-limit"] as const;
export type AgentPauseReason = (typeof AGENT_PAUSE_REASONS)[number];

export function isAgentPauseReason(value: unknown): value is AgentPauseReason {
  return isOneOf(AGENT_PAUSE_REASONS, value);
}

/** The item type of the chat marker that tells the user an agent stopped or waits, with its Continue action. */
export const AGENT_PAUSE_ITEM_TYPE = "agent-pause";

/**
 * What an `agent-pause` marker says and does. The marker is the record: it holds everything that
 * Continue needs, so a restart of OpenBot loses nothing.
 *
 * - `usedTokens` and `limitTokens` are the count and the limit of a budget pause; 0 and 0 for a plan limit.
 * - `resetsAt` is the plan reset in epoch seconds, when the provider gave one.
 * - `resume` is true when a turn stopped: Continue then starts a short turn that carries on with the work.
 * - `resolved` is true once the agent went on, by Continue or by itself.
 */
export interface ConversationPause {
  reason: AgentPauseReason;
  usedTokens: number;
  limitTokens: number;
  resetsAt: number | null;
  resume: boolean;
  resolved: boolean;
}

export function isConversationPause(value: unknown): value is ConversationPause {
  return (
    isDynamicRecord(value) &&
    isAgentPauseReason(value.reason) &&
    isTokenLimit(value.usedTokens) &&
    isTokenLimit(value.limitTokens) &&
    (value.resetsAt === null || (typeof value.resetsAt === "number" && Number.isFinite(value.resetsAt))) &&
    typeof value.resume === "boolean" &&
    typeof value.resolved === "boolean"
  );
}

/** Continues an agent that an `agent-pause` marker in its chat holds. */
export interface ContinueAgentPauseInput {
  agentId: string;
  /** The id of the marker message. */
  messageId: string;
}
