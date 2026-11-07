import type { VoltoDesktopApi } from "@volto/contracts/ipc";

/**
 * What the settings context reaches in main: the app-wide preferences, the switches for updates,
 * notifications and the Dynamic Island, and the local agents of the Agents tab.
 */
export interface SettingsPort {
  getAgentDefaults: VoltoDesktopApi["getAgentDefaults"];
  getAgentMemoryLimitPreference: VoltoDesktopApi["getAgentMemoryLimitPreference"];
  getAnalyticsPreference: VoltoDesktopApi["getAnalyticsPreference"];
  getAppBehaviorPreference: VoltoDesktopApi["getAppBehaviorPreference"];
  getApprovalAutomation: VoltoDesktopApi["getApprovalAutomation"];
  getBusyMessageModePreference: VoltoDesktopApi["getBusyMessageModePreference"];
  getExternalLinkPreference: VoltoDesktopApi["getExternalLinkPreference"];
  listExternalLinkBrowsers: VoltoDesktopApi["listExternalLinkBrowsers"];
  chooseExternalLinkApp: VoltoDesktopApi["chooseExternalLinkApp"];
  onExternalLinkFallback: VoltoDesktopApi["onExternalLinkFallback"];
  getRemoteNetworkRoute: VoltoDesktopApi["getRemoteNetworkRoute"];
  getRemoteSessionReusePreference: VoltoDesktopApi["getRemoteSessionReusePreference"];
  getTokenBudgetPreference: VoltoDesktopApi["getTokenBudgetPreference"];
  onAgentMemoryLimitPreference: VoltoDesktopApi["onAgentMemoryLimitPreference"];
  onApprovalAutomation: VoltoDesktopApi["onApprovalAutomation"];
  onOpenSettings: VoltoDesktopApi["onOpenSettings"];
  setAgentDefaults: VoltoDesktopApi["setAgentDefaults"];
  setAnalyticsPreference: VoltoDesktopApi["setAnalyticsPreference"];
  setAppBehaviorPreference: VoltoDesktopApi["setAppBehaviorPreference"];
  setBusyMessageModePreference: VoltoDesktopApi["setBusyMessageModePreference"];
  setExternalLinkPreference: VoltoDesktopApi["setExternalLinkPreference"];
  setRemoteNetworkRoute: VoltoDesktopApi["setRemoteNetworkRoute"];
  setRemoteSessionReusePreference: VoltoDesktopApi["setRemoteSessionReusePreference"];
  setTokenBudgetPreference: VoltoDesktopApi["setTokenBudgetPreference"];
  agent: Pick<VoltoDesktopApi["agent"], "listAgents" | "updateAgent">;
  dynamicIsland: Pick<
    VoltoDesktopApi["dynamicIsland"],
    "getBuiltInDisplayGeometry" | "getPreference" | "setPreference"
  >;
  notifications: Pick<VoltoDesktopApi["notifications"], "getPreference" | "openSettings" | "setPreference" | "test">;
  update: Pick<VoltoDesktopApi["update"], "getPreference" | "onPreference" | "setPreference">;
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function settingsPort(): SettingsPort {
  return window.volto;
}
