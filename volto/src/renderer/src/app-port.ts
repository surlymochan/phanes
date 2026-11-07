import type { VoltoDesktopApi } from "@volto/contracts/ipc";

/**
 * What the app shell and the cross-domain root modules reach in main, plus the shell actions
 * (`openUrl`, `openExternal`) that several domains share.
 */
export interface AppPort {
  getAppAppearancePreference: VoltoDesktopApi["getAppAppearancePreference"];
  getAppInfo: VoltoDesktopApi["getAppInfo"];
  getAppInterfaceSizePreference: VoltoDesktopApi["getAppInterfaceSizePreference"];
  getAppLanguagePreference: VoltoDesktopApi["getAppLanguagePreference"];
  getAppLogoColorPreference: VoltoDesktopApi["getAppLogoColorPreference"];
  onAppAppearancePreference: VoltoDesktopApi["onAppAppearancePreference"];
  onAppInterfaceSizePreference: VoltoDesktopApi["onAppInterfaceSizePreference"];

  onAppLanguagePreference: VoltoDesktopApi["onAppLanguagePreference"];
  onAppLogoColorPreference: VoltoDesktopApi["onAppLogoColorPreference"];
  openExternal: VoltoDesktopApi["openExternal"];
  openUrl: VoltoDesktopApi["openUrl"];
  openChatWindow: VoltoDesktopApi["openChatWindow"];
  getChatWindows: VoltoDesktopApi["getChatWindows"];
  onChatWindows: VoltoDesktopApi["onChatWindows"];
  setAppAppearancePreference: VoltoDesktopApi["setAppAppearancePreference"];
  setAppInterfaceSizePreference: VoltoDesktopApi["setAppInterfaceSizePreference"];

  setAppLanguagePreference: VoltoDesktopApi["setAppLanguagePreference"];
  setAppLogoColorPreference: VoltoDesktopApi["setAppLogoColorPreference"];
  agent: Pick<
    VoltoDesktopApi["agent"],
    "readConversationPage" | "searchConversationMessages" | "searchConversationFiles" | "listRoutines" | "listAgents"
  >;
  agentTemplates: Pick<VoltoDesktopApi["agentTemplates"], "onOpenLink" | "takePendingLink">;
  acpRegistry: VoltoDesktopApi["acpRegistry"];
  hostedSites: VoltoDesktopApi["hostedSites"];
  billing: VoltoDesktopApi["billing"];
  license: VoltoDesktopApi["license"];
  hostedServers: VoltoDesktopApi["hostedServers"];
  routineFeed: VoltoDesktopApi["routineFeed"];
  events: VoltoDesktopApi["events"];
  plugins: Pick<VoltoDesktopApi["plugins"], "onOpenListing" | "takePendingListing">;
  servers: Pick<VoltoDesktopApi["servers"], "onInvite" | "takePendingInvite">;
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function appPort(): AppPort {
  return window.volto;
}
