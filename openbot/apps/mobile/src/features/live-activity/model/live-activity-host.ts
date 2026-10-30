import { create } from "zustand";

/**
 * The server whose host updates the Live Activity, and when iOS started the activity, in
 * milliseconds. The notification registration gives that host the start, because the released
 * `live-activity-push-v1` registration has no field for it, and a host that restarts does not know it.
 */
export const useLiveActivityHost = create<{ serverId: string | null; startedAt: number | null }>(() => ({
  serverId: null,
  startedAt: null,
}));
