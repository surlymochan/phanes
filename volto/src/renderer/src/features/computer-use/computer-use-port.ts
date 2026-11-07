import type { VoltoDesktopApi } from "@volto/contracts/ipc";

/**
 * What Computer Use reaches in main: the driver state, the macOS permission panes and helper, and
 * the highlight placement. The helper and highlight windows have no provider tree, so this is a
 * module.
 */
export interface ComputerUsePort {
  computerUse: VoltoDesktopApi["computerUse"];
  remoteDesktop: Pick<VoltoDesktopApi["remoteDesktop"], "checkSetup">;
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function computerUsePort(): ComputerUsePort {
  return window.volto;
}
