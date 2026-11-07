import type { VoltoDesktopApi } from "@volto/contracts/ipc";

/**
 * What team presence reaches in main: who is online, and whether this member is typing.
 */
export interface TeamPort {
  servers: Pick<VoltoDesktopApi["servers"], "onPresence" | "setTyping">;
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function teamPort(): TeamPort {
  return window.volto;
}
