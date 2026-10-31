import type { HostAdminDesktopApi, HostTimezoneSettings } from "@openbot/contracts/ipc";

/** Preview has one host, so every server shares this timezone. */
export function createMockHostTimezone(): Pick<HostAdminDesktopApi, "getTimezone" | "setTimezone"> {
  const settings: HostTimezoneSettings = { timezone: null, systemTimezone: "UTC" };
  return {
    getTimezone: async () => ({ ...settings }),
    setTimezone: async (timezone) => {
      settings.timezone = timezone;
      return { ...settings };
    },
  };
}
