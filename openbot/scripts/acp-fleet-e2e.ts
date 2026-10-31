import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Effect, Fiber } from "effect";
import type { AgentClient } from "../src/backend/agent-client";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { getRecord, getString } from "../src/backend/protocol";
import { NO_PROVIDER_CREDENTIALS, requireProviderDriver } from "../src/backend/provider-drivers";
import { installFakeAgent, runTurn, startThread, stopClients } from "./provider-fakes/host";

const root = await mkdtemp(join(tmpdir(), "openbot-acp-fleet-e2e-"));
const journal = join(root, "journal");
const held = join(root, "held");
const coordinator = join(root, "coordinator.mjs");
const log = join(root, "coordinator.log");
const reportDirectory = resolve(import.meta.dirname, "../.openbot-build/acp-fleet-e2e");
const clients: AgentClient[] = [];
const checks: string[] = [];
let agyClient: AgentClient | undefined;
let agyThread: string | undefined;
let cursorClient: AgentClient | undefined;
let cursorThread: string | undefined;
await mkdir(held);
await writeFile(
  coordinator,
  `
import { readFileSync, writeFileSync, unlinkSync, appendFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
const request = JSON.parse(readFileSync(0, 'utf8'));
const path = join(process.argv[2], request.run_id);
appendFileSync(process.argv[3], JSON.stringify({op:request.op, backend:request.backend ?? null}) + '\\n');
if (request.op === 'acquire') {
  if (existsSync(join(process.argv[2], 'deny'))) {
    writeFileSync(join(process.argv[2], 'denied'), '');
    process.stdout.write(JSON.stringify({ok:true, admitted:false}));
    process.exit(0);
  }
  writeFileSync(path, 'held', {flag:'wx'});
  if (existsSync(join(process.argv[2], 'delay-acquire'))) {
    writeFileSync(join(process.argv[2], 'acquire-started'), '');
    while (!existsSync(join(process.argv[2], 'acquire-continue'))) await new Promise(resolve => setTimeout(resolve, 20));
  }
  process.stdout.write(JSON.stringify({ok:true, admitted:true}));
} else {
  unlinkSync(path);
  if (existsSync(join(process.argv[2], 'delay-release'))) {
    writeFileSync(join(process.argv[2], 'release-started'), '');
    while (!existsSync(join(process.argv[2], 'release-continue'))) await new Promise(resolve => setTimeout(resolve, 20));
  }
  process.stdout.write(JSON.stringify({ok:true, released:true}));
}
`,
);
const config = join(root, "config.json");
await writeFile(
  config,
  JSON.stringify({ journalDirectory: journal, command: [process.execPath, coordinator, held, log] }),
);
process.env.OPENBOT_FLEET_CAPACITY_CONFIG = config;
try {
  for (const provider of ["cursor", "antigravity"] as const) {
    const executable = join(root, provider, "agent");
    await installFakeAgent(executable, "conformance", {
      FAKE_MISSING: provider,
      ...(provider === "antigravity" ? { FAKE_IGNORE_CANCEL: "1" } : {}),
    });
    const client = requireProviderDriver(provider).createClient(
      { executable, version: "1.4.1", source: "managed" },
      10_000,
      NO_PROVIDER_CREDENTIALS,
    );
    clients.push(client);
    client.start();
    const thread = await startThread(client, { cwd: root });
    if (provider === "antigravity") {
      agyClient = client;
      agyThread = thread;
    }
    if (provider === "cursor") {
      cursorClient = client;
      cursorThread = thread;
    }
    await assert.rejects(
      runCauseEffect(
        client.request(
          "turn/start",
          {
            threadId: thread,
            input: [{ type: "localImage", path: join(root, "missing.png") }],
          },
          (value) => value,
        ),
      ),
    );
    assert.deepEqual(await readdir(held), []);
    assert.deepEqual(await readdir(journal), []);
    const result = await runTurn(client, thread, { text: "hello", timeoutMs: 10_000 });
    assert.equal(result.status, "completed");
    const deadline = Date.now() + 10_000;
    while ((await readdir(held)).length) {
      if (Date.now() > deadline) throw new Error(`${provider} reservation did not release.`);
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    assert.ok(client.readUsageText);
    const answer = await runCauseEffect(client.readUsageText("fake-alpha", "private usage probe"));
    assert.ok(answer);
    assert.deepEqual(await readdir(held), []);
    await assert.rejects(runCauseEffect(client.readUsageText("fake-alpha", "reject-prompt")));
    assert.deepEqual(await readdir(held), []);
    assert.deepEqual(await readdir(journal), []);
    assert.ok(await runCauseEffect(client.readUsageText("fake-alpha", "private usage after rejection")));
    const entries = (await readFile(log, "utf8"))
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    const backend = provider === "cursor" ? "cursor" : "agy";
    assert.equal(entries.filter((entry) => entry.op === "acquire" && entry.backend === backend).length, 5);
    checks.push(`${provider} holds admission for both a turn and a private usage prompt`);
  }
  {
    const client = requireProviderDriver("cursor").createClient(
      { executable: join(root, "cursor", "agent"), version: "1.4.1", source: "managed" },
      10_000,
      NO_PROVIDER_CREDENTIALS,
    );
    clients.push(client);
    client.start();
    const thread = await startThread(client, { cwd: root });
    await writeFile(join(held, "delay-acquire"), "");
    const fiber = Effect.runFork(
      client.request(
        "turn/start",
        { threadId: thread, input: [{ type: "text", text: "interrupted before send" }] },
        (v) => v,
      ),
    );
    const deadline = Date.now() + 10_000;
    while (!(await readdir(held)).includes("acquire-started")) {
      if (Date.now() > deadline) throw new Error("ACP acquisition did not start.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const interruption = Effect.runPromise(Fiber.interrupt(fiber));
    await writeFile(join(held, "acquire-continue"), "");
    await interruption;
    assert.deepEqual(await readdir(journal), []);
    for (const marker of ["delay-acquire", "acquire-started", "acquire-continue"]) await rm(join(held, marker));
    assert.deepEqual(await readdir(held), []);
    checks.push("interrupted ACP admission releases the unsent reservation");
  }
  {
    const client = requireProviderDriver("cursor").createClient(
      { executable: join(root, "cursor", "agent"), version: "1.4.1", source: "managed" },
      10_000,
      NO_PROVIDER_CREDENTIALS,
    );
    clients.push(client);
    client.start();
    const thread = await startThread(client, { cwd: root });
    await writeFile(join(held, "delay-acquire"), "");
    await writeFile(join(held, "delay-release"), "");
    const pending = runCauseEffect(
      client.request("turn/start", { threadId: thread, input: [{ type: "text", text: "unsent" }] }, (v) => v),
    );
    pending.catch(() => {});
    const deadline = Date.now() + 10_000;
    while (!(await readdir(held)).includes("acquire-started")) {
      if (Date.now() > deadline) throw new Error("Admission did not start.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    let stopped = false;
    const stopping = stopClients([client]).then(() => {
      stopped = true;
    });
    while (client.running) {
      if (Date.now() > deadline) throw new Error("ACP shutdown did not start.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    await writeFile(join(held, "acquire-continue"), "");
    while (!(await readdir(held)).includes("release-started")) {
      if (Date.now() > deadline) throw new Error("ACP acquisition release did not start.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    assert.equal(stopped, false, "shutdown must wait for an in-flight acquisition");
    await writeFile(join(held, "release-continue"), "");
    await stopping;
    await assert.rejects(pending);
    assert.deepEqual(await readdir(journal), []);
    for (const marker of [
      "delay-acquire",
      "acquire-started",
      "acquire-continue",
      "delay-release",
      "release-started",
      "release-continue",
    ])
      await rm(join(held, marker));
    assert.deepEqual(await readdir(held), []);
    checks.push("shutdown waits for a pending acquisition to release");
  }
  for (const prompt of ["private usage before stop", "reject-prompt"]) {
    const client = requireProviderDriver("cursor").createClient(
      { executable: join(root, "cursor", "agent"), version: "1.4.1", source: "managed" },
      10_000,
      NO_PROVIDER_CREDENTIALS,
    );
    clients.push(client);
    client.start();
    await startThread(client, { cwd: root });
    assert.ok(client.readUsageText);
    await writeFile(join(held, "delay-release"), "");
    const usage = runCauseEffect(client.readUsageText("fake-alpha", prompt));
    usage.catch(() => {});
    const deadline = Date.now() + 10_000;
    while (!(await readdir(held)).includes("release-started")) {
      if (Date.now() > deadline) throw new Error("Private prompt release did not start.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    let stopped = false;
    const stopping = stopClients([client]).then(() => {
      stopped = true;
    });
    while (client.running) {
      if (Date.now() > deadline) throw new Error("ACP shutdown did not start.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    assert.equal(stopped, false, "shutdown must wait for a private prompt release");
    await writeFile(join(held, "release-continue"), "");
    await stopping;
    if (prompt === "reject-prompt") await assert.rejects(usage);
    else assert.ok(await usage);
    assert.deepEqual(await readdir(journal), []);
    for (const marker of ["delay-release", "release-started", "release-continue"]) await rm(join(held, marker));
  }
  checks.push("shutdown waits for private prompt releases after success and rejection");
  assert.ok(cursorClient && cursorThread);
  const cursorEnded = new Promise<void>((resolve) => {
    cursorClient?.on("notification", (event) => {
      if (event.method === "turn/completed" && getString(event.params, "threadId") === cursorThread) resolve();
    });
  });
  const active = await runCauseEffect(
    cursorClient.request("turn/start", { threadId: cursorThread, input: [{ type: "text", text: "wait" }] }, (v) => v),
  );
  const activeId = getString(getRecord(active, "turn"), "id");
  assert.ok(activeId);
  const competing = await Effect.runPromise(
    Effect.forkDetach(
      cursorClient.request(
        "turn/start",
        { threadId: cursorThread, input: [{ type: "text", text: "competing" }] },
        (v) => v,
      ),
      { startImmediately: true },
    ),
  );
  await assert.rejects(
    runCauseEffect(
      cursorClient.request(
        "turn/start",
        { threadId: cursorThread, input: [{ type: "text", text: "duplicate" }] },
        (v) => v,
      ),
    ),
  );
  await Effect.runPromise(Fiber.interrupt(competing));
  await runCauseEffect(cursorClient.request("turn/interrupt", { threadId: cursorThread, turnId: activeId }, (v) => v));
  await cursorEnded;
  const cursorDeadline = Date.now() + 10_000;
  while ((await readdir(journal)).length) {
    if (Date.now() > cursorDeadline) throw new Error("Active Cursor reservation was not released.");
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.equal(
    (await runTurn(cursorClient, cursorThread, { text: "after competing request", timeoutMs: 10_000 })).status,
    "completed",
  );
  while ((await readdir(journal)).length) {
    if (Date.now() > cursorDeadline) throw new Error("Follow-up Cursor reservation was not released.");
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  checks.push("interrupting another request leaves the active Cursor reservation usable");
  assert.ok(agyClient && agyThread);
  await writeFile(join(held, "deny"), "");
  const pending = runCauseEffect(
    agyClient.request(
      "turn/start",
      {
        threadId: agyThread,
        input: [{ type: "text", text: "cancel-before-send", text_elements: [] }],
      },
      (value) => value,
    ),
  );
  pending.catch(() => {});
  const waitingDeadline = Date.now() + 5_000;
  while (!(await readdir(held)).includes("denied")) {
    if (Date.now() > waitingDeadline) throw new Error("ACP admission did not wait.");
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  await assert.rejects(
    runCauseEffect(
      agyClient.request("turn/start", { threadId: agyThread, input: [{ type: "text", text: "duplicate" }] }, (v) => v),
    ),
  );
  await runCauseEffect(agyClient.request("turn/interrupt", { threadId: agyThread }, (value) => value));
  await assert.rejects(pending);
  assert.deepEqual(await readdir(journal), []);
  await rm(join(held, "deny"));
  await rm(join(held, "denied"));
  checks.push("stopping a pending ACP turn cancels admission before sending");
  const ended = new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Antigravity cancellation did not finish locally.")), 10_000);
    agyClient?.on("notification", (event) => {
      if (event.method === "turn/completed" && getString(event.params, "threadId") === agyThread) {
        clearTimeout(timer);
        resolve();
      }
    });
  });
  const started = await runCauseEffect(
    agyClient.request(
      "turn/start",
      {
        threadId: agyThread,
        input: [{ type: "text", text: "wait", text_elements: [] }],
      },
      (value) => value,
    ),
  );
  const turnId = getString(getRecord(started, "turn"), "id");
  assert.ok(turnId);
  await runCauseEffect(agyClient.request("turn/interrupt", { threadId: agyThread, turnId }, (value) => value));
  await ended;
  assert.equal((await readdir(held)).length, 1);
  assert.equal((await readdir(journal)).length, 1);
  checks.push("a locally abandoned Antigravity prompt retains its reservation");
  assert.ok(cursorClient && cursorThread);
  await writeFile(join(held, "delay-release"), "");
  const cursorAfter = await runTurn(cursorClient, cursorThread, { text: "still available", timeoutMs: 10_000 });
  assert.equal(cursorAfter.status, "completed");
  const releaseDeadline = Date.now() + 10_000;
  while (!(await readdir(held)).includes("release-started")) {
    if (Date.now() > releaseDeadline) throw new Error("Cursor release did not start.");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  let stopped = false;
  const stopping = stopClients([cursorClient]).then(() => {
    stopped = true;
  });
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(stopped, false, "shutdown must wait for the blocked release");
  await writeFile(join(held, "release-continue"), "");
  await stopping;
  const remaining = await readdir(journal);
  assert.equal(remaining.length, 1);
  const remainingFile = remaining[0];
  assert.ok(remainingFile);
  const retained = JSON.parse(await readFile(join(journal, remainingFile), "utf8"));
  assert.equal(retained.backend, "agy");
  checks.push("stopping waits for a completed Cursor release to finish");
  checks.push("an uncertain Antigravity turn does not block Cursor");
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
  await writeFile(join(held, "acquire-continue"), "").catch(() => {});
  await writeFile(join(held, "release-continue"), "").catch(() => {});
  await stopClients(clients);
  await rm(root, { recursive: true, force: true });
}
