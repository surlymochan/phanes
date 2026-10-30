import { INPUT_LIMITS } from "@openbot/contracts/input-limits";
import type { MemoryEntry } from "@openbot/contracts/ipc";
import { sourceText } from "@openbot/i18n/source";
import { MemoryStore, type MemoryTables } from "../memory-store";
import type { OpenBotDatabase } from "../openbot-database";

const MESSAGING_MEMORY_TABLES: MemoryTables = {
  table: "projection_messaging_memories",
  ownerColumn: "connection_id",
  ownerKey: "connectionId",
  aggregateType: "messaging-memory",
  limit: () => INPUT_LIMITS.messagingMemories,
  limitMessage: (limit) => sourceText("error.messaging.memoryLimit", { limit }),
};

/**
 * The facts of one messaging connection: a Slack workspace, a Discord server or a Telegram chat. The
 * same `MemoryStore` as an agent's or a channel's, with a connection for an owner. Unlike link
 * events, which keep ids only, a memory's events keep its text until the memory is deleted.
 */
export class MessagingMemoryStore extends MemoryStore {
  constructor(database: OpenBotDatabase) {
    super(database, MESSAGING_MEMORY_TABLES);
  }

  /**
   * A tool writes at call time. `commandId` is the call's own receipt key: a retried tool call reads
   * it and saves nothing more. A `memoryId` of another connection is not found, so the save fails.
   */
  saveFromTool(
    connectionId: string,
    input: { text: string; memoryId?: string; sourceTurnId: string; commandId: string },
  ): MemoryEntry {
    const memory = this.saveAutomaticEntry(connectionId, input);
    if (!memory) throw new Error(sourceText("error.backend.memoryGone"));
    return memory;
  }
}
