import type { VoltoDesktopApi } from "@volto/contracts/ipc";

/**
 * What the updates context reaches in main: the app update status, its check, download and
 * install steps, the cancel of a restart that a server admin asked for, and the restart when idle.
 */
export interface UpdatesPort {
  update: Pick<
    VoltoDesktopApi["update"],
    | "check"
    | "download"
    | "getStatus"
    | "install"
    | "onEvent"
    | "cancelScheduledRestart"
    | "restartWhenIdle"
    | "cancelIdleRestart"
  >;
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function updatesPort(): UpdatesPort {
  return window.volto;
}
