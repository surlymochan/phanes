import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  FleetTurnGate,
  type FleetTurnReservation,
  type FleetTurnTransport,
} from "../src/backend/agent/fleet-turn-gate";
import { runCauseEffect } from "../src/backend/effect-boundary";

// A separate coordinator process persists admission before replying. Its state is deliberately
// independent of the Hub process so a lost reply can be checked after the gate is restarted.
const root = await mkdtemp(join(tmpdir(), "openbot-fleet-e2e-"));
const coordinator = join(root, "coordinator.mjs");
const reservations = join(root, "reservations");
const journal = join(root, "journal");
const output = join(process.cwd(), ".openbot-build", "fleet-turn-gate-e2e", "report.json");
await mkdir(reservations);
await writeFile(
  coordinator,
  `
import { readFileSync, writeFileSync, unlinkSync, existsSync } from 'node:fs';
import { join } from 'node:path';
const { operation, reservation, mode, directory } = JSON.parse(readFileSync(0, 'utf8'));
const path = join(directory, reservation.run_id);
if (operation === 'acquire') {
  if (mode === 'deny') { process.stdout.write(JSON.stringify({ admitted: false })); process.exit(0); }
  writeFileSync(path, 'held', { flag: 'wx', mode: 0o600 });
  if (mode === 'lost') process.exit(1);
  process.stdout.write(JSON.stringify({ admitted: true }));
} else {
  if (mode === 'delay-release') {
    writeFileSync(join(directory, 'release-pending'), '');
    while (!existsSync(join(directory, 'release-allowed'))) await new Promise(resolve => setTimeout(resolve, 20));
  }
  unlinkSync(path);
  process.stdout.write('{}');
}
`,
);

let mode = "normal";
function call(operation: string, reservation: FleetTurnReservation): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [coordinator], { stdio: ["pipe", "pipe", "ignore"] });
    let response = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      response += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => (code === 0 ? resolve(response) : reject(new Error("Coordinator response lost."))));
    child.stdin.end(JSON.stringify({ operation, reservation, mode, directory: reservations }));
  });
}
const transport: FleetTurnTransport = {
  acquire: async (reservation) => JSON.parse(await call("acquire", reservation)),
  release: async (reservation) => {
    await call("release", reservation);
  },
};
const checks: string[] = [];
try {
  const gate = await runCauseEffect(FleetTurnGate.open(journal, transport));
  assert.equal(await runCauseEffect(gate.begin("thread", "codex", null)), true);
  const [entry] = await readdir(journal);
  assert.ok(entry);
  assert.equal((await stat(join(journal, entry))).mode & 0o777, 0o600);
  gate.sent("thread");
  await runCauseEffect(gate.completed("thread", "current"));
  await runCauseEffect(gate.completed("thread", "old"));
  assert.equal((await readdir(reservations)).length, 1);
  await runCauseEffect(gate.started("thread", "current"));
  assert.deepEqual(await readdir(reservations), []);
  assert.deepEqual(await readdir(journal), []);
  checks.push("early matching completion survives a later stale completion");

  mode = "deny";
  assert.equal(await runCauseEffect(gate.begin("denied", "codex", null)), false);
  assert.deepEqual(await readdir(journal), []);
  checks.push("denial removes the local journal");

  mode = "normal";
  assert.equal(await runCauseEffect(gate.begin("completed", "agy", null)), true);
  gate.sent("completed");
  await runCauseEffect(gate.started("completed", "completed-turn"));
  mode = "delay-release";
  const release = runCauseEffect(gate.completed("completed", "completed-turn"));
  const deadline = Date.now() + 10_000;
  while (!(await readdir(reservations)).includes("release-pending")) {
    if (Date.now() > deadline) throw new Error("Coordinator release did not start.");
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  gate.uncertain("completed");
  await writeFile(join(reservations, "release-allowed"), "");
  await release;
  assert.equal(gate.requiresRecovery("agy"), false);
  await rm(join(reservations, "release-pending"));
  await rm(join(reservations, "release-allowed"));
  checks.push("shutdown during a confirmed completion does not block later turns");

  mode = "lost";
  await assert.rejects(runCauseEffect(gate.begin("lost", "codex", null)), /response lost/);
  assert.equal((await readdir(reservations)).length, 1);
  assert.equal((await readdir(journal)).length, 1);
  mode = "normal";
  assert.equal(await runCauseEffect(gate.begin("unrelated", "cursor", null)), true);
  await runCauseEffect(gate.cancelBeforeSend("unrelated"));
  const restarted = await runCauseEffect(FleetTurnGate.open(journal, transport));
  assert.equal(restarted.recoveryRequired, true);
  assert.equal(restarted.requiresRecovery("codex"), true);
  assert.equal(restarted.requiresRecovery("cursor"), false);
  await assert.rejects(runCauseEffect(restarted.begin("other", "codex", null)), /requires recovery/);
  assert.equal(await runCauseEffect(restarted.begin("unrelated-after-restart", "cursor", null)), true);
  await runCauseEffect(restarted.cancelBeforeSend("unrelated-after-restart"));
  assert.equal((await readdir(reservations)).length, 1);
  checks.push(
    "lost admission response retains its backend slot without blocking unrelated backends, including after restart",
  );

  const corrupt = join(root, "corrupt-journal");
  await mkdir(corrupt);
  await writeFile(join(corrupt, "broken.json"), "{}");
  const corruptGate = await runCauseEffect(FleetTurnGate.open(corrupt, transport));
  assert.equal(corruptGate.requiresRecovery("codex"), true);
  assert.equal(corruptGate.requiresRecovery("cursor"), true);
  checks.push("an unreadable journal fails closed for every backend");

  await mkdir(join(process.cwd(), ".openbot-build", "fleet-turn-gate-e2e"), { recursive: true });
  await writeFile(output, `${JSON.stringify({ passed: true, checks }, null, 2)}\n`);
  assert.equal(JSON.parse(await readFile(output, "utf8")).passed, true);
  process.stdout.write(`PASS ${output}\n`);
} catch (error) {
  await mkdir(join(process.cwd(), ".openbot-build", "fleet-turn-gate-e2e"), { recursive: true });
  await writeFile(output, `${JSON.stringify({ passed: false, checks, error: String(error) }, null, 2)}\n`);
  throw error;
} finally {
  await rm(root, { recursive: true, force: true });
}
