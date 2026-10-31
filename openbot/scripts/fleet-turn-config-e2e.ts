import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

if (process.argv.includes("--missing")) {
  process.env.OPENBOT_FLEET_CAPACITY_REQUIRED = "1";
  delete process.env.OPENBOT_FLEET_CAPACITY_CONFIG;
  const { configuredFleetTurnGate } = await import("../src/backend/agent/fleet-turn-config");
  assert.throws(() => configuredFleetTurnGate(), /configuration is required/);
} else if (process.argv.includes("--relative")) {
  const root = await mkdtemp(join(tmpdir(), "openbot-fleet-config-e2e-"));
  try {
    const file = join(root, "config.json");
    await writeFile(file, JSON.stringify({ journalDirectory: join(root, "journal"), command: ["ssh"] }));
    process.env.OPENBOT_FLEET_CAPACITY_CONFIG = file;
    const { configuredFleetTurnGate } = await import("../src/backend/agent/fleet-turn-config");
    assert.throws(() => configuredFleetTurnGate());
  } finally {
    await rm(root, { recursive: true, force: true });
  }
} else {
  const directory = join(process.cwd(), ".openbot-build", "fleet-turn-config-e2e");
  const checks: string[] = [];
  try {
    for (const scenario of ["--missing", "--relative"]) {
      const result = spawnSync(process.execPath, [import.meta.path, scenario], { encoding: "utf8" });
      assert.equal(result.status, 0, `${scenario} failed: ${result.stderr}`);
      checks.push(scenario);
    }
    await mkdir(directory, { recursive: true });
    await writeFile(join(directory, "report.json"), `${JSON.stringify({ passed: true, checks }, null, 2)}\n`);
    process.stdout.write(`PASS ${join(directory, "report.json")}\n`);
  } catch (error) {
    await mkdir(directory, { recursive: true });
    await writeFile(
      join(directory, "report.json"),
      `${JSON.stringify({ passed: false, checks, error: String(error) }, null, 2)}\n`,
    );
    throw error;
  }
}
