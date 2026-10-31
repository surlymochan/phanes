import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { AgentClient } from "../src/backend/agent-client";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { NO_PROVIDER_CREDENTIALS, requireProviderDriver } from "../src/backend/provider-drivers";
import { installFakeAgent, runTurn, startThread, stopClients } from "./provider-fakes/host";

const root = await mkdtemp(join(tmpdir(), "openbot-copilot-fleet-e2e-"));
const journal = join(root, "journal");
const held = join(root, "held");
const coordinator = join(root, "coordinator.mjs");
const log = join(root, "coordinator.log");
const reportDirectory = resolve(import.meta.dirname, "../.openbot-build/copilot-fleet-e2e");
const clients: AgentClient[] = [];
const checks: string[] = [];
await mkdir(held);
await writeFile(
  coordinator,
  `
import { readFileSync, writeFileSync, unlinkSync, appendFileSync } from 'node:fs';
import { join } from 'node:path';
const request = JSON.parse(readFileSync(0, 'utf8'));
const path = join(process.argv[2], request.run_id);
appendFileSync(process.argv[3], JSON.stringify({op:request.op, backend:request.backend ?? null}) + '\\n');
if (request.op === 'acquire') {
  writeFileSync(path, 'held', {flag:'wx'});
  process.stdout.write(JSON.stringify({ok:true, admitted:true}));
} else {
  unlinkSync(path);
  process.stdout.write(JSON.stringify({ok:true, released:true}));
}
`,
);
const config = join(root, "fleet.json");
await writeFile(
  config,
  JSON.stringify({ journalDirectory: journal, command: [process.execPath, coordinator, held, log] }),
);
process.env.OPENBOT_FLEET_CAPACITY_CONFIG = config;
const executable = join(root, "bin", "copilot");
await installFakeAgent(executable, "conformance");
try {
  const driver = requireProviderDriver("acp");
  const cli = await runCauseEffect(driver.resolveCli());
  const configs = ["copilot", "alias"].map((id) => ({
    id,
    name: id,
    command: executable,
    args: [],
    env: [],
    usagePrompt: "/usage",
  }));
  const client = driver.createClient(cli, 10_000, { ...NO_PROVIDER_CREDENTIALS, customAgents: () => configs });
  clients.push(client);
  client.start();
  for (const id of ["copilot", "alias"]) {
    const thread = await startThread(client, { cwd: root, model: `${id}/fake-alpha` });
    const result = await runTurn(client, thread, { model: `${id}/fake-alpha`, text: "hello", timeoutMs: 10_000 });
    assert.equal(result.status, "completed");
    const deadline = Date.now() + 10_000;
    while ((await readdir(held)).length || (await readdir(journal)).length) {
      if (Date.now() > deadline) throw new Error("Copilot reservation did not release.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    checks.push(`${id} routes its real custom ACP adapter turn through Copilot admission`);
  }
  const entries = (await readFile(log, "utf8"))
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  assert.equal(entries.filter((entry) => entry.op === "acquire" && entry.backend === "copilot").length, 2);
  assert.deepEqual(await readdir(journal), []);
  await mkdir(reportDirectory, { recursive: true });
  await writeFile(join(reportDirectory, "report.json"), `${JSON.stringify({ passed: true, checks }, null, 2)}\n`);
  process.stdout.write(`PASS ${join(reportDirectory, "report.json")}\n`);
} catch (error) {
  await mkdir(reportDirectory, { recursive: true });
  await writeFile(
    join(reportDirectory, "report.json"),
    `${JSON.stringify({ passed: false, checks, error: String(error) }, null, 2)}\n`,
  );
  throw error;
} finally {
  await stopClients(clients);
  await rm(root, { recursive: true, force: true });
}
