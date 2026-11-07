/**
 * The first message of a new agent. It states the role, and adds the first task when the user wrote
 * one. With neither, the agent greets the user. Agent prompts stay in English; the user's words do
 * not change.
 */
export function agentInitialMessage(roleDescription: string, task = ""): string {
  const parts = [roleDescription.trim() ? `Your ongoing role is: ${roleDescription.trim()}` : "", task.trim()].filter(
    Boolean,
  );
  return parts.length > 0 ? parts.join("\n\n") : "Greet me briefly.";
}
