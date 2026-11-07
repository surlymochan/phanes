export interface ElectronCommandLine {
  appendSwitch(name: string, value?: string): void;
}

export interface ElectronSwitch {
  name: string;
  value?: string;
}

const TEST_FLAGS = ["CI", "VOLTO_E2E", "OPENBOT_E2E", "PLAYWRIGHT_TEST"] as const;
const RUN_IDS = ["OPENSWARM_RUN_ID", "SWRM_RUN_ID"] as const;

export function isTestEnvironment(environment: NodeJS.ProcessEnv): boolean {
  if (TEST_FLAGS.some((key) => environment[key] === "1" || environment[key] === "true")) return true;
  if (RUN_IDS.some((key) => Boolean(environment[key]?.trim()))) return true;
  // A worker index is a test-run marker; browser installation settings are not.
  return /^\d+$/.test(environment.PLAYWRIGHT_WORKER_INDEX ?? "");
}

export function electronSafeStorageSwitches(
  platform: NodeJS.Platform,
  isPackaged: boolean,
  environment: NodeJS.ProcessEnv,
): ElectronSwitch[] {
  if (isPackaged && !isTestEnvironment(environment)) return [];
  if (platform === "darwin") return [{ name: "use-mock-keychain" }];
  if (platform === "linux") return [{ name: "password-store", value: "basic" }];
  return [];
}

export function configureElectronSafeStorage(
  commandLine: ElectronCommandLine,
  platform: NodeJS.Platform,
  isPackaged: boolean,
  environment: NodeJS.ProcessEnv,
): ElectronSwitch[] {
  const switches = electronSafeStorageSwitches(platform, isPackaged, environment);
  for (const { name, value } of switches) commandLine.appendSwitch(name, value);
  return switches;
}
