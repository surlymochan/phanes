import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Effect, Fiber } from "effect";
import type { AgentClient } from "../src/backend/agent-client";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { PiAgentClient } from "../src/backend/pi-client";
import { getRecord, getString } from "../src/backend/protocol";
import { requireProviderDriver } from "../src/backend/provider-drivers";
import { installFakeAgent, startThread, stopClients } from "./provider-fakes/host";

const root = await mkdtemp(join(tmpdir(), "openbot-pi-fleet-e2e-"));
const journal = join(root, "journal");
const held = join(root, "held");
const config = join(root, "config.json");
const coordinator = join(root, "coordinator.mjs");
const executable = join(root, "bin", "pi");
const reportDirectory = resolve(import.meta.dirname, "../.openbot-build/pi-fleet-e2e");
const clients: AgentClient[] = [];
const checks: string[] = [];
function observePiReleaseDrain(client: AgentClient): Promise<number> {
  if (!(client instanceof PiAgentClient)) throw new Error("Expected the Pi adapter.");
  return new Promise((resolve) => client.once("fleet-release-drain", resolve));
}
const timeoutScenario = process.argv.includes("--timeout");
const interruptScenario = process.argv.includes("--interrupt");
const idleScenario = process.argv.includes("--idle");
const stopScenario = process.argv.includes("--stop");
const releaseStopScenario = process.argv.includes("--release-stop");
const acquireStopScenario = process.argv.includes("--acquire-stop");
const compactReleaseStopScenario = process.argv.includes("--compact-release-stop");
await mkdir(held);
await writeFile(
  coordinator,
  `
import { readFileSync, writeFileSync, unlinkSync, existsSync } from 'node:fs';
import { join } from 'node:path';
const request = JSON.parse(readFileSync(0, 'utf8'));
const path = join(process.argv[2], request.run_id);
if (request.op === 'acquire') {
  if (request.backend !== 'pi' || request.provider !== 'minimax-cn') process.exit(4);
  if (existsSync(join(process.argv[2], 'deny'))) {
    writeFileSync(join(process.argv[2], 'denied'), '');
    process.stdout.write(JSON.stringify({ok:true, admitted:false}));
    process.exit(0);
  }
  writeFileSync(path, 'held', { flag: 'wx' });
  if (existsSync(join(process.argv[2], 'delay-response'))) {
    writeFileSync(join(process.argv[2], 'acquire-pending'), '');
    while (!existsSync(join(process.argv[2], 'allow-response'))) await new Promise(resolve => setTimeout(resolve, 20));
  }
  if (existsSync(join(process.argv[2], 'lose-response'))) process.exit(1);
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
await writeFile(config, JSON.stringify({ journalDirectory: journal, command: [process.execPath, coordinator, held] }));
process.env.OPENBOT_FLEET_CAPACITY_CONFIG = config;
await installFakeAgent(executable, "pi-steer", {
  FAKE_LOG: join(root, "pi.log"),
  FAKE_DIR: root,
  FAKE_COMPACT_DELAY: "1",
});

try {
  const client = requireProviderDriver("deepseek").createClient(
    { executable, version: "1.4.0", source: "managed" },
    5_000,
    { apiKey: () => null, customProviders: () => [], mcpServers: () => [] },
  );
  clients.push(client);
  client.start();
  const thread = await startThread(client, { cwd: root });
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
  checks.push("a preparation failure releases admission before Pi receives a prompt");
  let completed: (() => void) | undefined;
  const done = new Promise<void>((finish) => {
    completed = finish;
  });
  client.on("notification", (notification) => {
    if (notification.method === "turn/completed" && getString(notification.params, "threadId") === thread)
      completed?.();
  });
  const response = await runCauseEffect(
    client.request(
      "turn/start",
      {
        threadId: thread,
        input: [{ type: "text", text: "work", text_elements: [] }],
      },
      (value) => value,
    ),
  );
  const turnId = getString(getRecord(response, "turn"), "id");
  assert.ok(turnId);
  assert.equal((await readdir(held)).length, 1);
  const competing = await Effect.runPromise(
    Effect.forkDetach(
      client.request(
        "turn/start",
        { threadId: thread, input: [{ type: "text", text: "competing", text_elements: [] }] },
        (value) => value,
      ),
      { startImmediately: true },
    ),
  );
  await assert.rejects(
    runCauseEffect(
      client.request("turn/start", { threadId: thread, input: [{ type: "text", text: "duplicate" }] }, (v) => v),
    ),
  );
  await Effect.runPromise(Fiber.interrupt(competing));
  assert.equal((await readdir(held)).length, 1, "interrupting a waiting start must preserve the active turn's slot");
  await runCauseEffect(
    client.request(
      "turn/steer",
      {
        threadId: thread,
        expectedTurnId: turnId,
        input: [{ type: "text", text: "finish", text_elements: [] }],
      },
      (value) => value,
    ),
  );
  await done;
  const deadline = Date.now() + 10_000;
  while ((await readdir(held)).length) {
    if (Date.now() > deadline) throw new Error("Pi reservation did not release.");
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  checks.push("Pi holds admission throughout a real adapter turn and steer");
  checks.push("interrupting another start does not retain or block the active turn's reservation");
  const compact = runCauseEffect(client.request("thread/compact/start", { threadId: thread }, (value) => value));
  const compactDeadline = Date.now() + 10_000;
  while (!(await readFile(join(root, "pi.log"), "utf8")).includes('"event":"compact"')) {
    if (Date.now() > compactDeadline) throw new Error("Pi compaction did not start.");
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.equal(client.canReleaseProcess?.(), false);
  assert.ok(client.releaseIdleThreads);
  await runCauseEffect(client.releaseIdleThreads());
  if (compactReleaseStopScenario) await writeFile(join(held, "delay-release"), "");
  await writeFile(join(root, "compact-continue"), "");
  if (compactReleaseStopScenario) {
    const deadline = Date.now() + 10_000;
    while (!(await readdir(held)).includes("release-started")) {
      if (Date.now() > deadline) throw new Error("Pi compaction release did not start.");
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    const drain = observePiReleaseDrain(client);
    let stopped = false;
    const stopping = stopClients([client]).then(() => {
      stopped = true;
    });
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    const draining = await Promise.race([
      drain,
      new Promise<never>((_, reject) => {
        watchdog = setTimeout(() => reject(new Error("Pi shutdown did not reach release drain.")), 10_000);
      }),
    ]).finally(() => clearTimeout(watchdog));
    assert.equal(draining, 1, "shutdown must track the in-flight compaction release");
    assert.equal(stopped, false, "shutdown must wait for the compaction release");
    await writeFile(join(held, "release-continue"), "");
    await compact;
    await stopping;
    await rm(join(held, "delay-release"));
    await rm(join(held, "release-started"));
    await rm(join(held, "release-continue"));
    checks.push("Pi shutdown waits for the completed compaction release");
  } else {
    await compact;
  }
  assert.deepEqual(await readdir(held), []);
  assert.deepEqual(await readdir(journal), []);
  checks.push("Pi compaction releases admission after the RPC acknowledges completion");
  if (compactReleaseStopScenario) {
    // The client was stopped by the compaction release scenario above.
  } else if (acquireStopScenario) {
    await writeFile(join(held, "delay-response"), "");
    await writeFile(join(held, "delay-release"), "");
    const pending = runCauseEffect(
      client.request("turn/start", { threadId: thread, input: [{ type: "text", text: "unsent" }] }, (v) => v),
    );
    pending.catch(() => {});
    const deadline = Date.now() + 10_000;
    while (!(await readdir(held)).includes("acquire-pending")) {
      if (Date.now() > deadline) throw new Error("Pi acquisition did not start.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    let stopped = false;
    const stopping = stopClients([client]).then(() => {
      stopped = true;
    });
    while (client.running) {
      if (Date.now() > deadline) throw new Error("Pi shutdown did not start.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    await writeFile(join(held, "allow-response"), "");
    while (!(await readdir(held)).includes("release-started")) {
      if (Date.now() > deadline) throw new Error("Pi acquisition release did not start.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    assert.equal(stopped, false, "shutdown must wait for an in-flight Pi acquisition");
    await writeFile(join(held, "release-continue"), "");
    await stopping;
    await assert.rejects(pending);
    assert.deepEqual(await readdir(journal), []);
    checks.push("Pi shutdown waits for an in-flight acquisition to release");
  } else if (releaseStopScenario) {
    const completed = new Promise<void>((resolve) => {
      client.on("notification", (event) => {
        if (event.method === "turn/completed" && getString(event.params, "threadId") === thread) resolve();
      });
    });
    const started = await runCauseEffect(
      client.request(
        "turn/start",
        {
          threadId: thread,
          input: [{ type: "text", text: "work", text_elements: [] }],
        },
        (value) => value,
      ),
    );
    const turnId = getString(getRecord(started, "turn"), "id");
    assert.ok(turnId);
    await writeFile(join(held, "delay-release"), "");
    await runCauseEffect(
      client.request(
        "turn/steer",
        {
          threadId: thread,
          expectedTurnId: turnId,
          input: [{ type: "text", text: "finish", text_elements: [] }],
        },
        (value) => value,
      ),
    );
    await completed;
    const deadline = Date.now() + 10_000;
    while (!(await readdir(held)).includes("release-started")) {
      if (Date.now() > deadline) throw new Error("Pi release did not start.");
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    let stopped = false;
    const stopping = stopClients([client]).then(() => {
      stopped = true;
    });
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.equal(stopped, false, "shutdown must wait for the blocked release");
    await writeFile(join(held, "release-continue"), "");
    await stopping;
    assert.deepEqual(await readdir(journal), []);
    checks.push("Pi shutdown waits for the completed turn's release");
  } else if (stopScenario) {
    await writeFile(join(held, "deny"), "");
    const waiting = runCauseEffect(
      client.request(
        "turn/start",
        {
          threadId: thread,
          input: [{ type: "text", text: "stopped-before-send", text_elements: [] }],
        },
        (value) => value,
      ),
    );
    waiting.catch(() => {});
    const deadline = Date.now() + 10_000;
    while (!(await readdir(held)).includes("denied")) {
      if (Date.now() > deadline) throw new Error("Pi admission did not wait.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    await assert.rejects(
      runCauseEffect(
        client.request(
          "turn/start",
          { threadId: thread, input: [{ type: "text", text: "duplicate" }] },
          (value) => value,
        ),
      ),
    );
    await runCauseEffect(client.request("turn/interrupt", { threadId: thread }, (value) => value));
    await assert.rejects(waiting);
    assert.deepEqual(await readdir(journal), []);
    const log = await readFile(join(root, "pi.log"), "utf8");
    assert.equal(log.includes("stopped-before-send"), false);
    checks.push("stopping a waiting Pi turn prevents its prompt from being sent");
  } else if (idleScenario) {
    await writeFile(join(held, "deny"), "");
    const next = runCauseEffect(
      client.request(
        "turn/start",
        {
          threadId: thread,
          input: [{ type: "text", text: "work", text_elements: [] }],
        },
        (value) => value,
      ),
    );
    const deadline = Date.now() + 10_000;
    while (!(await readdir(held)).includes("denied")) {
      if (Date.now() > deadline) throw new Error("Pi admission did not wait.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    assert.ok(client.releaseIdleThreads);
    await runCauseEffect(client.releaseIdleThreads());
    await rm(join(held, "deny"));
    const reopened = await next;
    const newTurn = getString(getRecord(reopened, "turn"), "id");
    assert.ok(newTurn);
    const resumed = new Promise<void>((resolve) =>
      client.on("notification", (event) => {
        if (event.method === "turn/completed" && getString(event.params, "threadId") === thread) resolve();
      }),
    );
    await runCauseEffect(
      client.request(
        "turn/steer",
        {
          threadId: thread,
          expectedTurnId: newTurn,
          input: [{ type: "text", text: "finish", text_elements: [] }],
        },
        (value) => value,
      ),
    );
    await resumed;
    while ((await readdir(journal)).length) {
      if (Date.now() > deadline) throw new Error("Pi reopened turn did not release admission.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    checks.push("a thread released while waiting is reopened after admission");
  } else if (interruptScenario) {
    await writeFile(join(held, "delay-response"), "");
    const fiber = Effect.runFork(
      client.request(
        "turn/start",
        {
          threadId: thread,
          input: [{ type: "text", text: "interrupted-before-send", text_elements: [] }],
        },
        (value) => value,
      ),
    );
    const deadline = Date.now() + 10_000;
    while (!(await readdir(held)).includes("acquire-pending")) {
      if (Date.now() > deadline) throw new Error("Pi acquisition did not start.");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    await assert.rejects(
      Effect.runPromise(client.request("thread/compact/start", { threadId: thread }, (value) => value)),
    );
    checks.push("pending Pi start rejects overlapping compaction without changing its reservation");
    const interruption = Effect.runPromise(Fiber.interrupt(fiber));
    await writeFile(join(held, "allow-response"), "");
    await interruption;
    assert.deepEqual(await readdir(journal), []);
    const entries = await readdir(held);
    assert.deepEqual(entries.sort(), ["acquire-pending", "allow-response", "delay-response"]);
    const log = await readFile(join(root, "pi.log"), "utf8");
    assert.equal(log.includes("interrupted-before-send"), false);
    checks.push("interrupted acquisition releases its unsent reservation");
  } else if (timeoutScenario) {
    const timedOut = new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Pi acknowledgement timeout was not observed.")), 10_000);
      client.on("notification", (event) => {
        if (event.method === "turn/completed" && getString(event.params, "threadId") === thread) {
          clearTimeout(timer);
          resolve();
        }
      });
    });
    await runCauseEffect(
      client.request(
        "turn/start",
        {
          threadId: thread,
          input: [{ type: "text", text: "no-ack", text_elements: [] }],
        },
        (value) => value,
      ),
    );
    await timedOut;
    assert.equal((await readdir(held)).length, 1);
    assert.equal((await readdir(journal)).length, 1);
    checks.push("a timed-out Pi acknowledgement retains admission while the provider may still run");
  } else {
    await writeFile(join(held, "lose-response"), "");
    await assert.rejects(
      runCauseEffect(
        client.request(
          "turn/start",
          {
            threadId: thread,
            input: [{ type: "text", text: "not-sent", text_elements: [] }],
          },
          (value) => value,
        ),
      ),
    );
    assert.equal((await readdir(journal)).length, 1);
    const log = await readFile(join(root, "pi.log"), "utf8");
    assert.equal(log.includes("not-sent"), false);
    checks.push("a lost admission response retains its journal and never sends the Pi prompt");
  }
  await mkdir(reportDirectory, { recursive: true });
  const report = compactReleaseStopScenario
    ? "compact-release-stop-report.json"
    : acquireStopScenario
      ? "acquire-stop-report.json"
      : releaseStopScenario
        ? "release-stop-report.json"
        : stopScenario
          ? "stop-report.json"
          : idleScenario
            ? "idle-report.json"
            : interruptScenario
              ? "interrupt-report.json"
              : timeoutScenario
                ? "timeout-report.json"
                : "report.json";
  await writeFile(join(reportDirectory, report), `${JSON.stringify({ passed: true, checks }, null, 2)}\n`);
  process.stdout.write(`PASS ${join(reportDirectory, report)}\n`);
} catch (error) {
  await mkdir(reportDirectory, { recursive: true });
  await writeFile(
    join(reportDirectory, "report.json"),
    `${JSON.stringify({ passed: false, checks, error: String(error) }, null, 2)}\n`,
  );
  throw error;
} finally {
  await writeFile(join(root, "compact-continue"), "").catch(() => {});
  await writeFile(join(held, "allow-response"), "").catch(() => {});
  await writeFile(join(held, "release-continue"), "").catch(() => {});
  await stopClients(clients);
  await rm(root, { recursive: true, force: true });
}
