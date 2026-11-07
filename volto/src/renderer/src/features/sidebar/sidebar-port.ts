import type { VoltoDesktopApi } from "@volto/contracts/ipc";

/**
 * What the sidebar reaches in main: the saved sidebar layout and its changes, and the free space
 * on the disk of a server for the storage card.
 */
export interface SidebarPort {
  agent: Pick<VoltoDesktopApi["agent"], "getSidebarLayout" | "mutateSidebarLayout">;
  storage: Pick<VoltoDesktopApi["storage"], "getDisk">;
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function sidebarPort(): SidebarPort {
  return window.volto;
}
