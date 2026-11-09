// Runs only test-owned hosts and a local fake provider. No signed-in provider or desktop is used.
// Run: bun test ./scripts/agent-task-restart-recovery-e2e.ts
import assert from "node:assert/strict";
import { type ChildProcess, spawn } from "node:child_process";
import { once } from "node:events";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { type AgentTask, isAgentTask } from "@volto/contracts/ipc";

import { z } from "zod";

const artifact = resolve(import.meta.dirname, "../.volto-build/agent-task-restart-recovery-e2e");
const marker = "restart-recovery-input";
type Outcome = "completed" | "failed" | "unknown";
interface Evidence {
  agentId: string;
  taskId: string;
  deliveryId: string;
  turnId: string;
  status: string;
  task: AgentTask;
  activeTurnId: string | null;
}

const evidenceSchema = z.object({
  agentId: z.string(),
  taskId: z.string(),
  deliveryId: z.string(),
  turnId: z.string(),
  status: z.string(),
  task: z.custom<AgentTask>(isAgentTask),
  activeTurnId: z.string().nullable(),
});

// The fake persists accepted inputs before replying. A fresh provider process reads the same file.
// It never sends the task's terminal notification, so only boot recovery can settle that delivery.
const fake = String.raw`#!/usr/bin/env node
const fs = require("node:fs");
const { randomUUID } = require("node:crypto");
const { createInterface } = require("node:readline");
if (process.argv.includes("--version")) { console.log("codex-cli 0.160.1"); process.exit(0); }
const file = process.env.RECOVERY_DATA + "/provider.json";
const state = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : { threads: {}, inputs: [], historyReads: 0 };
const save = () => fs.writeFileSync(file, JSON.stringify(state));
const write = (value) => process.stdout.write(JSON.stringify(value) + "\n");
process.stdout.on("error", () => process.exit(0));
const lines = createInterface({ input: process.stdin });
lines.on("close", () => process.exit(0));
lines.on("line", (line) => {
  const m = JSON.parse(line), p = m.params || {};
  if (m.id === undefined) return;
  const reply = (result) => write({ id: m.id, result });
  switch (m.method) {
    case "initialize": return reply({});
    case "account/read": return reply({ account: { type: "chatgpt", email: "fixture@example.com" } });
    case "account/rateLimits/read": return reply({ rateLimits: { limitId: "codex", primary: null, secondary: null }, rateLimitsByLimitId: null });
    case "model/list": return reply({ data: [{ model: "gpt-5.5", displayName: "Fixture" }] });
    case "plugin/list": return reply({ marketplaces: [] });
    case "config/read": return reply({ config: { mcp_servers: {} }, layers: [] });
    case "config/batchWrite": return reply({});
    case "thread/start": {
      const id = randomUUID(); state.threads[id] = []; save();
      return reply({ thread: { id, turns: [] } });
    }
    case "thread/resume": return reply({ thread: { id: p.threadId, turns: [] } });
    case "thread/unsubscribe": return reply({ status: "Unsubscribed" });
    case "thread/read": return reply({ thread: { id: p.threadId, turns: [] } });
    case "thread/turns/list": {
      state.historyReads++; save();
      const data = (state.threads[p.threadId] || []).flatMap(t => {
        if (t.held && process.env.RECOVERY_OUTCOME === "unknown") return [];
        return [{ id: t.id, status: t.held ? process.env.RECOVERY_OUTCOME : "completed" }];
      });
      return reply({ data, nextCursor: null });
    }
    case "thread/items/list": return reply({ data: [], nextCursor: null });
    case "turn/start": {
      const held = JSON.stringify(p.input).includes("restart-recovery-input");
      const turn = { id: randomUUID(), held };
      state.inputs.push({ threadId: p.threadId, held });
      state.threads[p.threadId].push(turn); save();
      reply({ turn: { id: turn.id, status: "inProgress", items: [] } });
      write({ method: "turn/started", params: { threadId: p.threadId, turn: { id: turn.id } } });
      if (!held) {
        const item = { type: "agentMessage", id: randomUUID(), text: "Ready.", phase: "final_answer" };
        write({ method: "item/completed", params: { threadId: p.threadId, turnId: turn.id, item } });
        write({ method: "turn/completed", params: { threadId: p.threadId, turn: { id: turn.id, status: "completed" } } });
      }
      return;
    }
    default: return write({ id: m.id, error: { code: -32601, message: "Unsupported fixture method: " + m.method } });
  }
});
`;

async function waitFor<T>(label: string, read: () => T | undefined, timeout = 25_000): Promise<T> {
  const deadline = Date.now() + timeout;
  for (;;) {
    const value = read();
    if (value !== undefined) return value;
    if (Date.now() >= deadline) throw new Error(`Timed out waiting for ${label}.`);
    await new Promise((done) => setTimeout(done, 20));
  }
}

async function host(): Promise<void> {
  const root = process.env.RECOVERY_DATA;
  assert.ok(root);
  // Set all profile paths before loading the service. No real account or application data is used.
  process.env.HOME = join(root, "home");
  process.env.CODEX_HOME = join(root, "home", ".codex");
  process.env.VOLTO_CODEX_PATH = join(root, "provider.cjs");
  await mkdir(process.env.HOME, { recursive: true });
  const { AGENT_PROVIDERS } = await import("@volto/contracts/ipc");
  const { Effect } = await import("effect");
  const { AgentService } = await import("../src/backend/agent-service");
  const { AgentStore } = await import("../src/backend/agent-store");
  const { MailboxStore } = await import("../src/backend/mailbox-store");
  const { VoltoDatabase } = await import("../src/backend/volto-database");
  const { runCauseEffect: run } = await import("../src/backend/effect-boundary");
  const unused = () => Effect.die(new Error("Unexpected browser operation."));
  const profile = join(root, "profile");
  const database = new VoltoDatabase(profile);
  const store = new AgentStore(profile, process.env.HOME, database);
  const mailbox = new MailboxStore(profile, store.sharedRoot, database);
  const service = new AgentService({
    store,
    mailbox,
    preferredProvider: "codex",
    offProviders: AGENT_PROVIDERS.filter((p) => p !== "codex"),
    browser: {
      listTabs: () => [],
      beginTakeover: unused,
      endTakeover: () => {},
      userControlTabFor: () => null,
      awaitUserControlRelease: unused,
      resolveUploadTarget: unused,
      handleDynamicTool: unused,
      onChanged: () => () => {},
      onControlChanged: () => () => {},
      onDocumentChanged: () => () => {},
      clearControls: () => {},
      endControl: () => {},
      close: () => Effect.void,
      clearAgentSession: () => Effect.void,
    },
  });
  const publishedTasks = new Map<string, AgentTask>();
  service.on("event", (event) => {
    if (event.type === "agent-tasks-changed") {
      for (const task of event.snapshot.tasks) publishedTasks.set(task.id, task);
    }
  });
  try {
    await run(service.initialize());
    if (process.env.RECOVERY_PHASE === "before") {
      const agent = await run(
        service.createAgent({
          name: "Restart fixture",
          description: "Local restart test",
          avatarSeed: "restart",
          avatarHue: null,
          initialMessage: "Prepare.",
        }),
      );
      await waitFor("initial turn", () =>
        service.listQueue(agent.id).deliveries.length > 0 &&
        service.listQueue(agent.id).deliveries.every((d) => d.status === "completed")
          ? true
          : undefined,
      );
      const task = await run(service.startTask({ agentId: agent.id, text: marker, clientMessageId: "fixture-input" }));
      const evidence = await waitFor("running task delivery", () => {
        const delivery = mailbox
          .unresolvedDeliveries()
          .find(({ delivery: d }) => mailbox.taskIdForDelivery(d.id) === task.id)?.delivery;
        const current = service.listTasks(agent.id).tasks.find((t) => t.id === task.id);
        if (delivery?.status !== "running" || !delivery.turnId || current?.state !== "running") return;
        return {
          agentId: agent.id,
          taskId: task.id,
          deliveryId: delivery.id,
          turnId: delivery.turnId,
          status: delivery.status,
          task: current,
          activeTurnId: service.readTask(agent.id, task.id).activeTurnId,
        };
      });
      await writeFile(join(root, "identity.json"), JSON.stringify(evidence));
      process.send?.(evidence);
      // The parent kills this process without service.stop(), exercising real crash recovery.
      await new Promise<void>(() => {});
    } else {
      const before = evidenceSchema.parse(JSON.parse(await readFile(join(root, "identity.json"), "utf8")));
      const expected = process.env.RECOVERY_OUTCOME === "unknown" ? "interrupted" : process.env.RECOVERY_OUTCOME;
      const evidence = await waitFor("recovered task", () => {
        const delivery = mailbox.getDelivery(before.deliveryId)?.delivery;
        const task = service.listTasks(before.agentId).tasks.find((t) => t.id === before.taskId);
        if (!delivery || delivery.status !== expected || !task || task.state === "running" || task.state === "queued")
          return;
        return {
          ...before,
          status: delivery.status,
          task,
          activeTurnId: service.readTask(before.agentId, before.taskId).activeTurnId,
        };
      });
      assert.deepEqual(
        publishedTasks.get(before.taskId),
        evidence.task,
        "The pushed UI task snapshot must match recovery.",
      );
      process.send?.(evidence);
    }
  } finally {
    await run(service.stop());
    database.close();
    process.disconnect?.();
  }
}

function startHost(root: string, phase: string, outcome: Outcome) {
  const child = spawn(process.execPath, [import.meta.filename], {
    env: { ...process.env, RECOVERY_DATA: root, RECOVERY_PHASE: phase, RECOVERY_OUTCOME: outcome },
    stdio: ["ignore", "pipe", "pipe", "ipc"],
  });
  let output = "";
  child.stdout?.on("data", (bytes) => {
    output += String(bytes);
  });
  child.stderr?.on("data", (bytes) => {
    output += String(bytes);
  });
  const result = new Promise<Evidence>((accept, reject) => {
    const timeout = setTimeout(() => reject(new Error("Host did not report recovery.")), 40_000);
    child.once("message", (message) => {
      clearTimeout(timeout);
      const parsed = evidenceSchema.safeParse(message);
      if (parsed.success) accept(parsed.data);
      else reject(new Error("Invalid host evidence."));
    });
    child.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.once("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`Host exited (${code}): ${output}`));
    });
  });
  return { child, result };
}

async function stopHost(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const ended = once(child, "exit");
  child.kill("SIGKILL");
  await ended;
}

async function scenario(outcome: Outcome) {
  await mkdir(artifact, { recursive: true });
  const root = await mkdtemp(join(artifact, "profile-"));
  const children: ChildProcess[] = [];
  try {
    await writeFile(join(root, "provider.cjs"), fake, { mode: 0o755 });
    const first = startHost(root, "before", outcome);
    children.push(first.child);
    const before = await first.result;
    assert.equal(before.status, "running");
    assert.equal(before.task.state, "running");
    assert.equal(before.activeTurnId, before.turnId);
    await stopHost(first.child);
    const second = startHost(root, "after", outcome);
    children.push(second.child);
    const ended = once(second.child, "exit", { signal: AbortSignal.timeout(40_000) });
    const after = await second.result;
    const [code] = await ended;
    assert.equal(code, 0);
    assert.notEqual(first.child.pid, second.child.pid, "A new host must read the persisted state.");
    const terminal = outcome === "unknown" ? "interrupted" : outcome;
    assert.equal(after.status, terminal);
    assert.equal(after.task.outcome, terminal);
    assert.equal(after.task.state, "idle");
    if (terminal === "completed") assert.equal(after.task.completion, 100);
    if (terminal === "failed") assert.ok(after.task.error, "The failed task must explain its recovered failure.");
    else assert.equal(after.task.error, null);
    assert.equal(after.task.activeTurnId, null);
    assert.equal(after.activeTurnId, null);
    assert.equal(after.task.queuedMessages.length, 0);
    const provider = z
      .object({ inputs: z.array(z.object({ held: z.boolean() })), historyReads: z.number() })
      .parse(JSON.parse(await readFile(join(root, "provider.json"), "utf8")));
    assert.ok(provider.historyReads > 0, "Recovery must read the fake provider history.");
    assert.equal(provider.inputs.filter((input) => input.held).length, 1, "The task input must not run twice.");
    assert.equal(provider.inputs.length, 2, "Neither input may be replayed.");
    return { outcome, terminal, duplicateInputs: 0, hostRestarted: true, taskState: after.task.state };
  } finally {
    for (const child of children) await stopHost(child);
    await rm(root, { recursive: true, force: true });
  }
}

if (process.env.RECOVERY_PHASE) {
  await host();
} else {
  const { test } = await import("bun:test");
  test("task terminal states survive a host restart without duplicate inputs", async () => {
    const report: { status: string; scenarios: Awaited<ReturnType<typeof scenario>>[] } = {
      status: "failed",
      scenarios: [],
    };
    try {
      for (const outcome of ["completed", "failed", "unknown"] as const) report.scenarios.push(await scenario(outcome));
      report.status = "passed";
    } finally {
      await mkdir(artifact, { recursive: true });
      await writeFile(join(artifact, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
    }
  }, 150_000);
}
