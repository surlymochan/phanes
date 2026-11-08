import type { VoltoDesktopApi } from "@volto/contracts/ipc";

/**
 * What onboarding reaches in main: the saved setup, and the preview of an invite before the user
 * joins.
 */
export interface OnboardingPort {
  getSetupState: VoltoDesktopApi["getSetupState"];
  saveSetup: VoltoDesktopApi["saveSetup"];
  onSetupState: VoltoDesktopApi["onSetupState"];
  servers: Pick<VoltoDesktopApi["servers"], "previewInvite">;
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function onboardingPort(): OnboardingPort {
  return window.volto;
}
