import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { NO_PROVIDER_CREDENTIALS, requireProviderDriver } from "../src/backend/provider-drivers";
import { installFakeAgent, runTurn, startThread, stopClients } from "./provider-fakes/host";

const root = await mkdtemp(join(tmpdir(), "openbot-acp-stream-loss-"));
const held = join(root, "held");
const journal = join(root, "journal");
const coordinator = join(root, "coordinator.mjs");
const reportDirectory = resolve(import.meta.dirname, "../.openbot-build/acp-stream-loss-e2e");
const checks: string[] = [];
await mkdir(held);
await writeFile(
  coordinator,
  `
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
const request = JSON.parse(readFileSync(0, 'utf8'));
const path = join(process.argv[2], request.run_id);
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
await writeFile(config, JSON.stringify({ journalDirectory: journal, command: [process.execPath, coordinator, held] }));
process.env.OPENBOT_FLEET_CAPACITY_CONFIG = config;
const executable = join(root, "bin", "agent");
await installFakeAgent(executable, "conformance", { FAKE_MISSING: "cursor" });
const client = requireProviderDriver("cursor").createClient(
  { executable, version: "1.4.1", source: "managed" },
  10_000,
  NO_PROVIDER_CREDENTIALS,
);
client.start();
try {
  const thread = await startThread(client, { cwd: root });
  const rejected = await runTurn(client, thread, { text: "reject-prompt", timeoutMs: 10_000 });
  assert.equal(rejected.status, "failed");
  const deadline = Date.now() + 10_000;
  while ((await readdir(held)).length || (await readdir(journal)).length) {
    if (Date.now() > deadline) throw new Error("Explicit prompt rejection did not release its slot.");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.deepEqual(await readdir(journal), []);
  checks.push("explicit rejection releases its reservation");
  const result = await runTurn(client, thread, { text: "drop-stream", timeoutMs: 10_000 });
  assert.equal(result.status, "failed");
  assert.equal(client.running, true, "fake provider remains alive after its output stream closes");
  // stop waits for all completion releases, so the next assertions cannot race their SSH response.
  await stopClients([client]);
  assert.equal((await readdir(held)).length, 1, "coordinator must retain the uncertain provider slot");
  assert.equal((await readdir(journal)).length, 1, "the uncertain reservation must survive a restart");
  checks.push("stream loss retains the reservation while the provider is alive");
  await mkdir(reportDirectory, { recursive: true });
  await writeFile(join(reportDirectory, "report.json"), `${JSON.stringify({ passed: true, checks }, null, 2)}\n`);
  process.stdout.write("PASS ACP stream loss retains the reservation while the provider is alive\n");
} catch (error) {
  await mkdir(reportDirectory, { recursive: true });
  await writeFile(
    join(reportDirectory, "report.json"),
    `${JSON.stringify({ passed: false, checks, error: String(error) }, null, 2)}\n`,
  );
  throw error;
} finally {
  await stopClients([client]);
  await rm(root, { recursive: true, force: true });
}
