import { INPUT_LIMITS } from "./input-limits";
import { isBoundedString, isIdentifier } from "./ipc-bounded-values";
import { isDynamicRecord } from "./runtime-values";

/** One earlier question of a side question, with the answer the agent gave. */
export interface SideQuestionTurn {
  question: string;
  answer: string;
}

/**
 * A question about an agent's chat that does not go into the chat. The host keeps nothing of it, so
 * the request carries the earlier turns.
 */
export interface AskSideQuestionInput {
  agentId: string;
  question: string;
  earlier: SideQuestionTurn[];
}

function isSideQuestionTurn(value: unknown): value is SideQuestionTurn {
  return (
    isDynamicRecord(value) &&
    isBoundedString(value.question, INPUT_LIMITS.messageText) &&
    isBoundedString(value.answer, INPUT_LIMITS.sideQuestionAnswer)
  );
}

export function parseAskSideQuestion(value: unknown): AskSideQuestionInput {
  if (
    !isDynamicRecord(value) ||
    !isIdentifier(value.agentId) ||
    !isBoundedString(value.question, INPUT_LIMITS.messageText) ||
    !value.question.trim() ||
    !Array.isArray(value.earlier) ||
    value.earlier.length > INPUT_LIMITS.sideQuestionTurns ||
    !value.earlier.every(isSideQuestionTurn)
  ) {
    throw new Error("Provide a valid side question.");
  }
  return {
    agentId: value.agentId,
    question: value.question.trim(),
    earlier: value.earlier.map((turn) => ({ question: turn.question, answer: turn.answer })),
  };
}
