import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { electronSafeStorageSwitches } from "../src/main/electron-safe-storage";
import { electronSafeStorageLaunchArguments, withoutElectronRuntimeFlags } from "./electron-spawn-env";

const ROOT = resolve(import.meta.dirname, "..");
const OUT = join(ROOT, ".volto-build", "electron-safe-storage-e2e");
const cases: Array<{ name: string; environment: NodeJS.ProcessEnv; isolated: boolean }> = [
  { name: "production", environment: {}, isolated: false },
  { name: "false CI", environment: { CI: "false" }, isolated: false },
  { name: "zero CI", environment: { CI: "0" }, isolated: false },
  { name: "empty flags", environment: { CI: "", VOLTO_E2E: "", OPENBOT_E2E: "", SWRM_RUN_ID: "" }, isolated: false },
  {
    name: "browser settings",
    environment: { PLAYWRIGHT_BROWSERS_PATH: "0", PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1" },
    isolated: false,
  },
  { name: "CI true", environment: { CI: "true" }, isolated: true },
  { name: "CI one", environment: { CI: "1" }, isolated: true },
  { name: "Volto E2E", environment: { VOLTO_E2E: "1" }, isolated: true },
  { name: "OpenBot E2E", environment: { OPENBOT_E2E: "1" }, isolated: true },
  { name: "worker zero", environment: { PLAYWRIGHT_WORKER_INDEX: "0" }, isolated: true },
  { name: "OpenSwarm run", environment: { OPENSWARM_RUN_ID: "probe" }, isolated: true },
  { name: "SWRM run", environment: { SWRM_RUN_ID: "probe" }, isolated: true },
];
await mkdir(OUT, { recursive: true });
const root = await mkdtemp(join(tmpdir(), "safe-storage-"));
const results: object[] = [];
let passed = false;
try {
  const bundle = join(root, "probe.mjs");
  const build = await Bun.build({
    entrypoints: [join(ROOT, "scripts/electron-safe-storage-electron.ts")],
    target: "node",
    external: ["electron"],
  });
  assert.equal(build.success, true);
  const artifact = build.outputs[0];
  assert.ok(artifact);
  await Bun.write(bundle, artifact);
  const executable = join(
    ROOT,
    "node_modules/electron/dist",
    (await readFile(join(ROOT, "node_modules/electron/path.txt"), "utf8")).trim(),
  );
  for (const item of [
    ...cases,
    { name: "development", environment: {}, isolated: true },
    { name: "launch", environment: {}, isolated: true },
  ]) {
    const packaged = item.name !== "development" && item.name !== "launch";
    // Test both platform policies, even when only one Electron binary can run here.
    for (const platform of ["darwin", "linux"] as const) {
      assert.equal(
        electronSafeStorageSwitches(platform, packaged, item.environment).length > 0,
        item.isolated,
        item.name,
      );
    }
    if (process.platform !== "darwin" && process.platform !== "linux") continue;
    const report = join(root, "ready.json");
    const child = spawnSync(
      executable,
      [
        ...(item.name === "launch" ? electronSafeStorageLaunchArguments() : []),
        ...(process.platform === "linux" ? ["--no-sandbox"] : []),
        bundle,
        `--profile=${join(root, `profile-${results.length}`)}`,
        `--report=${report}`,
        `--mode=${item.name}`,
        `--packaged=${packaged}`,
        `--isolated=${item.isolated}`,
      ],
      {
        // An allowlist prevents inherited lane/CI markers from changing production cases.
        env: withoutElectronRuntimeFlags({
          PATH: process.env.PATH,
          HOME: root,
          TMPDIR: process.env.TMPDIR,
          DISPLAY: process.env.DISPLAY,
          XAUTHORITY: process.env.XAUTHORITY,
          DBUS_SESSION_BUS_ADDRESS: `unix:path=${join(root, "no-bus")}`,
          ...item.environment,
        }),
        timeout: 30_000,
        encoding: "utf8",
      },
    );
    assert.equal(child.status, 0, `${item.name}: ${child.error ?? child.stderr}`);
    results.push({ name: item.name, ...JSON.parse(await readFile(report, "utf8")) });
  }
  passed = true;
} finally {
  await writeFile(
    join(OUT, "report.json"),
    `${JSON.stringify({ passed, platform: process.platform, results }, null, 2)}\n`,
  );
  await rm(root, { recursive: true, force: true });
}
process.stdout.write(
  `PASS: ${results.length} Electron cases; report: .volto-build/electron-safe-storage-e2e/report.json\n`,
);
