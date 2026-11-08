import { z } from "zod";

/** The side threads that have a tab on this device, in tab order, and the open tab. */
export interface AgentThreadTabs {
  open: string[];
  active: string | null;
}

/** Server id → agent id → tabs. */
export type ThreadTabsByServer = Record<string, Record<string, AgentThreadTabs>>;

const THREAD_TABS_STORAGE_KEY = "openbot:thread-tabs:v1";
/** More tabs than this do not fit in a header. */
const MAX_STORED_TABS = 50;

type ThreadTabsStorage = Pick<Storage, "getItem" | "setItem">;

const id = z.string().trim().min(1).max(200);
const agentTabsSchema = z.object({
  open: z.array(id.nullable().catch(null)).catch([]),
  active: id.nullable().catch(null),
});
const threadTabsSchema = z.record(id, z.record(id, agentTabsSchema.nullable().catch(null)).catch({}));

export function readThreadTabs(storage: ThreadTabsStorage = window.localStorage): ThreadTabsByServer {
  try {
    const parsed = threadTabsSchema.safeParse(JSON.parse(storage.getItem(THREAD_TABS_STORAGE_KEY) ?? "{}"));
    if (!parsed.success) return {};
    const result: ThreadTabsByServer = {};
    for (const [serverId, agents] of Object.entries(parsed.data)) {
      const byAgent: Record<string, AgentThreadTabs> = {};
      for (const [agentId, tabs] of Object.entries(agents)) {
        if (!tabs) continue;
        const open = [...new Set(tabs.open.flatMap((tab) => (tab ? [tab] : [])))].slice(0, MAX_STORED_TABS);
        if (open.length > 0 || tabs.active) byAgent[agentId] = { open, active: tabs.active };
      }
      if (Object.keys(byAgent).length > 0) result[serverId] = byAgent;
    }
    return result;
  } catch {
    return {};
  }
}

export function writeThreadTabs(tabs: ThreadTabsByServer, storage: ThreadTabsStorage = window.localStorage): void {
  try {
    storage.setItem(THREAD_TABS_STORAGE_KEY, JSON.stringify(tabs));
  } catch {
    // A UI preference must not block navigation when browser storage is unavailable.
  }
}

/** A side thread that is archived and waits for the end of its Undo time before the delete. */
export interface PendingThreadDelete {
  serverId: string;
  agentId: string;
  threadId: string;
  /** Epoch milliseconds. After this time, any window of this device can finish the delete. */
  deadline: number;
  /** The thread was not archived before the delete, so Undo restores it. */
  restore: boolean;
}

const THREAD_DELETES_STORAGE_KEY = "openbot:thread-deletes:v1";
/** A device does not delete more threads than this in one Undo time. */
const MAX_STORED_DELETES = 200;

const pendingDeleteSchema = z.object({
  serverId: id,
  agentId: id,
  threadId: id,
  deadline: z.number().finite(),
  restore: z.boolean(),
});
const pendingDeletesSchema = z.array(pendingDeleteSchema.nullable().catch(null)).catch([]);

export function readPendingThreadDeletes(storage: ThreadTabsStorage = window.localStorage): PendingThreadDelete[] {
  try {
    const parsed = pendingDeletesSchema.parse(JSON.parse(storage.getItem(THREAD_DELETES_STORAGE_KEY) ?? "[]"));
    return parsed.flatMap((entry) => (entry ? [entry] : [])).slice(0, MAX_STORED_DELETES);
  } catch {
    return [];
  }
}

function writePendingThreadDeletes(deletes: readonly PendingThreadDelete[], storage: ThreadTabsStorage): void {
  try {
    storage.setItem(THREAD_DELETES_STORAGE_KEY, JSON.stringify(deletes.slice(-MAX_STORED_DELETES)));
  } catch {
    // Without browser storage, a delete still runs at the end of its Undo time in this window.
  }
}

const sameDelete = (a: PendingThreadDelete, b: Pick<PendingThreadDelete, "serverId" | "threadId">) =>
  a.serverId === b.serverId && a.threadId === b.threadId;

export function addPendingThreadDelete(
  entry: PendingThreadDelete,
  storage: ThreadTabsStorage = window.localStorage,
): void {
  writePendingThreadDeletes(
    [...readPendingThreadDeletes(storage).filter((stored) => !sameDelete(stored, entry)), entry],
    storage,
  );
}

/** Removes a stored delete. Returns false when another window removed it first. */
export function takePendingThreadDelete(
  entry: Pick<PendingThreadDelete, "serverId" | "threadId">,
  storage: ThreadTabsStorage = window.localStorage,
): boolean {
  const stored = readPendingThreadDeletes(storage);
  const kept = stored.filter((candidate) => !sameDelete(candidate, entry));
  if (kept.length === stored.length) return false;
  writePendingThreadDeletes(kept, storage);
  return true;
}

/** Removes the stored deletes of a deleted agent: its threads went with it. */
export function forgetPendingThreadDeletes(
  serverId: string,
  agentId: string,
  storage: ThreadTabsStorage = window.localStorage,
): void {
  const stored = readPendingThreadDeletes(storage);
  const kept = stored.filter((entry) => entry.serverId !== serverId || entry.agentId !== agentId);
  if (kept.length !== stored.length) writePendingThreadDeletes(kept, storage);
}
