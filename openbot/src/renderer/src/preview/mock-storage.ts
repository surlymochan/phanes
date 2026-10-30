import type { GetStorageUsageInput, StorageDesktopApi, StorageUsage } from "@openbot/contracts/ipc";
import {
  FILES_AGENT_USAGE,
  FILES_BREAKDOWN,
  FILES_CHIEF_BREAKDOWN,
  FILES_CONVERSATIONS,
  FILES_OLD_RUNTIME_BYTES,
  FILES_ROWS,
} from "../../stories/files-fixtures";

const GB = 1024 ** 3;
const FREE_BYTES = 182 * GB;

/** The Files fixtures as one server's storage, so a delete or clear in the preview shows at once. */
export function createMockStorage(): StorageDesktopApi {
  let files = structuredClone(FILES_ROWS);
  let breakdown = structuredClone(FILES_BREAKDOWN);
  let oldRuntimeBytes = FILES_OLD_RUNTIME_BYTES;

  const usage = (input: GetStorageUsageInput): StorageUsage => {
    const inScope = (agentId: string | null, conversationId: string | null) =>
      input.scope === "host" ||
      (input.scope === "agent" ? agentId === input.agentId : conversationId === input.conversationId);
    const scopedFiles = files.filter((file) => inScope(file.agentId, file.conversation?.id ?? null));
    const agentBreakdown = input.scope === "agent" && input.agentId === "chief" ? FILES_CHIEF_BREAKDOWN : [];
    return structuredClone({
      scope: input.scope,
      agentId: input.agentId ?? null,
      conversationId: input.conversationId ?? null,
      scannedAt: new Date().toISOString(),
      freeBytes: FREE_BYTES,
      breakdown: input.scope === "host" ? breakdown : agentBreakdown,
      agents: FILES_AGENT_USAGE.filter((row) => input.scope === "host" || row.agentId === input.agentId),
      conversations: FILES_CONVERSATIONS.filter((row) => inScope(row.agentId, row.id)),
      files: scopedFiles,
      truncated: false,
    });
  };

  return {
    getUsage: async (input) => usage(input),
    getDisk: async () => ({ freeBytes: FREE_BYTES, totalBytes: 994 * GB }),
    deleteFile: async ({ fileId }) => {
      if (!files.some((file) => file.id === fileId)) throw new Error("The file does not exist or is already deleted.");
      files = files.filter((file) => file.id !== fileId);
    },
    clear: async ({ category }) => {
      breakdown = breakdown.map((row) => (row.category === category ? { ...row, bytes: 0 } : row));
    },
    openFile: async () => undefined,
    openLocation: async () => undefined,
    getOldRuntimeVersions: async () => ({ bytes: oldRuntimeBytes }),
    removeOldRuntimeVersions: async () => {
      breakdown = breakdown.map((row) =>
        row.category === "runtimes" ? { ...row, bytes: row.bytes - oldRuntimeBytes } : row,
      );
      oldRuntimeBytes = 0;
      return { bytes: 0 };
    },
  };
}
