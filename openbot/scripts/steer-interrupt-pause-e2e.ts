// Checks the real AgentService + SQLite + a provider process for queue pause, Run now, Stop and send,
// the steer default, and teammate steer and interrupt through `openbot.send_message`. No model or
// credentials. `ELECTRON_RUN_AS_NODE=1 node_modules/electron/dist/Electron.app/Contents/MacOS/Electron --import tsx
// scripts/steer-interrupt-pause-e2e.ts` (node:sqlite with FTS5). Writes .openbot-build/steer-interrupt-pause-e2e/report.json.
import assert from "node:assert/strict";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { AGENT_PROVIDERS, type BusyMessageMode, DEFAULT_BUSY_MESSAGE_MODE } from "@openbot/contracts/ipc";
import { Effect } from "effect";
import type { AgentBrowserHost } from "../src/backend/agent/turn-lifecycle";
import { getString, isRecord } from "../src/backend/protocol";

const OUT = resolve(import.meta.dirname, "../.openbot-build/steer-interrupt-pause-e2e");
const root = await mkdtemp(join(tmpdir(), "openbot-steer-pause-"));
const userData = join(root, "user-data");
const home = join(root, "home");
const logPath = join(root, "protocol.jsonl");
const controlPath = join(root, "control.json");
const fakeCodex = join(root, "codex");

// Before the service modules load: the Codex process and the stores read the home folder.
process.env.OPENBOT_CODEX_PATH = fakeCodex;
process.env.OPENBOT_FAKE_CODEX_LOG = logPath;
process.env.OPENBOT_FAKE_CODEX_CONTROL = controlPath;
mkdirSync(home, { recursive: true });

const { AgentService } = await import("../src/backend/agent-service");
const { AgentStore } = await import("../src/backend/agent-store");
const { runCauseEffect } = await import("../src/backend/effect-boundary");
const { MailboxStore } = await import("../src/backend/mailbox-store");
const { OpenBotDatabase } = await import("../src/backend/openbot-database");

/**
 * A turn whose input has HOLD stays open until it is interrupted. A turn whose input has CALL sends
 * the `openbot.send_message` call named in the control file, logs the answer, and ends.
 */
const FAKE_CODEX = `#!/usr/bin/env node
const fs = require("node:fs");
if (process.argv.includes("--version")) {
  process.stdout.write("codex-cli 0.160.1\\n");
  process.exit(0);
}
const log = (entry) => fs.appendFileSync(process.env.OPENBOT_FAKE_CODEX_LOG, JSON.stringify(entry) + "\\n");
const control = () => JSON.parse(fs.readFileSync(process.env.OPENBOT_FAKE_CODEX_CONTROL, "utf8"));
const config = { config: { mcp_servers: {} }, layers: [{ name: { type: "user", file: "/fake/.codex/config.toml", profile: null }, version: "1", config: { mcp_servers: {} } }] };
let buffer = "";
let threads = 0;
let turns = 0;
let calls = 0;
/** threadId -> turnId of the held turn. */
const held = new Map();
/** request id -> the turn whose tool calls wait for it, shared by the calls of one turn. */
const pending = new Map();
process.stdout.on("error", (error) => {
  if (error.code === "EPIPE") process.exit(0);
  throw error;
});
const write = (message) => process.stdout.write(JSON.stringify(message) + "\\n");
const finish = (threadId, turnId, status) => {
  const item = { type: "agentMessage", id: "message-" + turnId, text: "Done.", phase: "final_answer" };
  if (status === "completed") write({ method: "item/completed", params: { threadId, turnId, item } });
  write({ method: "turn/completed", params: { threadId, turn: { id: turnId, status } } });
};
const answer = (message) => {
  const params = message.params || {};
  switch (message.method) {
    case "initialize": return write({ id: message.id, result: {} });
    case "account/read": return write({ id: message.id, result: { account: { type: "chatgpt", email: "codex@example.com" } } });
    case "account/rateLimits/read": return write({ id: message.id, result: { rateLimits: { limitId: "codex", primary: null, secondary: null }, rateLimitsByLimitId: null } });
    case "model/list": return write({ id: message.id, result: { data: [{ model: "gpt-5.5", displayName: "GPT-5.5" }] } });
    case "plugin/list": return write({ id: message.id, result: { marketplaces: [] } });
    case "config/read": return write({ id: message.id, result: config });
    case "config/batchWrite": return write({ id: message.id, result: {} });
    case "thread/start": return write({ id: message.id, result: { thread: { id: "thread-" + (++threads), turns: [] } } });
    case "thread/resume": return write({ id: message.id, result: { thread: { id: params.threadId, turns: [] } } });
    case "thread/unsubscribe": return write({ id: message.id, result: { status: "Unsubscribed" } });
    case "thread/turns/list":
    case "thread/items/list": return write({ id: message.id, result: { data: [], nextCursor: null } });
    case "thread/read": return write({ id: message.id, result: { thread: { id: params.threadId, turns: [] } } });
    case "turn/start": {
      const turnId = "turn-" + (++turns);
      const input = JSON.stringify(params.input);
      write({ id: message.id, result: { turn: { id: turnId, status: "inProgress", items: [] } } });
      write({ method: "turn/started", params: { threadId: params.threadId, turn: { id: turnId } } });
      if (input.includes("HOLD")) held.set(params.threadId, turnId);
      else if (input.includes("CALL")) {
        // calls sends several tool calls at once, as a model may; call sends one.
        const specs = control().calls ?? [control().call];
        const shared = { threadId: params.threadId, turnId, remaining: specs.length };
        for (const spec of specs) {
          const id = "call-" + (++calls);
          pending.set(id, shared);
          write({ id, method: "item/tool/call", params: { threadId: params.threadId, turnId, callId: id, namespace: "openbot", tool: "send_message", arguments: spec } });
        }
      } else setImmediate(() => finish(params.threadId, turnId, "completed"));
      return;
    }
    case "turn/steer":
      return write({ id: message.id, result: { turnId: params.expectedTurnId } });
    case "turn/interrupt": {
      write({ id: message.id, result: {} });
      const turnId = held.get(params.threadId);
      if (turnId) {
        held.delete(params.threadId);
        finish(params.threadId, turnId, "interrupted");
      }
      return;
    }
    default:
      return write({ id: message.id, error: { code: -32601, message: "Method not found: " + message.method } });
  }
};
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let newline = buffer.indexOf("\\n");
  while (newline >= 0) {
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    if (line) {
      const message = JSON.parse(line);
      if (message.method) {
        log({ method: message.method, id: message.id ?? null, params: message.params ?? null });
        if (message.id !== undefined) answer(message);
      } else if (pending.has(message.id)) {
        const shared = pending.get(message.id);
        pending.delete(message.id);
        log({ method: "tool-answer", id: message.id, params: { result: message.result ?? null, error: message.error ?? null } });
        if (--shared.remaining === 0) finish(shared.threadId, shared.turnId, "completed");
      }
    }
    newline = buffer.indexOf("\\n");
  }
});
`;

const unused = () => Effect.die(new Error("Not used by this check."));
const browser: AgentBrowserHost = {
  listTabs: () => [],
  beginTakeover: unused,
  endTakeover: () => {},
  resolveUploadTarget: unused,
  handleDynamicTool: unused,
  userControlTabFor: () => null,
  awaitUserControlRelease: unused,
  onChanged: () => () => {},
  onControlChanged: () => () => {},
  onDocumentChanged: () => () => {},
  clearControls: () => {},
  endControl: () => {},
  close: () => Effect.void,
  clearAgentSession: () => Effect.void,
};

function recordOf(value: unknown) {
  return isRecord(value) ? value : {};
}

/** The first busy outcome in the answer to a `send_message` call, from the fake's `tool-answer` entry. */
function busyOutcome(params: unknown) {
  const result = isRecord(params) && isRecord(params.result) ? params.result : {};
  const items = Array.isArray(result.contentItems) ? result.contentItems : [];
  const text = isRecord(items[0]) ? getString(items[0], "text") : undefined;
  const receipt = recordOf(text ? JSON.parse(text) : null);
  const busy = Array.isArray(receipt.busy) ? receipt.busy : [];
  const first = isRecord(busy[0]) ? busy[0] : {};
  return { result: getString(first, "result"), reason: getString(first, "reason") };
}

function protocol() {
  try {
    return readFileSync(logPath, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => {
        const entry = recordOf(JSON.parse(line));
        const params = isRecord(entry.params) ? entry.params : {};
        return {
          method: getString(entry, "method"),
          input: JSON.stringify(params.input ?? ""),
          threadId: getString(params, "threadId"),
          outcome: getString(entry, "method") === "tool-answer" ? busyOutcome(params) : null,
        };
      });
  } catch {
    return [];
  }
}

const calls = (method: string) => protocol().filter((entry) => entry.method === method);

/** Waits for a state of the service or of the fake Codex. */
async function waitFor<T>(label: string, read: () => T | null | undefined | false, timeoutMs = 20_000): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = read();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${label}.`);
    await new Promise((done) => setTimeout(done, 25));
  }
}

await writeFile(fakeCodex, FAKE_CODEX, { mode: 0o755 });
await writeFile(controlPath, JSON.stringify({ call: null }));
const database = new OpenBotDatabase(userData);
const store = new AgentStore(userData, home, database);
const mailbox = new MailboxStore(userData, store.sharedRoot, store.database);
let busyMode: BusyMessageMode = "queue";
const agents = new AgentService({
  store,
  mailbox,
  browser,
  preferredProvider: "codex",
  offProviders: AGENT_PROVIDERS.filter((provider) => provider !== "codex"),
  busyMessageMode: () => busyMode,
});

/** Read back from SQLite, not from the mailbox in memory. */
const pausedInDatabase = (agentId: string) => {
  const state = recordOf(database.readMailboxState());
  return Array.isArray(state.pausedAgentIds) && state.pausedAgentIds.includes(agentId);
};
const status = (deliveryId: string | undefined) =>
  deliveryId ? mailbox.getDelivery(deliveryId)?.delivery.status : null;
const runningTurn = (agentId: string) =>
  agents.listQueue(agentId).deliveries.find((item) => item.status === "running")?.turnId;
let sends = 0;
const send = async (agentId: string, text: string, interrupt?: boolean) => {
  const receipt = await runCauseEffect(
    agents.sendMessage({ agentId, text, clientMessageId: `send-${++sends}`, ...(interrupt ? { interrupt } : {}) }),
  );
  const id = receipt.deliveries[0]?.id;
  assert(id, `no delivery for ${text}`);
  return id;
};

const report: { passed: boolean; checks: string[]; error?: string } = { passed: false, checks: [] };
try {
  await runCauseEffect(agents.initialize());
  const target = await runCauseEffect(
    agents.createAgent({
      name: "Target",
      description: "Synthetic target",
      avatarSeed: "target",
      avatarHue: null,
      initialMessage: "HOLD-FIRST",
    }),
  );
  const firstDelivery = await waitFor(
    "the held first turn",
    () => agents.listQueue(target.id).deliveries.find((item) => item.status === "running")?.id,
  );

  // Pause, Run now, and Resume.
  const queuedA = await send(target.id, "QUEUED-A");
  const queuedB = await send(target.id, "QUEUED-B");
  assert.equal(status(queuedA), "queued");
  assert.equal(status(queuedB), "queued");
  await runCauseEffect(agents.setQueuePaused({ agentId: target.id, paused: true }));
  assert.equal(agents.listQueue(target.id).paused, true);
  assert(pausedInDatabase(target.id), "the pause is not stored");
  report.checks.push("Pause is in the queue snapshot and stored in SQLite; queued messages stay queued");

  await runCauseEffect(agents.runQueuedMessageNow({ agentId: target.id, deliveryId: queuedB }));
  await waitFor("QUEUED-B to complete", () => status(queuedB) === "completed");
  assert.equal(calls("turn/interrupt").length, 1);
  assert.equal(status(firstDelivery), "interrupted");
  assert.equal(status(queuedA), "queued");
  assert.equal(calls("turn/start").filter((entry) => entry.input.includes("QUEUED-A")).length, 0);
  report.checks.push("Run now stops the current turn and starts its message while the queue is paused; the rest waits");

  await runCauseEffect(agents.setQueuePaused({ agentId: target.id, paused: false }));
  await waitFor("QUEUED-A to complete after resume", () => status(queuedA) === "completed");
  assert.equal(agents.listQueue(target.id).paused, undefined);
  assert(!pausedInDatabase(target.id), "the resume is not stored");
  report.checks.push("Resume starts the kept message once and clears the stored pause");

  // Stop and send: the new message starts before the message that waited.
  const holdTwo = await send(target.id, "HOLD-TWO");
  await waitFor("HOLD-TWO to run", () => status(holdTwo) === "running");
  const queuedC = await send(target.id, "QUEUED-C");
  const nowD = await send(target.id, "NOW-D", true);
  await waitFor(
    "QUEUED-C and NOW-D to complete",
    () => status(queuedC) === "completed" && status(nowD) === "completed",
  );
  assert.equal(status(holdTwo), "interrupted");
  const order = calls("turn/start").map((entry) => entry.input);
  const nowIndex = order.findIndex((input) => input.includes("NOW-D"));
  const queuedIndex = order.findIndex((input) => input.includes("QUEUED-C"));
  assert(nowIndex >= 0 && nowIndex < queuedIndex, "NOW-D did not start before QUEUED-C");
  assert.equal(calls("turn/steer").length, 0);
  report.checks.push("Stop and send interrupts, starts the new message next, and keeps the earlier queued message");

  // The default busy mode steers.
  assert.equal(DEFAULT_BUSY_MESSAGE_MODE, "steer");
  busyMode = DEFAULT_BUSY_MESSAGE_MODE;
  const holdThree = await send(target.id, "HOLD-THREE");
  const heldTurn = await waitFor("HOLD-THREE to run", () => status(holdThree) === "running" && runningTurn(target.id));
  const steered = await send(target.id, "STEERED-E");
  await waitFor("STEERED-E to join the running turn", () => status(steered) === "running");
  assert.equal(mailbox.getDelivery(steered)?.delivery.turnId, heldTurn);
  assert(calls("turn/steer").some((entry) => entry.input.includes("STEERED-E")));
  report.checks.push("With the default busy mode, a message to a busy agent steers the running turn");

  // A teammate steers, interrupts, and meets the cooldown.
  const sender = await runCauseEffect(
    agents.createAgent({
      name: "Sender",
      description: "Synthetic sender",
      avatarSeed: "sender",
      avatarHue: null,
      initialMessage: "Hello.",
    }),
  );
  await waitFor("the sender's first turn to end", () =>
    agents.listQueue(sender.id).deliveries.every((item) => item.status === "completed"),
  );
  const teammateCall = async (busy: "steer" | "interrupt", text: string) => {
    const before = calls("tool-answer").length;
    await writeFile(
      controlPath,
      JSON.stringify({ call: { recipientAgentIds: [target.id], text, expectsReply: false, busy } }),
    );
    await send(sender.id, `CALL-${text}`);
    await waitFor(`the ${busy} tool answer`, () => calls("tool-answer").length > before);
    return calls("tool-answer").at(-1)?.outcome;
  };
  const steer = await teammateCall("steer", "TEAM-STEER");
  assert.equal(steer?.result, "steered");
  assert(calls("turn/steer").some((entry) => entry.input.includes("TEAM-STEER")));
  report.checks.push("A teammate steers another agent's running turn through send_message");

  const interrupt = await teammateCall("interrupt", "TEAM-INTERRUPT");
  assert.equal(interrupt?.result, "interrupted");
  await waitFor("the interrupted turn to end", () => status(holdThree) === "interrupted");
  await waitFor("TEAM-INTERRUPT to start", () =>
    calls("turn/start").some((entry) => entry.input.includes("TEAM-INTERRUPT")),
  );
  report.checks.push("A teammate interrupts another agent's turn, and its message starts next");

  const holdFour = await send(target.id, "HOLD-FOUR");
  await waitFor("HOLD-FOUR to run", () => status(holdFour) === "running");
  const interruptsBefore = calls("turn/interrupt").length;
  const cooled = await teammateCall("interrupt", "TEAM-AGAIN");
  assert.equal(cooled?.result, "queued");
  assert(cooled?.reason, "the cooldown gives no reason");
  assert.equal(calls("turn/interrupt").length, interruptsBefore);
  assert.equal(status(holdFour), "running");
  report.checks.push("A second interrupt within the cooldown is refused with a reason and the message stays queued");

  // Overlapping requests: the cooldown and the limit must hold when the calls arrive together.
  const interruptSpec = (text: string) => ({
    recipientAgentIds: [target.id],
    text,
    expectsReply: false,
    busy: "interrupt",
  });
  const overlapping = async (senderIds: string[], specs: unknown[], expected: number) => {
    const start = calls("tool-answer").length;
    const interruptsBefore = calls("turn/interrupt").length;
    await writeFile(controlPath, JSON.stringify({ calls: specs }));
    await Promise.all(senderIds.map((senderId) => send(senderId, "CALL-OVERLAP")));
    await waitFor("every overlapping tool answer", () => calls("tool-answer").length >= start + expected);
    const outcomes = calls("tool-answer")
      .slice(start)
      .map((entry) => entry.outcome?.result ?? "none");
    return { outcomes, stopped: calls("turn/interrupt").length - interruptsBefore };
  };

  // One new sender, two calls at once: the cooldown lets one stop through. The first sender is still
  // inside its cooldown for this target, so it cannot be used here.
  const cooling = await runCauseEffect(
    agents.createAgent({
      name: "Cooling",
      description: "Synthetic",
      avatarSeed: "cooling",
      avatarHue: null,
      initialMessage: "Hello.",
    }),
  );
  await waitFor("the cooling sender's first turn to end", () =>
    agents.listQueue(cooling.id).deliveries.every((item) => item.status === "completed"),
  );
  await waitFor("the held turn of the target", () => runningTurn(target.id));
  const sameSender = await overlapping([cooling.id], [interruptSpec("HOLD-COOL-1"), interruptSpec("HOLD-COOL-2")], 2);
  assert.deepEqual([...sameSender.outcomes].sort(), ["interrupted", "queued"]);
  assert.equal(sameSender.stopped, 1);
  report.checks.push("Two overlapping interrupts from one sender stop the turn once; the second waits on the cooldown");

  // Two senders at once, with one interrupt left in the window: the limit lets one stop through.
  const second = await runCauseEffect(
    agents.createAgent({
      name: "Second",
      description: "Synthetic",
      avatarSeed: "second",
      avatarHue: null,
      initialMessage: "Hello.",
    }),
  );
  const third = await runCauseEffect(
    agents.createAgent({
      name: "Third",
      description: "Synthetic",
      avatarSeed: "third",
      avatarHue: null,
      initialMessage: "Hello.",
    }),
  );
  await waitFor("the new senders' first turns to end", () =>
    [second.id, third.id].every((id) => agents.listQueue(id).deliveries.every((item) => item.status === "completed")),
  );
  await waitFor("the held turn of the target", () => runningTurn(target.id));
  const limited = await overlapping([second.id, third.id], [interruptSpec("HOLD-LIMIT")], 2);
  assert.equal(limited.outcomes.filter((result) => result === "interrupted").length, 1);
  assert.equal(limited.stopped, 1);
  report.checks.push("Two overlapping interrupts from two senders stop the turn once; the limit holds for the second");

  report.passed = true;
} catch (error) {
  report.error = error instanceof Error ? (error.stack ?? error.message) : String(error);
  process.exitCode = 1;
} finally {
  await runCauseEffect(agents.stop()).catch(() => {});
  database.close();
  mkdirSync(OUT, { recursive: true });
  writeFileSync(join(OUT, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  await rm(root, { recursive: true, force: true });
  // Machine-readable: the E2E report, without provider credentials or user content.
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}
