// Runs provider history through decoding, the real history importer and SQLite, then reopens it.
// Run with `node --import tsx scripts/response-process-e2e.ts` on Node 24 or newer.
// No account or external provider is used. A local fake Codex app-server drives the live turn.
// Writes .openbot-build/response-process-e2e/report.json.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { AGENT_PROVIDERS } from "@openbot/contracts/ipc";
import { Effect } from "effect";
import { toThreadItem } from "../src/backend/agent/thread-items";
import type { AgentBrowserHost } from "../src/backend/agent/turn-lifecycle";
import { AgentStore } from "../src/backend/agent-store";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { OpenBotDatabase } from "../src/backend/openbot-database";
import { decodeThreadResponse, type ThreadItem } from "../src/backend/protocol";
import type { ProviderHistoryFragment, ReadProviderHistory } from "../src/backend/provider-history";
import { importProviderHistory } from "../src/backend/provider-history-import";

const out = resolve(".openbot-build/response-process-e2e");
const root = await mkdtemp(join(tmpdir(), "openbot-process-e2e-"));
const home = join(root, "home");
const data = join(root, "data");
const fakeCodex = join(root, "codex");
const previousEnvironment = {
  HOME: process.env.HOME,
  CODEX_HOME: process.env.CODEX_HOME,
  OPENBOT_CODEX_PATH: process.env.OPENBOT_CODEX_PATH,
};
let database = new OpenBotDatabase(data);
const acceptance: Record<string, boolean> = {};

const FAKE_CODEX = `#!/usr/bin/env node
if (process.argv.includes("--version")) {
  process.stdout.write("codex-cli 0.162.1\\n");
  process.exit(0);
}
let buffer = "";
let turns = 0;
process.stdout.on("error", (error) => {
  if (error.code === "EPIPE") process.exit(0);
  throw error;
});
const write = (message) => process.stdout.write(JSON.stringify(message) + "\\n");
const config = { config: { mcp_servers: {} }, layers: [{ name: { type: "user", file: "/fake/.codex/config.toml", profile: null }, version: "1", config: { mcp_servers: {} } }] };
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
    case "thread/start": return write({ id: message.id, result: { thread: { id: "process-thread", turns: [] } } });
    case "thread/resume": return write({ id: message.id, result: { thread: { id: params.threadId, turns: [] } } });
    case "thread/unsubscribe": return write({ id: message.id, result: { status: "Unsubscribed" } });
    case "thread/turns/list":
    case "thread/items/list": return write({ id: message.id, result: { data: [], nextCursor: null } });
    case "thread/read": return write({ id: message.id, result: { thread: { id: params.threadId, turns: [] } } });
    case "turn/start": {
      const turnId = "process-turn-" + (++turns);
      write({ id: message.id, result: { turn: { id: turnId, status: "inProgress", items: [] } } });
      write({ method: "turn/started", params: { threadId: params.threadId, turn: { id: turnId } } });
      setImmediate(() => {
        const itemId = "streamed-note-" + turnId;
        write({ method: "item/started", params: { threadId: params.threadId, turnId, item: { type: "reasoning", id: itemId, summary: [] } } });
        write({ method: "item/reasoning/summaryTextDelta", params: { threadId: params.threadId, turnId, itemId, delta: "Keep the streamed note." } });
        write({ method: "item/completed", params: { threadId: params.threadId, turnId, item: { type: "reasoning", id: itemId, summary: [] } } });
        write({ method: "item/completed", params: { threadId: params.threadId, turnId, item: { type: "agentMessage", id: "answer-" + turnId, text: "Done.", phase: "final_answer" } } });
        write({ method: "turn/completed", params: { threadId: params.threadId, turn: { id: turnId, status: "completed" } } });
      });
      return;
    }
    case "turn/interrupt": return write({ id: message.id, result: {} });
    default: return write({ id: message.id, error: { code: -32601, message: "Method not found: " + message.method } });
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

async function waitFor<T>(label: string, read: () => T | null | undefined | false, timeoutMs = 20_000): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = read();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${label}.`);
    await new Promise((done) => setTimeout(done, 25));
  }
}

async function verifyLiveCompletion(store: AgentStore): Promise<void> {
  process.env.HOME = home;
  const codexHome = join(home, ".codex");
  process.env.CODEX_HOME = codexHome;
  process.env.OPENBOT_CODEX_PATH = fakeCodex;
  await mkdir(codexHome, { recursive: true });
  await writeFile(fakeCodex, FAKE_CODEX, { mode: 0o755 });
  const { AgentService } = await import("../src/backend/agent-service");
  const { MailboxStore } = await import("../src/backend/mailbox-store");
  const mailbox = new MailboxStore(data, store.sharedRoot, store.database);
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
  const agents = new AgentService({
    store,
    mailbox,
    browser,
    preferredProvider: "codex",
    offProviders: AGENT_PROVIDERS.filter((provider) => provider !== "codex"),
  });
  try {
    await runCauseEffect(agents.initialize());
    const agent = await runCauseEffect(
      agents.createAgent({
        name: "Live process",
        description: "Checks live process updates.",
        avatarSeed: "live-process",
        avatarHue: null,
        initialMessage: "Start the process check.",
      }),
    );
    const saved = await waitFor("the live response process", () => {
      const threadId = store.list().find((candidate) => candidate.id === agent.id)?.threadId;
      if (!threadId) return null;
      const conversation = database.readConversation(agent.id, threadId);
      const note = conversation.messages.find((message) => message.text === "Keep the streamed note.");
      const answer = conversation.messages.find((message) => message.text === "Done.");
      const pending = agents
        .listQueue(agent.id)
        .deliveries.some((delivery) => ["queued", "starting", "running"].includes(delivery.status));
      return note?.status === "completed" && answer?.status === "completed" && !pending ? { note, answer } : null;
    });
    assert.equal(saved.note.text, "Keep the streamed note.");
    acceptance.liveEmptyCompletionKeepsStreamedNote = true;
  } finally {
    await runCauseEffect(agents.stop()).catch(() => {});
    for (const [key, value] of Object.entries(previousEnvironment)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

try {
  const store = new AgentStore(data, home, database);
  await runCauseEffect(store.initialize());
  const agent = await runCauseEffect(store.getOrCreate("process-agent", "Process check", "Process check"));
  const threadId = await runCauseEffect(store.ensureThreadId(agent.id));
  await verifyLiveCompletion(store);
  const payloads = [
    { id: "string-summary", summary: ["String summary"], content: ["Do not replace the summary"] },
    { id: "block-summary", summary: [{ type: "summary_text", text: "Block summary" }], encrypted_content: "opaque" },
    { id: "block-content", content: [{ type: "reasoning_text", text: "Readable content" }] },
    { id: "pi-text", text: "Pi process note", summary: [{ type: "summary_text", text: "Pi process note" }] },
    { id: "text-only", text: "Text-only process note" },
    { id: "encrypted-only", encrypted_content: "opaque" },
  ];
  const expected = ["String summary", "Block summary", "Readable content", "Pi process note", "Text-only process note"];
  // Both live notifications and saved history use the same readable content and classification.
  const live = payloads.map((value) => toThreadItem({ ...value, type: "reasoning" }));
  const decoded = decodeThreadResponse({
    thread: {
      id: "external-thread",
      turns: [
        {
          id: "history-turn",
          status: "completed",
          items: [
            ...payloads.map((value) => ({ ...value, type: "reasoning" })),
            { id: "answer", type: "agentMessage", text: "Final answer", phase: "final_answer" },
          ],
        },
      ],
    },
  });
  const turns = decoded.thread.turns;
  assert(turns?.[0]);
  const items: ThreadItem[] = turns[0].items ?? [];
  assert.deepEqual(
    live.flatMap((item) => (item?.text ? [item.text] : [])),
    expected,
  );
  assert.deepEqual(
    items.filter((item) => item.phase === "commentary").flatMap((item) => (item.text ? [item.text] : [])),
    expected,
  );
  acceptance.liveAndHistoryAgree = true;
  const emptyCompletion = toThreadItem({ id: "empty-completion", type: "reasoning", summary: [] });
  assert.equal(emptyCompletion?.text, undefined);
  acceptance.emptyReasoningItemHasNoReplacementText = true;
  const fragment: ProviderHistoryFragment = {
    turnId: "history-turn",
    status: "completed",
    startedAt: 1,
    itemOffset: 0,
    complete: true,
    items,
  };
  const readHistory: ReadProviderHistory = (_request, consume) =>
    Effect.gen(function* () {
      yield* consume(fragment);
    });
  const importHistory = () =>
    runCauseEffect(
      importProviderHistory({
        database,
        readHistory,
        sessionId: "process-session",
        provider: "codex",
        externalSessionId: "external-thread",
        agentId: agent.id,
        publicThreadId: threadId,
        findDelivery: () => null,
        findMessageDelivery: () => null,
        quietRoutineDelivery: () => false,
      }),
    );
  await importHistory();
  database.close();
  database = new OpenBotDatabase(data);
  await runCauseEffect(database.initialize());
  const stored = database.readConversation(agent.id, threadId).messages;
  assert.deepEqual(
    stored.filter((message) => message.itemType === "commentary").map((message) => message.text),
    expected,
  );
  assert.equal(stored.find((message) => message.id === "answer")?.text, "Final answer");
  acceptance.reopenedHistoryKeepsProcessAndAnswer = true;
  assert(!JSON.stringify(stored).includes("opaque"));
  acceptance.encryptedContentIsNotDisplayed = true;
  const search = database.searchConversationMessages("Readable content", agent.id, undefined, 100);
  assert.deepEqual(
    search.results.map((result) => result.message.id),
    ["block-content"],
  );
  acceptance.processNotesAreSearchable = true;
  const repeat = await importHistory();
  assert.equal(repeat.messages, 0);
  acceptance.historyReimportDoesNotDuplicate = true;
  await mkdir(out, { recursive: true });
  await writeFile(join(out, "history.json"), `${JSON.stringify(stored, null, 2)}\n`);
  await writeFile(join(out, "report.json"), `${JSON.stringify({ passed: true, acceptance }, null, 2)}\n`);
  // Machine-readable: the E2E runner reports its artifact path.
  process.stdout.write(`${JSON.stringify({ passed: true, acceptance, out })}\n`);
} finally {
  database.close();
  for (const [key, value] of Object.entries(previousEnvironment)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  await rm(root, { recursive: true, force: true });
}
