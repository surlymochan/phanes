// Checks that a Codex `ask_user` waits for the answer. Codex code mode calls tools from a JavaScript
// `exec` cell, and the cell returns control after about 30 seconds while the call still runs. The
// model can then end the turn, and the end of the turn expires the question before the user sees it.
//
// Part 1 runs the real agent service on a temporary profile with a fake Codex app-server, and reads
// the `thread/start` that OpenBot sends: it must ask Codex to call the `openbot` tools directly.
// Part 2 (`--live`) gives that configuration and those tools to the real `codex app-server` of this
// computer, holds the `ask_user` answer for 40 seconds, and reads the session file: the model must
// call `ask_user` directly and get no `exec` yield before the answer. It uses the Codex sign-in of
// this computer and one short model turn.
// `bunx tsx scripts/codex-ask-user-direct-e2e.ts [--live]` on Node 24 or later. Writes .openbot-build/codex-ask-user-direct-e2e/report.json.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { AGENT_PROVIDERS } from "@openbot/contracts/ipc";
import type { DynamicRecord } from "@openbot/contracts/runtime-values";
import { Effect } from "effect";
import type { AgentBrowserHost } from "../src/backend/agent/turn-lifecycle";
import { getArray, getRecord, getString, isRecord } from "../src/backend/protocol";

const LIVE = process.argv.includes("--live");
const HOLD_MS = 40_000;
const OUT = resolve(import.meta.dirname, "../.openbot-build/codex-ask-user-direct-e2e");
const root = await mkdtemp(join(tmpdir(), "openbot-ask-user-direct-"));
const userData = join(root, "user-data");
const home = join(root, "home");
const logPath = join(root, "protocol.jsonl");
const fakeCodex = join(root, "codex");
const realHome = process.env.HOME;
const realCodex = process.env.OPENBOT_CODEX_PATH ?? "codex";

// Before the service modules load: the Codex process and the stores read the home folder.
process.env.HOME = home;
process.env.CODEX_HOME = join(home, ".codex");
process.env.OPENBOT_CODEX_PATH = fakeCodex;
process.env.OPENBOT_FAKE_CODEX_LOG = logPath;
mkdirSync(home, { recursive: true });

const { AgentService } = await import("../src/backend/agent-service");
const { AgentStore } = await import("../src/backend/agent-store");
const { runCauseEffect } = await import("../src/backend/effect-boundary");
const { MailboxStore } = await import("../src/backend/mailbox-store");
const { OpenBotDatabase } = await import("../src/backend/openbot-database");

/** A Codex app-server with the methods that the service calls. Each turn ends with one answer. */
const FAKE_CODEX = `#!/usr/bin/env node
const fs = require("node:fs");
if (process.argv.includes("--version")) {
  process.stdout.write("codex-cli 0.162.1\\n");
  process.exit(0);
}
const log = (entry) => fs.appendFileSync(process.env.OPENBOT_FAKE_CODEX_LOG, JSON.stringify(entry) + "\\n");
const config = { config: { mcp_servers: {} }, layers: [{ name: { type: "user", file: "/fake/.codex/config.toml", profile: null }, version: "1", config: { mcp_servers: {} } }] };
let buffer = "";
let threads = 0;
let turns = 0;
process.stdout.on("error", (error) => {
  if (error.code === "EPIPE") process.exit(0);
  throw error;
});
const write = (message) => process.stdout.write(JSON.stringify(message) + "\\n");
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
      write({ id: message.id, result: { turn: { id: turnId, status: "inProgress", items: [] } } });
      write({ method: "turn/started", params: { threadId: params.threadId, turn: { id: turnId } } });
      setTimeout(() => {
        const item = { type: "agentMessage", id: "message-" + turnId, text: "Done.", phase: "final_answer" };
        write({ method: "item/completed", params: { threadId: params.threadId, turnId, item } });
        write({ method: "turn/completed", params: { threadId: params.threadId, turn: { id: turnId, status: "completed" } } });
      }, 50);
      return;
    }
    case "turn/interrupt": return write({ id: message.id, result: {} });
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
      log({ method: message.method ?? null, id: message.id ?? null, params: message.params ?? null });
      if (message.method && message.id !== undefined) answer(message);
    }
    newline = buffer.indexOf("\\n");
  }
});
`;

interface LiveReport {
  codex: string;
  sessionFile: string;
  firstToolCall: { type: string; name: string | null; namespace: string | null } | null;
  yieldsBeforeAnswer: number;
  heldMs: number;
  finalAnswer: string | null;
  passed: boolean;
}

const report: {
  features: unknown;
  openbotTools: string[];
  live: LiveReport | "skipped";
  passed: boolean;
  error?: string;
} = { features: null, openbotTools: [], live: "skipped", passed: false };

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

/** The `thread/start` parameters that OpenBot sent to the fake Codex. */
function threadStartParams(): DynamicRecord | null {
  try {
    for (const line of readFileSync(logPath, "utf8").split("\n").filter(Boolean)) {
      const entry = JSON.parse(line);
      if (isRecord(entry) && entry.method === "thread/start" && isRecord(entry.params)) return entry.params;
    }
  } catch {}
  return null;
}

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

/**
 * Starts a real Codex session with the configuration and tools that OpenBot sent, asks the model to
 * call `ask_user`, and answers only after `HOLD_MS`. The session file shows how the model called it.
 */
async function liveCheck(features: unknown, dynamicTools: unknown): Promise<LiveReport> {
  const cwd = join(root, "live");
  mkdirSync(cwd, { recursive: true });
  const child = spawn(realCodex, ["app-server"], {
    stdio: ["pipe", "pipe", "inherit"],
    env: { ...process.env, HOME: realHome, CODEX_HOME: undefined },
  });
  const lines = createInterface({ input: child.stdout });
  const pending = new Map<number, (message: DynamicRecord) => void>();
  let nextId = 0;
  const send = (message: DynamicRecord) => child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", ...message })}\n`);
  const request = (method: string, params: unknown) =>
    new Promise<DynamicRecord>((done) => {
      const id = ++nextId;
      pending.set(id, done);
      send({ id, method, params });
    });
  let askedAt = 0;
  let answeredAt = 0;
  let finalAnswer: string | null = null;
  const turnDone = new Promise<void>((done) => {
    lines.on("line", (line) => {
      const message = JSON.parse(line);
      if (!isRecord(message)) return;
      const id = typeof message.id === "number" ? message.id : null;
      if (id !== null && !message.method && pending.has(id)) {
        pending.get(id)?.(message);
        pending.delete(id);
        return;
      }
      const params = getRecord(message, "params") ?? {};
      if (message.method === "item/tool/call" && message.id !== undefined) {
        askedAt = Date.now();
        setTimeout(() => {
          answeredAt = Date.now();
          send({ id: message.id, result: { success: true, contentItems: [{ type: "inputText", text: "BLUE-42" }] } });
        }, HOLD_MS);
        return;
      }
      if (message.method && message.id !== undefined) send({ id: message.id, result: {} });
      const item = getRecord(params, "item");
      if (message.method === "item/completed" && item?.type === "agentMessage" && item.phase === "final_answer") {
        finalAnswer = getString(item, "text");
      }
      if (message.method === "turn/completed") done();
    });
  });
  try {
    await request("initialize", {
      clientInfo: { name: "openbot-e2e", version: "0" },
      capabilities: { experimentalApi: true },
    });
    send({ method: "initialized" });
    const started = await request("thread/start", {
      cwd,
      ephemeral: false,
      approvalPolicy: "never",
      sandbox: "read-only",
      serviceName: "openbot-e2e",
      config: { features },
      dynamicTools,
    });
    const thread = getRecord(getRecord(started, "result"), "thread");
    const threadId = getString(thread, "id");
    const sessionFile = getString(thread, "path");
    assert.ok(threadId && sessionFile, `thread/start failed: ${JSON.stringify(started.error ?? started)}`);
    const turn = await request("turn/start", {
      threadId,
      input: [
        {
          type: "text",
          text: "Run the shell command `echo ready`. Then call the openbot ask_user tool once with the question 'Pick a code' and no options. Reply with the answer only.",
        },
      ],
      effort: "low",
    });
    assert.ok(!turn.error, `turn/start failed: ${JSON.stringify(turn.error)}`);
    await Promise.race([
      turnDone,
      new Promise((_, fail) => setTimeout(() => fail(new Error("The live turn did not end.")), HOLD_MS + 120_000)),
    ]);
    const entries = readFileSync(sessionFile, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => getRecord(JSON.parse(line), "payload") ?? {});
    const calls = entries.filter((entry) => entry.type === "function_call" || entry.type === "custom_tool_call");
    const askCall = calls.find((entry) =>
      JSON.stringify(entry.type === "custom_tool_call" ? entry.input : entry.name).includes("ask_user"),
    );
    const askIndex = askCall ? entries.indexOf(askCall) : -1;
    const answerIndex = entries.findIndex(
      (entry, index) => index > askIndex && JSON.stringify(entry.output ?? "").includes("BLUE-42"),
    );
    const yieldsBeforeAnswer = entries
      .slice(askIndex + 1, answerIndex < 0 ? undefined : answerIndex)
      .filter((entry) => JSON.stringify(entry.output ?? "").includes("Script running with cell ID")).length;
    const live: LiveReport = {
      codex: realCodex,
      sessionFile,
      firstToolCall: askCall
        ? {
            type: String(askCall.type),
            name: getString(askCall, "name"),
            namespace: getString(askCall, "namespace"),
          }
        : null,
      yieldsBeforeAnswer,
      heldMs: answeredAt - askedAt,
      finalAnswer,
      passed: false,
    };
    assert.equal(live.firstToolCall?.type, "function_call", "Codex called ask_user from an exec cell.");
    assert.equal(live.firstToolCall?.name, "ask_user");
    assert.equal(live.firstToolCall?.namespace, "openbot");
    assert.equal(yieldsBeforeAnswer, 0, "The exec cell returned control before the answer.");
    assert.ok(live.heldMs >= HOLD_MS);
    assert.match(finalAnswer ?? "", /BLUE-42/);
    live.passed = true;
    return live;
  } finally {
    child.kill();
  }
}

await writeFile(fakeCodex, FAKE_CODEX, { mode: 0o755 });
const database = new OpenBotDatabase(userData);
const store = new AgentStore(userData, home, database);
const mailbox = new MailboxStore(userData, store.sharedRoot, store.database);
const agents = new AgentService({
  store,
  mailbox,
  browser,
  preferredProvider: "codex",
  offProviders: AGENT_PROVIDERS.filter((provider) => provider !== "codex"),
});

try {
  await runCauseEffect(agents.initialize());
  await runCauseEffect(
    agents.createAgent({
      name: "Ask user",
      description: "Checks how Codex calls ask_user.",
      avatarSeed: "ask-user",
      avatarHue: null,
      initialMessage: "First message.",
    }),
  );
  const params = await waitFor("thread/start", threadStartParams);
  const features = getRecord(getRecord(params, "config"), "features");
  report.features = features;
  const openbot = getArray(params, "dynamicTools").find(
    (namespace) => isRecord(namespace) && namespace.name === "openbot",
  );
  report.openbotTools = isRecord(openbot)
    ? getArray(openbot, "tools").flatMap((tool) => (isRecord(tool) && typeof tool.name === "string" ? [tool.name] : []))
    : [];
  assert.ok(report.openbotTools.includes("ask_user"), "The openbot namespace has no ask_user.");
  assert.deepEqual(getArray(getRecord(features, "code_mode"), "direct_only_tool_namespaces"), ["openbot"]);
  if (LIVE) report.live = await liveCheck(features, params.dynamicTools);
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
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}
