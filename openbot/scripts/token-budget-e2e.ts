// Checks the real AgentService + SQLite + a provider process for the token budget: the hourly hold,
// the turn stop at a step boundary, Continue, a limit raised by the user, and the plan-limit stop that
// carries on by itself when the plan limit ends. No model or credentials.
// `ELECTRON_RUN_AS_NODE=1 node_modules/electron/dist/Electron.app/Contents/MacOS/Electron --import tsx
// scripts/token-budget-e2e.ts` (node:sqlite with FTS5).
// Writes .openbot-build/token-budget-e2e/report.json. The plan-limit check waits for the 30 second
// grace after the reset, so a run takes about one minute.
import assert from "node:assert/strict";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { AGENT_PAUSE_ITEM_TYPE, AGENT_PROVIDERS, type TokenBudgetPreference } from "@openbot/contracts/ipc";
import { Effect } from "effect";
import type { AgentBrowserHost } from "../src/backend/agent/turn-lifecycle";
import { getString, isRecord } from "../src/backend/protocol";

const OUT = resolve(import.meta.dirname, "../.openbot-build/token-budget-e2e");
const root = await mkdtemp(join(tmpdir(), "openbot-token-budget-"));
const userData = join(root, "user-data");
const home = join(root, "home");
const logPath = join(root, "protocol.jsonl");
const fakeCodex = join(root, "codex");

// Before the service modules load: the Codex process and the stores read the home folder.
process.env.OPENBOT_CODEX_PATH = fakeCodex;
process.env.OPENBOT_FAKE_CODEX_LOG = logPath;
mkdirSync(home, { recursive: true });

const { AgentService } = await import("../src/backend/agent-service");
const { AgentStore } = await import("../src/backend/agent-store");
const { runCauseEffect } = await import("../src/backend/effect-boundary");
const { MailboxStore } = await import("../src/backend/mailbox-store");
const { OpenBotDatabase } = await import("../src/backend/openbot-database");

/**
 * A turn whose input has STEPS-n runs n steps, 60 ms apart. Each step reports 400 more tokens and
 * finishes a command, and an interrupt ends the turn at once. A turn whose input has PLAN-n runs one
 * step and then reports that the plan refused it, with a reset 2 seconds away.
 */
const FAKE_CODEX = `#!/usr/bin/env node
const fs = require("node:fs");
if (process.argv.includes("--version")) {
  process.stdout.write("codex-cli 0.160.1\\n");
  process.exit(0);
}
const log = (entry) => fs.appendFileSync(process.env.OPENBOT_FAKE_CODEX_LOG, JSON.stringify(entry) + "\\n");
const config = { config: { mcp_servers: {} }, layers: [{ name: { type: "user", file: "/fake/.codex/config.toml", profile: null }, version: "1", config: { mcp_servers: {} } }] };
let buffer = "";
let threads = 0;
let turns = 0;
let steps = 0;
/** threadId -> cumulative counters, as Codex reports them. */
const totals = new Map();
/** threadId -> the running turn: { turnId, timer }. */
const running = new Map();
process.stdout.on("error", (error) => {
  if (error.code === "EPIPE") process.exit(0);
  throw error;
});
const write = (message) => process.stdout.write(JSON.stringify(message) + "\\n");
const finish = (threadId, turnId, status) => {
  running.delete(threadId);
  if (status === "completed") {
    const item = { type: "agentMessage", id: "message-" + turnId, text: "Done.", phase: "final_answer" };
    write({ method: "item/completed", params: { threadId, turnId, item } });
  }
  write({ method: "turn/completed", params: { threadId, turn: { id: turnId, status } } });
};
const step = (threadId, turnId) => {
  const total = totals.get(threadId) || { inputTokens: 0, cachedInputTokens: 0, outputTokens: 0 };
  total.inputTokens += 300;
  total.outputTokens += 100;
  totals.set(threadId, total);
  const usage = { total: { ...total, totalTokens: total.inputTokens + total.outputTokens }, last: { inputTokens: 300, outputTokens: 100, totalTokens: 400 } };
  write({ method: "thread/tokenUsage/updated", params: { threadId, turnId, tokenUsage: { ...usage, modelContextWindow: 1000000 } } });
  const id = "command-" + (++steps);
  write({ method: "item/completed", params: { threadId, turnId, item: { type: "commandExecution", id, command: "true", aggregatedOutput: "", status: "completed" } } });
  log({ method: "step", threadId, turnId });
};
const runSteps = (threadId, turnId, count) => {
  let done = 0;
  const timer = setInterval(() => {
    if (done >= count) {
      clearInterval(timer);
      finish(threadId, turnId, "completed");
      return;
    }
    done += 1;
    step(threadId, turnId);
  }, 60);
  running.set(threadId, { turnId, timer });
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
      const stepsMatch = /STEPS-(\\d+)/.exec(input);
      if (stepsMatch) runSteps(params.threadId, turnId, Number(stepsMatch[1]));
      else if (input.includes("PLAN-")) {
        setTimeout(() => {
          step(params.threadId, turnId);
          write({ method: "openbot/usageLimit", params: { threadId: params.threadId, turnId, resetsAt: Math.floor(Date.now() / 1000) + 2 } });
          write({ method: "error", params: { threadId: params.threadId, turnId, willRetry: false, error: { message: "You've hit your usage limit.", codexErrorInfo: "usageLimitExceeded" } } });
          finish(params.threadId, turnId, "failed");
        }, 60);
      } else setImmediate(() => finish(params.threadId, turnId, "completed"));
      return;
    }
    case "turn/steer":
      return write({ id: message.id, result: { turnId: params.expectedTurnId } });
    case "turn/interrupt": {
      write({ id: message.id, result: {} });
      const turn = running.get(params.threadId);
      if (turn) {
        clearInterval(turn.timer);
        finish(params.threadId, turn.turnId, "interrupted");
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

function protocol() {
  try {
    return readFileSync(logPath, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => {
        const entry = recordOf(JSON.parse(line));
        const params = isRecord(entry.params) ? entry.params : {};
        return { method: getString(entry, "method"), input: JSON.stringify(params.input ?? "") };
      });
  } catch {
    return [];
  }
}

const calls = (method: string) => protocol().filter((entry) => entry.method === method);
const startsWith = (text: string) => calls("turn/start").filter((entry) => entry.input.includes(text)).length;

/** Waits for a state of the service or of the fake Codex. */
async function waitFor<T>(
  label: string,
  read: () => T | null | undefined | false | Promise<T | null | undefined | false>,
  timeoutMs = 20_000,
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await read();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${label}.`);
    await new Promise((done) => setTimeout(done, 25));
  }
}

/** Waits long enough for a start that would be wrong to show. */
const settle = (ms = 400) => new Promise((done) => setTimeout(done, ms));

await writeFile(fakeCodex, FAKE_CODEX, { mode: 0o755 });
const database = new OpenBotDatabase(userData);
const store = new AgentStore(userData, home, database);
const mailbox = new MailboxStore(userData, store.sharedRoot, store.database);
let limits: TokenBudgetPreference = { perHour: 0, perTurn: 0 };
const agents = new AgentService({
  store,
  mailbox,
  browser,
  preferredProvider: "codex",
  offProviders: AGENT_PROVIDERS.filter((provider) => provider !== "codex"),
  busyMessageMode: () => "queue",
  tokenBudget: () => limits,
});

const status = (deliveryId: string | undefined) =>
  deliveryId ? mailbox.getDelivery(deliveryId)?.delivery.status : null;
let sends = 0;
const send = async (agentId: string, text: string) => {
  const receipt = await runCauseEffect(agents.sendMessage({ agentId, text, clientMessageId: `send-${++sends}` }));
  const id = receipt.deliveries[0]?.id;
  assert(id, `no delivery for ${text}`);
  return id;
};
/** The pause markers of the agent's chat, oldest first, read from the stored conversation. */
const pauses = async (agentId: string) => {
  const snapshot = await runCauseEffect(agents.readConversation(agentId));
  return snapshot.messages.filter((message) => message.itemType === AGENT_PAUSE_ITEM_TYPE && message.pause);
};

const report: { passed: boolean; checks: string[]; error?: string } = { passed: false, checks: [] };
try {
  await runCauseEffect(agents.initialize());
  const agent = await runCauseEffect(
    agents.createAgent({
      name: "Budget",
      description: "Synthetic",
      avatarSeed: "budget",
      avatarHue: null,
      initialMessage: "Hello.",
    }),
  );
  const idle = () =>
    agents.listQueue(agent.id).deliveries.every((item) => !["queued", "starting", "running"].includes(item.status));
  await waitFor("the first turn to end", idle);

  // 1. The turn limit stops a turn after a step and holds the agent.
  limits = { perHour: 0, perTurn: 1_000 };
  const longTurn = await send(agent.id, "STEPS-10");
  await waitFor("the long turn to stop", () => status(longTurn) === "interrupted");
  const stepsRun = calls("step").length;
  assert(stepsRun >= 3 && stepsRun < 10, `the turn ran ${stepsRun} steps`);
  assert.equal(calls("turn/interrupt").length, 1);
  const [turnMarker] = await pauses(agent.id);
  assert.equal(turnMarker?.pause?.reason, "turn-budget");
  assert.equal(turnMarker?.pause?.resolved, false);
  assert.equal(turnMarker?.pause?.resume, true);
  report.checks.push(
    `A turn past the turn limit stops after ${stepsRun} of 10 steps and writes a marker with a resume`,
  );

  const queuedAfter = await send(agent.id, "QUEUED-AFTER");
  await settle();
  assert.equal(status(queuedAfter), "queued");
  assert.equal(startsWith("QUEUED-AFTER"), 0);
  report.checks.push("The agent starts no new turn while the stop holds it");

  // 2. Continue ends the hold, starts the queue, and carries the stopped work on once.
  limits = { perHour: 0, perTurn: 0 };
  assert(turnMarker, "no marker");
  await runCauseEffect(agents.continueAgentPause({ agentId: agent.id, messageId: turnMarker.id }));
  await waitFor(
    "the queued message and the resume to end",
    () => idle() && startsWith("Continue the work you stopped") === 1,
  );
  assert.equal(status(queuedAfter), "completed");
  assert.equal((await pauses(agent.id))[0]?.pause?.resolved, true);
  await runCauseEffect(agents.continueAgentPause({ agentId: agent.id, messageId: turnMarker.id }));
  await settle();
  assert.equal(startsWith("Continue the work you stopped"), 1);
  report.checks.push("Continue starts the held queue and one resume turn; a second Continue changes nothing");

  // 3. The hourly limit holds the agent after the work that crossed it, and does not stop that work.
  limits = { perHour: 1_500, perTurn: 0 };
  const firstHour = await send(agent.id, "STEPS-3");
  await waitFor("the first hour turn to end", () => status(firstHour) === "completed");
  assert.equal((await pauses(agent.id)).filter((marker) => marker.pause?.reason === "hour-budget").length, 0);
  const crossing = await send(agent.id, "STEPS-3");
  await waitFor("the crossing turn to end", () => status(crossing) === "completed");
  const hourMarker = (await pauses(agent.id)).find((marker) => marker.pause?.reason === "hour-budget");
  assert(hourMarker && !hourMarker.pause?.resolved, "no open hour marker");
  const interruptsBefore = calls("turn/interrupt").length;
  assert.equal(interruptsBefore, 1, "the hourly limit stopped a turn");
  const blocked = await send(agent.id, "BLOCKED-HOUR");
  await settle();
  assert.equal(status(blocked), "queued");
  assert.equal(startsWith("BLOCKED-HOUR"), 0);
  report.checks.push("The hourly limit lets the crossing turn finish, then holds the next message");

  await runCauseEffect(agents.continueAgentPause({ agentId: agent.id, messageId: hourMarker.id }));
  await waitFor("the held message to end", () => status(blocked) === "completed");
  const afterContinue = await send(agent.id, "STEPS-3");
  await waitFor("a turn in the new window", () => status(afterContinue) === "completed");
  assert.equal(
    (await pauses(agent.id)).filter((marker) => marker.pause?.reason === "hour-budget" && !marker.pause.resolved)
      .length,
    0,
  );
  report.checks.push("Continue starts a new window: the tokens before it do not count again");

  // 4. Raising the limit ends an hourly hold without a click.
  limits = { perHour: 1_500, perTurn: 0 };
  const second = await send(agent.id, "STEPS-3");
  await waitFor("the second window turn to end", () => status(second) === "completed");
  const held = await send(agent.id, "BLOCKED-RAISE");
  await settle();
  assert.equal(status(held), "queued");
  limits = { perHour: 0, perTurn: 0 };
  await runCauseEffect(agents.tokenBudgetChanged());
  await waitFor("the held message after the limit was raised", () => status(held) === "completed");
  assert.equal(
    (await pauses(agent.id)).filter((marker) => marker.pause?.reason === "hour-budget" && !marker.pause.resolved)
      .length,
    0,
  );
  report.checks.push("A raised limit ends the hourly hold and resolves its marker");

  // 5. A spent plan stops a turn that did work; the agent carries on when the limit ends.
  const planTurn = await send(agent.id, "PLAN-1");
  await waitFor("the refused turn to end", () => status(planTurn) === "interrupted");
  const open = await waitFor("the open plan marker", async () =>
    (await pauses(agent.id)).find((marker) => marker.pause?.reason === "plan-limit" && !marker.pause.resolved),
  );
  assert.equal(open.pause?.resume, true);
  const resumesBefore = startsWith("Continue the work you stopped");
  await waitFor(
    "the resume after the plan limit",
    () => startsWith("Continue the work you stopped") > resumesBefore && idle(),
    60_000,
  );
  assert.equal((await pauses(agent.id)).find((marker) => marker.id === open.id)?.pause?.resolved, true);
  report.checks.push(
    "A turn the plan refused after work ends as interrupted, and the agent resumes when the limit ends",
  );

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
