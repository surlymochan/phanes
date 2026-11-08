import type { VoltoDesktopApi } from "@volto/contracts/ipc";

/**
 * What the usage panel reaches in main: the analytics of a server, its agents, and the events that
 * make the report stale.
 */
export interface UsagePort {
  agent: Pick<VoltoDesktopApi["agent"], "getHostAnalytics" | "listAgents" | "onScopedEvent">;
  servers: Pick<VoltoDesktopApi["servers"], "onEvent">;
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function usagePort(): UsagePort {
  return window.volto;
}
