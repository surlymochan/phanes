import type { VoltoDesktopApi } from "@volto/contracts/ipc";

/**
 * What provider state reaches in main: connecting, signing in and refreshing providers, and the
 * runtimes Volto installs for them, on this computer or on a host the account administers.
 */
export interface ProvidersPort {
  cancelProviderCodeLogin: VoltoDesktopApi["cancelProviderCodeLogin"];
  connectProvider: VoltoDesktopApi["connectProvider"];
  cancelProviderSignIn: VoltoDesktopApi["cancelProviderSignIn"];
  refreshAgentProviders: VoltoDesktopApi["refreshAgentProviders"];
  setProviderOn: VoltoDesktopApi["setProviderOn"];
  restartProvider: VoltoDesktopApi["restartProvider"];
  cancelProviderRestart: VoltoDesktopApi["cancelProviderRestart"];
  startProviderCodeLogin: VoltoDesktopApi["startProviderCodeLogin"];
  providerRuntimes: VoltoDesktopApi["providerRuntimes"];
  connectProviderAccount: VoltoDesktopApi["connectProviderAccount"];
  cancelProviderAccountSignIn: VoltoDesktopApi["cancelProviderAccountSignIn"];
  renameProviderAccount: VoltoDesktopApi["renameProviderAccount"];
  removeProviderAccount: VoltoDesktopApi["removeProviderAccount"];
  providerUsageResets: VoltoDesktopApi["providerUsageResets"];
  resetProviderUsage: VoltoDesktopApi["resetProviderUsage"];
  /** The same, for the host of a joined server the account administers. */
  providerAdmin: VoltoDesktopApi["providerAdmin"];
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function providersPort(): ProvidersPort {
  return window.volto;
}
