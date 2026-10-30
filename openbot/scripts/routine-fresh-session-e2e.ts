// Checks that each routine run of an agent's own chat starts its own provider session, so that it
// does not send the whole chat context again. Runs the real agent service on a temporary profile with
// a fake Codex app-server. The fake keeps the context of each session: each request sends all of the
// session's earlier input again, as a model request does, and reports that size as input tokens.
// The chat gets about 45,000 tokens of history, then a routine runs twice. The check asserts that
// each run starts an ephemeral session and closes it, that its request stays small, that its answer
// shows in the chat, that the chat keeps its session, and that the next chat turn reads the answers.
// `bun scripts/routine-fresh-session-e2e.ts`. Writes .openbot-build/routine-fresh-session-e2e/report.json.
import assert from "node:assert/strict";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { AGENT_PROVIDERS, decodeUsageTokens } from "@openbot/contracts/ipc";
import { Effect } from "effect";
import type { AgentBrowserHost } from "../src/backend/agent/turn-lifecycle";

const OUT = resolve(import.meta.dirname, "../.openbot-build/routine-fresh-session-e2e");
const root = await mkdtemp(join(tmpdir(), "openbot-routine-session-"));
const userData = join(root, "user-data");
const home = join(root, "home");
const fakeCodex = join(root, "codex");
const fakeLog = join(root, "codex.jsonl");

// Before the service modules load: the Codex process and the stores read the home folder.
process.env.HOME = home;
process.env.CODEX_HOME = join(home, ".codex");
process.env.OPENBOT_CODEX_PATH = fakeCodex;
mkdirSync(home, { recursive: true });

const { AgentService } = await import("../src/backend/agent-service");
const { AgentStore } = await import("../src/backend/agent-store");
const { runCauseEffect } = await import("../src/backend/effect-boundary");
const { MailboxStore } = await import("../src/backend/mailbox-store");
const { OpenBotDatabase } = await import("../src/backend/openbot-database");

/** Tokens that each session holds before its first request: the instructions and the tools. */
const BASE_TOKENS = 3_000;
/** Each history message, in characters. Four of them make about 45,000 tokens. */
const HISTORY_CHARACTERS = 45_000;
const ROUTINE_INPUT_LIMIT = 10_000;
const ROUTINE_MARK = "ROUTINE-CHECK";

/**
 * A Codex app-server with the methods that the service calls. It writes each `thread/start`,
 * `thread/resume`, `thread/unsubscribe` and `turn/start` to the log. A routine turn answers with a
 * numbered report; other turns answer "Done.".
 */
const FAKE_CODEX = `#!/usr/bin/env node
const { appendFileSync } = require("node:fs");
if (process.argv.includes("--version")) {
  process.stdout.write("codex-cli 0.160.1\\n");
  process.exit(0);
}
const LOG = ${JSON.stringify(fakeLog)};
const BASE = ${BASE_TOKENS};
const config = { config: { mcp_servers: {} }, layers: [{ name: { type: "user", file: "/fake/.codex/config.toml", profile: null }, version: "1", config: { mcp_servers: {} } }] };
let buffer = "";
let threads = 0;
let turns = 0;
let reports = 0;
/** threadId -> characters of all input so far, and the prompt and totals of the last request. */
const context = new Map();
process.stdout.on("error", (error) => {
  if (error.code === "EPIPE") process.exit(0);
  throw error;
});
const log = (entry) => appendFileSync(LOG, JSON.stringify(entry) + "\\n");
const write = (message) => process.stdout.write(JSON.stringify(message) + "\\n");
const breakdown = (input, cached, output) => ({ totalTokens: input + output, inputTokens: input, cachedInputTokens: cached, cacheWriteInputTokens: 0, outputTokens: output, reasoningOutputTokens: 0 });
const answer = (message) => {
  const params = message.params || {};
  switch (message.method) {
    case "initialize": return write({ id: message.id, result: {} });
    case "account/read": return write({ id: message.id, result: { account: { type: "chatgpt", email: "codex@example.com" } } });
    case "account/rateLimits/read": return write({ id: message.id, result: { rateLimits: { limitId: "codex", primary: null, secondary: null }, rateLimitsByLimitId: null } });
    case "model/list": return write({ id: message.id, result: { data: [{ model: "gpt-6-luna", displayName: "GPT-6 Luna" }] } });
    case "plugin/list": return write({ id: message.id, result: { marketplaces: [] } });
    case "config/read": return write({ id: message.id, result: config });
    case "config/batchWrite": return write({ id: message.id, result: {} });
    case "thread/start": {
      const id = "thread-" + (++threads);
      log({ method: "thread/start", threadId: id, ephemeral: params.ephemeral ?? null, persistSession: params.persistSession ?? null });
      return write({ id: message.id, result: { thread: { id, turns: [] } } });
    }
    case "thread/resume":
      log({ method: "thread/resume", threadId: params.threadId });
      return write({ id: message.id, result: { thread: { id: params.threadId, turns: [] } } });
    case "thread/unsubscribe":
      log({ method: "thread/unsubscribe", threadId: params.threadId });
      return write({ id: message.id, result: { status: "Unsubscribed" } });
    case "thread/turns/list":
    case "thread/items/list": return write({ id: message.id, result: { data: [], nextCursor: null } });
    case "thread/read": return write({ id: message.id, result: { thread: { id: params.threadId, turns: [] } } });
    case "turn/start": {
      const turnId = "turn-" + (++turns);
      const text = (params.input || []).map((item) => item.text || "").join("\\n");
      const state = context.get(params.threadId) ?? { characters: 0, prompt: 0, input: 0, cached: 0, output: 0 };
      state.characters += text.length;
      const prompt = BASE + Math.ceil(state.characters / 4);
      const cached = Math.min(state.prompt, prompt);
      Object.assign(state, { prompt, input: state.input + prompt, cached: state.cached + cached, output: state.output + 100 });
      context.set(params.threadId, state);
      log({ method: "turn/start", threadId: params.threadId, turnId, characters: text.length, prompt, text });
      write({ id: message.id, result: { turn: { id: turnId, status: "inProgress", items: [] } } });
      write({ method: "turn/started", params: { threadId: params.threadId, turn: { id: turnId } } });
      const tokenUsage = { total: breakdown(state.input, state.cached, state.output), last: breakdown(prompt, cached, 100), modelContextWindow: 1000000 };
      write({ method: "thread/tokenUsage/updated", params: { threadId: params.threadId, turnId, tokenUsage } });
      const reply = text.includes(${JSON.stringify(ROUTINE_MARK)}) ? "Routine report " + (++reports) + ": the build is green." : "Done.";
      setTimeout(() => {
        const item = { type: "agentMessage", id: "message-" + turnId, text: reply, phase: "final_answer" };
        write({ method: "item/completed", params: { threadId: params.threadId, turnId, item } });
        write({ method: "turn/completed", params: { threadId: params.threadId, turn: { id: turnId, status: "completed" } } });
      }, 50);
      return;
    }
    case "turn/interrupt": {
      write({ id: message.id, result: {} });
      return write({ method: "turn/completed", params: { threadId: params.threadId, turn: { id: params.turnId, status: "interrupted" } } });
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
      if (message.method && message.id !== undefined) answer(message);
    }
    newline = buffer.indexOf("\\n");
  }
});
`;

interface LogEntry {
  method: string;
  threadId: string;
  turnId?: string;
  ephemeral?: boolean | null;
  persistSession?: boolean | null;
  characters?: number;
  prompt?: number;
  text?: string;
}

interface TurnReport {
  name: string;
  threadId: string;
  turnId: string;
  inputCharacters: number;
  recordedInputTokens: number;
  sessionId: string;
}

const report: {
  mainSession?: { before: string; after: string };
  threadStarts: Omit<LogEntry, "text">[];
  closedThreads: string[];
  turns: TurnReport[];
  routineAnswersInChat: string[];
  digestInNextTurn?: { marker: boolean; answers: string[] };
  checks: { name: string; passed: boolean }[];
  passed: boolean;
  error?: string;
} = { threadStarts: [], closedThreads: [], turns: [], routineAnswersInChat: [], checks: [], passed: false };

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

/** Waits for a state of the service, its database or the fake's log. */
async function waitFor<T>(label: string, read: () => T | null | undefined | false, timeoutMs = 20_000): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = read();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${label}.`);
    await new Promise((done) => setTimeout(done, 25));
  }
}

function fakeEntries(): LogEntry[] {
  let text = "";
  try {
    text = readFileSync(fakeLog, "utf8");
  } catch {
    return [];
  }
  return text
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function pass(name: string, condition: boolean, detail?: string) {
  report.checks.push({ name, passed: condition });
  assert.ok(condition, detail ? `${name}: ${detail}` : name);
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
let connection: DatabaseSync | null = null;
const sqlite = () => {
  connection ??= new DatabaseSync(database.path);
  return connection;
};
const closeReader = () => connection?.close();

/** The usage session and the input tokens that the records of one provider turn hold. */
function usageOf(turnId: string): { sessionId: string; input: number } | null {
  const rows = sqlite()
    .prepare("SELECT session_id, tokens_json FROM agent_usage_records WHERE turn_id = ?")
    .all(turnId)
    .map((row) => ({
      sessionId: String(row.session_id),
      tokens: decodeUsageTokens(JSON.parse(String(row.tokens_json))),
    }));
  const first = rows[0];
  if (!first) return null;
  const input = rows.reduce(
    (sum, row) =>
      sum + (row.tokens.uncachedInput ?? 0) + (row.tokens.cachedInput ?? 0) + (row.tokens.cacheCreation ?? 0),
    0,
  );
  return { sessionId: first.sessionId, input };
}

function mainSession(threadId: string): string[] {
  return sqlite()
    .prepare("SELECT external_session_id FROM projection_provider_sessions WHERE thread_id = ? AND state = 'active'")
    .all(threadId)
    .map((row) => String(row.external_session_id));
}

async function settled(deliveryId: string, label: string) {
  const status = await waitFor(label, () => {
    const value = mailbox.getDelivery(deliveryId)?.delivery.status;
    return value === "completed" || value === "failed" || value === "interrupted" ? value : null;
  });
  assert.equal(status, "completed", `${label}: ${mailbox.getDelivery(deliveryId)?.delivery.error ?? status}`);
}

async function send(agentId: string, text: string, label: string) {
  const receipt = await runCauseEffect(agents.sendMessage({ agentId, text }));
  const deliveryId = receipt.deliveries[0]?.id;
  assert.ok(deliveryId, `${label}: no delivery`);
  await settled(deliveryId, label);
}

function turnEntry(name: string, entry: LogEntry): TurnReport {
  assert.ok(entry.turnId);
  const usage = usageOf(entry.turnId);
  assert.ok(usage, `${name}: no usage records`);
  const result: TurnReport = {
    name,
    threadId: entry.threadId,
    turnId: entry.turnId,
    inputCharacters: entry.characters ?? 0,
    recordedInputTokens: usage.input,
    sessionId: usage.sessionId,
  };
  report.turns.push(result);
  return result;
}

try {
  await runCauseEffect(agents.initialize());
  const agent = await runCauseEffect(
    agents.createAgent({
      name: "Routines",
      description: "Checks routine sessions.",
      avatarSeed: "routines",
      avatarHue: null,
      initialMessage: "Hello.",
    }),
  );
  const chatThread = await waitFor(
    "the agent's own thread",
    () => store.list().find((a) => a.id === agent.id)?.threadId,
  );
  await waitFor("the first chat turn to end", () =>
    agents
      .listQueue(agent.id)
      .deliveries.every((delivery) => !["queued", "starting", "running"].includes(delivery.status)),
  );
  for (let index = 1; index <= 4; index += 1) {
    const filler = `History message ${index}. ${"The quarterly numbers look stable this week. ".repeat(1_000)}`;
    await send(agent.id, filler.slice(0, HISTORY_CHARACTERS), `history message ${index}`);
  }
  const [mainBefore] = mainSession(chatThread);
  assert.ok(mainBefore, "The chat has an active provider session.");
  const lastChatTurn = fakeEntries()
    .filter((entry) => entry.method === "turn/start" && entry.threadId === mainBefore)
    .at(-1);
  assert.ok(lastChatTurn);
  const chatTurn = turnEntry("last history turn", lastChatTurn);
  pass("The chat session holds about 45,000 tokens", chatTurn.recordedInputTokens >= 45_000);

  const routine = agents.createRoutine({
    agentId: agent.id,
    name: "Build check",
    instruction: `${ROUTINE_MARK}: check the build and report.`,
    active: false,
    timezone: "UTC",
    schedule: { kind: "daily", time: "09:00" },
  });
  const routineTurns: TurnReport[] = [];
  for (const run of [1, 2]) {
    const queued = await runCauseEffect(agents.testRoutine({ agentId: agent.id, routineId: routine.id }));
    assert.ok(queued.deliveryId, `routine run ${run}: no delivery`);
    await settled(queued.deliveryId, `routine run ${run}`);
    const entry = await waitFor(`routine run ${run} turn`, () =>
      fakeEntries()
        .filter((item) => item.method === "turn/start" && item.text?.includes(ROUTINE_MARK))
        .at(run - 1),
    );
    await waitFor(`routine run ${run} session to close`, () =>
      fakeEntries().some((item) => item.method === "thread/unsubscribe" && item.threadId === entry.threadId),
    );
    routineTurns.push(turnEntry(`routine run ${run}`, entry));
  }

  const entries = fakeEntries();
  report.threadStarts = entries
    .filter((entry) => entry.method === "thread/start")
    .map(({ text: _text, ...rest }) => rest);
  report.closedThreads = entries
    .filter((entry) => entry.method === "thread/unsubscribe")
    .map((entry) => entry.threadId);
  const routineStarts = report.threadStarts.filter((entry) => entry.ephemeral === true);
  pass("Each routine run starts one new session", routineStarts.length === 2);
  pass(
    "Routine sessions are ephemeral and not saved",
    routineStarts.every((entry) => entry.persistSession === false),
  );
  pass(
    "Each routine turn runs in its own session",
    routineTurns.map((turn) => turn.threadId).join() === routineStarts.map((entry) => entry.threadId).join() &&
      routineTurns.every((turn) => turn.threadId !== mainBefore),
  );
  pass(
    "Each routine session closes after its run",
    routineTurns.every((turn) => report.closedThreads.includes(turn.threadId)),
  );
  pass(
    "The first routine request stays small",
    routineTurns.every((turn) => turn.recordedInputTokens <= ROUTINE_INPUT_LIMIT),
    routineTurns.map((turn) => turn.recordedInputTokens).join(", "),
  );
  pass(
    "Routine usage belongs to the routine session",
    routineTurns.every((turn) => turn.sessionId === `routine:${turn.threadId}`),
  );
  pass(
    "The second run reads the first run's answer",
    entries.some(
      (entry) =>
        entry.method === "turn/start" &&
        entry.threadId === routineTurns[1]?.threadId &&
        entry.text?.includes("Routine report 1"),
    ),
  );
  const after = mainSession(chatThread);
  report.mainSession = { before: mainBefore, after: after.join(",") };
  pass("The chat keeps its one provider session", after.length === 1 && after[0] === mainBefore);

  const conversation = await runCauseEffect(agents.readConversation(agent.id));
  report.routineAnswersInChat = conversation.messages
    .map((message) => message.text)
    .filter((text) => text.startsWith("Routine report"));
  pass("The routine answers show in the chat", report.routineAnswersInChat.length === 2);

  await send(agent.id, "What did the routine find?", "chat turn after the routines");
  const next = fakeEntries()
    .filter((entry) => entry.method === "turn/start" && entry.threadId === mainBefore)
    .at(-1);
  assert.ok(next?.text);
  const answers = ["Routine report 1", "Routine report 2"].filter((answer) => next.text?.includes(answer));
  report.digestInNextTurn = { marker: next.text.includes("routine run in its own session"), answers };
  pass("The next chat turn reads both routine answers", report.digestInNextTurn.marker && answers.length === 2);
  pass(
    "The chat did not resume or restart its session",
    !fakeEntries().some((entry) => entry.method === "thread/resume") &&
      report.threadStarts.filter((entry) => entry.ephemeral !== true).length === 1,
  );
  report.passed = true;
} catch (error) {
  report.error = error instanceof Error ? (error.stack ?? error.message) : String(error);
  process.exitCode = 1;
} finally {
  await runCauseEffect(agents.stop()).catch(() => {});
  closeReader();
  database.close();
  mkdirSync(OUT, { recursive: true });
  writeFileSync(join(OUT, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  await rm(root, { recursive: true, force: true });
  process.stdout.write(`${report.passed ? "PASS" : "FAIL"} ${join(OUT, "report.json")}\n`);
  if (report.error) process.stdout.write(`${report.error}\n`);
}
