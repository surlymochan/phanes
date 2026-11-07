import type { VoltoDesktopApi } from "@volto/contracts/ipc";

/**
 * What the account context reaches in main: the Volto account session, its devices, and the
 * usage the page rail and the Usage page show.
 */
export interface AccountPort {
  agent: Pick<VoltoDesktopApi["agent"], "getUsage">;
  auth: Pick<
    VoltoDesktopApi["auth"],
    | "cancelMobileConnect"
    | "createMobileConnect"
    | "createMobileConnectLink"
    | "getMobileConnectLink"
    | "getState"
    | "listAccountSessions"
    | "listMobileConnectedDevices"
    | "logout"
    | "onEvent"
    | "requestEmailCode"
    | "retry"
    | "revokeAccountSession"
    | "revokeMobileConnectedDevice"
    | "revokeMobileConnectLink"
    | "signInWithAdminKey"
    | "updateAvatar"
    | "updateName"
    | "verifyEmailCode"
  >;
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function accountPort(): AccountPort {
  return window.volto;
}
