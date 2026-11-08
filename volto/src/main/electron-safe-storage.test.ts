import { describe, expect, it } from "vitest";
import { configureElectronSafeStorage, electronSafeStorageSwitches } from "./electron-safe-storage";

describe("Electron safe storage switches", () => {
  it("uses the mock keychain for development on macOS and basic storage on Linux", () => {
    expect(electronSafeStorageSwitches("darwin", false, {})).toEqual([{ name: "use-mock-keychain" }]);
    expect(electronSafeStorageSwitches("linux", false, {})).toEqual([{ name: "password-store", value: "basic" }]);
  });

  it.each([
    ["CI", { CI: "1" }],
    ["CI true", { CI: "true" }],
    ["Playwright test", { PLAYWRIGHT_TEST: "1" }],
    ["Playwright", { PLAYWRIGHT_WORKER_INDEX: "0" }],
    ["Volto E2E", { VOLTO_E2E: "1" }],
    ["OpenBot E2E", { OPENBOT_E2E: "1" }],
    ["OpenSwarm run", { OPENSWARM_RUN_ID: "run" }],
    ["SWRM run", { SWRM_RUN_ID: "run" }],
  ])("uses safe storage isolation in a packaged test environment (%s)", (_name, environment) => {
    expect(electronSafeStorageSwitches("darwin", true, environment)).toEqual([{ name: "use-mock-keychain" }]);
    expect(electronSafeStorageSwitches("linux", true, environment)).toEqual([
      { name: "password-store", value: "basic" },
    ]);
  });

  it.each([
    {},
    { CI: "false" },
    { CI: "0" },
    { CI: "" },
    { VOLTO_E2E: "false", OPENBOT_E2E: "0" },
    { VOLTO_E2E: "", OPENBOT_E2E: "", OPENSWARM_RUN_ID: "", SWRM_RUN_ID: "  " },
    { PLAYWRIGHT_BROWSERS_PATH: "0", PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1" },
    { PLAYWRIGHT_WORKER_INDEX: "", PLAYWRIGHT_TEST: "false" },
    { PLAYWRIGHT_WORKER_INDEX: "false" },
  ])("keeps packaged storage with inactive test settings: %j", (environment) => {
    expect(electronSafeStorageSwitches("darwin", true, environment)).toEqual([]);
    expect(electronSafeStorageSwitches("linux", true, environment)).toEqual([]);
  });

  it("keeps packaged production behavior unchanged and applies selected switches", () => {
    expect(electronSafeStorageSwitches("darwin", true, {})).toEqual([]);
    expect(electronSafeStorageSwitches("linux", true, {})).toEqual([]);

    const appended: Array<{ name: string; value?: string }> = [];
    const switches = configureElectronSafeStorage(
      { appendSwitch: (name, value) => appended.push({ name, value }) },
      "darwin",
      false,
      {},
    );
    expect(appended).toEqual(switches);
  });
});
