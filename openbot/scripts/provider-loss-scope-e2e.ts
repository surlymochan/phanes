// Checks that the loss of a provider process ends only the work of that process (plan 006), that the
// warm generation clients of the lost provider end with it, and that the agents of another process
// keep their compaction and their browser control session.
// Runs the real agent service on a temporary profile with a fake Codex app-server. Three real
// processes of the fake run at once: the provider's shared process, the process of an agent that
// has an account of its own, and the process of a warm generation.
// The compaction of the lost process ends as "failed", its queued message reaches a final state, the
// service does not clear the browser controls of every agent, the warm generation process of the lost
// provider stops, and the agent on its own process keeps its compaction and its control session.
// `bun scripts/provider-loss-scope-e2e.ts`. Writes .openbot-build/provider-loss-scope-e2e/report.json.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { AGENT_PROVIDERS } from "@openbot/contracts/ipc";
import { Effect } from "effect";
import type { AgentBrowserHost } from "../src/backend/agent/turn-lifecycle";
import { controlSessionId } from "../src/backend/browser-control";
import { getString, isRecord } from "../src/backend/protocol";

const OUT = resolve(import.meta.dirname, "../.openbot-build/provider-loss-scope-e2e");
const root = await mkdtemp(join(tmpdir(), "openbot-provider-loss-"));
const userData = join(root, "user-data");
const home = join(root, "home");
const accountsRoot = join(root, "provider-accounts");
const logPath = join(root, "protocol.jsonl");
const controlPath = join(root, "control.json");
const exitedPath = join(root, "exited");
/** The `.cjs` name keeps the fake a CommonJS script, also when the temp folder sits in a package
 * whose package.json sets "type": "module". */
const fakeCodex = join(root, "codex.cjs");
/** The home folder of the provider's own sign-in. An account of its own reads another folder. */
const sharedCodexHome = join(home, ".codex");
/** The message that makes the fake call a browser tool and keep the turn open. */
const BROWSER_MARK = "KEEP-THE-BROWSER-CONTROL-OPEN";
/** The message that makes the fake report context usage, so the turn that ends compacts. */
const USAGE_MARK = "REPORT-CONTEXT-USAGE";

// Before the service modules load: the Codex process and the stores read the home folder.
process.env.HOME = home;
process.env.CODEX_HOME = sharedCodexHome;
process.env.OPENBOT_CODEX_PATH = fakeCodex;
process.env.OPENBOT_FAKE_CODEX_LOG = logPath;
process.env.OPENBOT_FAKE_CODEX_CONTROL = controlPath;
process.env.OPENBOT_FAKE_CODEX_EXITED = exitedPath;
mkdirSync(home, { recursive: true });

const { AgentService } = await import("../src/backend/agent-service");
const { AgentStore } = await import("../src/backend/agent-store");
const { ProviderAccountStore } = await import("../src/backend/agent/provider-account-store");
const { runCauseEffect } = await import("../src/backend/effect-boundary");
const { MailboxStore } = await import("../src/backend/mailbox-store");
const { OpenBotDatabase } = await import("../src/backend/openbot-database");

const FAKE_CODEX = `#!/usr/bin/env node
const fs = require("node:fs");
if (process.argv.includes("--version")) {
  process.stdout.write("codex-cli 0.160.1\\n");
  process.exit(0);
}
const log = (entry) => fs.appendFileSync(process.env.OPENBOT_FAKE_CODEX_LOG, JSON.stringify(entry) + "\\n");
const control = () => JSON.parse(fs.readFileSync(process.env.OPENBOT_FAKE_CODEX_CONTROL, "utf8"));
// The provider's own process reads the default home; an agent with an account of its own reads another.
const shared = process.env.CODEX_HOME === ${JSON.stringify(sharedCodexHome)};
const config = { config: { mcp_servers: {} }, layers: [{ name: { type: "user", file: "/fake/.codex/config.toml", profile: null }, version: "1", config: { mcp_servers: {} } }] };
let buffer = "";
let turns = 0;
/** threadId -> { turnId, finish } of the compaction turn that runs. */
const compacting = new Map();
log({ pid: process.pid, event: "start", home: process.env.CODEX_HOME, shared });
process.stdout.on("error", (error) => {
  if (error.code === "EPIPE") process.exit(0);
  throw error;
});
const write = (message) => process.stdout.write(JSON.stringify(message) + "\\n");
const answer = (message) => {
  const params = message.params || {};
  switch (message.method) {
    case "initialize": return write({ id: message.id, result: {} });
    case "account/read": return write({ id: message.id, result: { account: { type: "chatgpt", email: shared ? "codex@example.com" : "team-b@example.com" } } });
    case "account/rateLimits/read": return write({ id: message.id, result: { rateLimits: { limitId: "codex", primary: null, secondary: null }, rateLimitsByLimitId: null } });
    case "model/list": return write({ id: message.id, result: { data: [{ model: "gpt-5.5", displayName: "GPT-5.5" }] } });
    case "plugin/list": return write({ id: message.id, result: { marketplaces: [] } });
    case "config/read": return write({ id: message.id, result: config });
    case "config/batchWrite": return write({ id: message.id, result: {} });
    case "thread/start": return write({ id: message.id, result: { thread: { id: "thread-" + process.pid + "-" + (++turns), turns: [] } } });
    case "thread/resume": return write({ id: message.id, result: { thread: { id: params.threadId, turns: [] } } });
    case "thread/unsubscribe": return write({ id: message.id, result: { status: "Unsubscribed" } });
    case "thread/turns/list":
    case "thread/items/list": return write({ id: message.id, result: { data: [], nextCursor: null } });
    case "thread/read": return write({ id: message.id, result: { thread: { id: params.threadId, turns: [] } } });
    case "turn/start": {
      // Codex 0.160.1 \\\`start_or_steer_turn\\\`: an active compaction turn cannot take input.
      if (compacting.has(params.threadId)) {
        log({ refused: params.threadId });
        return write({ id: message.id, error: { code: -32603, message: "failed to submit turn input: ActiveTurnNotSteerable { turn_kind: Compact }" } });
      }
      const text = (params.input || []).map((item) => item.text || "").join(" ");
      log({ text });
      const turnId = "turn-" + process.pid + "-" + (++turns);
      write({ id: message.id, result: { turn: { id: turnId, status: "inProgress", items: [] } } });
      write({ method: "turn/started", params: { threadId: params.threadId, turn: { id: turnId } } });
      // The marked turn asks for the browser and never ends, so its control session stays open.
      if (text.includes(${JSON.stringify(BROWSER_MARK)})) {
        write({ id: "call-" + turnId, method: "item/tool/call", params: { threadId: params.threadId, turnId, callId: "call-" + turnId, namespace: "openbot_browser", tool: "list_tabs", arguments: {} } });
        return;
      }
      // Only a marked turn reports context usage. A turn without the mark leaves the context of its
      // thread unknown, so no turn of that thread compacts. The mark must not be a word of the
      // message alone: a turn that continues a conversation quotes the earlier messages, so the
      // mark of an earlier message would reach the later turn as well.
      if (text.includes(${JSON.stringify(USAGE_MARK)})) {
        write({ method: "thread/tokenUsage/updated", params: { threadId: params.threadId, turnId, tokenUsage: { total: { totalTokens: 90000 }, last: { totalTokens: 90000 }, modelContextWindow: 100000 } } });
      }
      setTimeout(() => {
        const item = { type: "agentMessage", id: "message-" + turnId, text: "Done.", phase: "final_answer" };
        write({ method: "item/completed", params: { threadId: params.threadId, turnId, item } });
        write({ method: "turn/completed", params: { threadId: params.threadId, turn: { id: turnId, status: "completed" } } });
      }, 50);
      return;
    }
    case "thread/compact/start": {
      const { compactionMs, ignoreInterrupt, exitDuringCompaction } = control();
      const turnId = "compact-turn-" + (++turns);
      const item = { type: "contextCompaction", id: "compact-item-" + turnId };
      write({ id: message.id, result: {} });
      // The provider's own process exits during its compaction. The restarted one finds the marker and
      // runs normally. The process of an agent with an account of its own keeps compacting.
      if (exitDuringCompaction && shared && !fs.existsSync(process.env.OPENBOT_FAKE_CODEX_EXITED)) {
        write({ method: "turn/started", params: { threadId: params.threadId, turn: { id: turnId } } });
        write({ method: "item/started", params: { threadId: params.threadId, turnId, item } });
        fs.writeFileSync(process.env.OPENBOT_FAKE_CODEX_EXITED, String(process.pid));
        log({ pid: process.pid, event: "exit", home: process.env.CODEX_HOME });
        process.stdout.write("", () => process.exit(1));
        return;
      }
      write({ method: "turn/started", params: { threadId: params.threadId, turn: { id: turnId } } });
      write({ method: "item/started", params: { threadId: params.threadId, turnId, item } });
      const finish = (status) => {
        if (compacting.get(params.threadId)?.turnId !== turnId) return;
        compacting.delete(params.threadId);
        clearTimeout(timer);
        log({ compactionEnded: turnId, threadId: params.threadId, status });
        if (status === "completed") write({ method: "item/completed", params: { threadId: params.threadId, turnId, item } });
        write({ method: "turn/completed", params: { threadId: params.threadId, turn: { id: turnId, status } } });
      };
      const timer = setTimeout(() => finish("completed"), compactionMs);
      compacting.set(params.threadId, { turnId, finish, ignoreInterrupt });
      return;
    }
    case "turn/interrupt": {
      write({ id: message.id, result: {} });
      const compaction = compacting.get(params.threadId);
      if (compaction?.turnId === params.turnId) {
        if (!compaction.ignoreInterrupt) compaction.finish("interrupted");
        return;
      }
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
      log({ pid: process.pid, method: message.method ?? null, id: message.id ?? null, params: message.params ?? null, home: process.env.CODEX_HOME });
      if (message.method && message.id !== undefined) answer(message);
    }
    newline = buffer.indexOf("\\n");
  }
});
`;

const checks: { name: string; passed: boolean; detail?: unknown }[] = [];
function check(name: string, passed: boolean, detail?: unknown) {
  checks.push({ name, passed, ...(detail === undefined ? {} : { detail }) });
}
const report: { passed: boolean; checks: typeof checks; error?: string; log?: unknown } = { passed: false, checks };

/** The control sessions the browser host holds, by thread and turn. */
const controlSessions = new Map<string, { threadId: string; turnId: string }>();
/** The control sessions that ended, by thread and turn. */
const endedControls: { threadId: string; turnId: string }[] = [];
const calls = {
  clearControls: 0,
  browserTools: 0,
  endControl: endedControls,
};
const unused = () => Effect.die(new Error("Not used by this check."));
const browser: AgentBrowserHost = {
  listTabs: () => [],
  beginTakeover: unused,
  endTakeover: () => {},
  userControlTabFor: () => null,
  awaitUserControlRelease: unused,
  resolveUploadTarget: unused,
  // The real host begins a control session for a browser call and ends it when the turn ends.
  handleDynamicTool: (params) => {
    calls.browserTools += 1;
    controlSessions.set(controlSessionId(params.threadId, params.turnId), {
      threadId: params.threadId,
      turnId: params.turnId,
    });
    // The call never answers, so the session stays open while the turn runs.
    return Effect.never;
  },
  onChanged: () => () => {},
  onControlChanged: () => () => {},
  onDocumentChanged: () => () => {},
  clearControls: () => {
    calls.clearControls += 1;
    controlSessions.clear();
  },
  endControl: (threadId, turnId) => {
    calls.endControl.push({ threadId, turnId });
    controlSessions.delete(controlSessionId(threadId, turnId));
  },
  close: () => Effect.void,
  clearAgentSession: () => Effect.void,
};

interface LogEntry {
  pid: number;
  event: string | null;
  method: string | null;
  threadId: string | null;
  home: string | null;
  profileGeneration?: boolean;
}

/** The messages that OpenBot sent to the fake Codex. */
function protocol(): LogEntry[] {
  try {
    return readFileSync(logPath, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => protocolEntry(JSON.parse(line)));
  } catch {
    return [];
  }
}

function protocolEntry(value: unknown): LogEntry {
  const entry = isRecord(value) ? value : {};
  const params = isRecord(entry.params) ? entry.params : {};
  return {
    pid: typeof entry.pid === "number" ? entry.pid : 0,
    event: getString(entry, "event"),
    method: getString(entry, "method"),
    threadId: getString(params, "threadId"),
    home: getString(entry, "home"),
    profileGeneration: params.profileGeneration === true,
  };
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

/** Waits for a state, and records a failed check instead of a stop when it does not come. */
async function settle<T>(label: string, read: () => T | null | undefined | false, timeoutMs?: number) {
  return waitFor(label, read, timeoutMs).catch(() => null);
}

/** True while the process runs. */
function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** Waits for a process to end; a stopped client resolves before the kernel reaps it. */
async function ended(pid: number, timeoutMs = 15_000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (alive(pid) && Date.now() < deadline) await new Promise((done) => setTimeout(done, 25));
  return !alive(pid);
}

await writeFile(fakeCodex, FAKE_CODEX, { mode: 0o755 });
await writeFile(
  controlPath,
  JSON.stringify({ compactionMs: 60_000, ignoreInterrupt: false, exitDuringCompaction: true }),
);
const database = new OpenBotDatabase(userData);
const store = new AgentStore(userData, home, database);
const mailbox = new MailboxStore(userData, store.sharedRoot, store.database);
const accounts = new ProviderAccountStore(join(userData, "openbot-provider-accounts-v1.json"), accountsRoot);
await runCauseEffect(accounts.load());
const second = await runCauseEffect(accounts.create("codex"));
if (!second) throw new Error("The second Codex account was not created.");
const agents = new AgentService({
  store,
  mailbox,
  browser,
  providerAccounts: accounts,
  preferredProvider: "codex",
  offProviders: AGENT_PROVIDERS.filter((provider) => provider !== "codex"),
  contextCompactionTimeoutMs: 30_000,
});

/** True while no delivery of the agent runs. */
const idle = (agentId: string) =>
  agents
    .listQueue(agentId)
    .deliveries.every((delivery) => !["queued", "starting", "running"].includes(delivery.status));

try {
  await runCauseEffect(agents.initialize());
  await waitFor("the second account to connect", () =>
    agents
      .getStatus()
      .providers?.find((row) => row.id === "codex")
      ?.accounts?.some((account) => account.id === second.id && account.state === "connected"),
  );

  const create = (name: string, initialMessage: string) =>
    runCauseEffect(
      agents.createAgent({
        name,
        description: `Checks the work of a lost provider process. ${name}.`,
        avatarSeed: name.toLowerCase(),
        avatarHue: null,
        initialMessage,
      }),
    );

  // The agent with an account of its own, and the agent whose turn keeps a browser control session
  // open. Their first turns report no context usage, so the second turn of the first one compacts
  // on the process of its own account.
  const confinedAgent = await create("Confined", "First message.");
  const browserAgent = await create("Browser", "First message.");
  await waitFor("the first turns to end", () => idle(confinedAgent.id) && idle(browserAgent.id));

  // An agent with an account of its own runs on a process of its own.
  await runCauseEffect(agents.updateAgent({ agentId: confinedAgent.id, providerAccountId: second.id }));
  await runCauseEffect(agents.updateAgent({ agentId: browserAgent.id, providerAccountId: second.id }));

  // The compaction of the agent on its own process runs on the process of its account.
  const confinedReceipt = await runCauseEffect(
    agents.sendMessage({ agentId: confinedAgent.id, text: `${USAGE_MARK} Second message.` }),
  );
  const confinedDeliveryId = confinedReceipt.deliveries[0]?.id;
  if (!confinedDeliveryId) throw new Error("The second message of the confined agent has no delivery.");
  await waitFor("the compaction of the agent on its own process", () =>
    protocol().some((line) => line.method === "thread/compact/start" && line.home !== sharedCodexHome),
  );

  // The browser call of the agent on its own process keeps its control session open.
  await runCauseEffect(
    agents.sendMessage({ agentId: browserAgent.id, text: `${BROWSER_MARK} Keep the control session open.` }),
  );
  await waitFor("the browser call of the agent on its own process", () => calls.browserTools > 0);

  // A warm generation starts a process of its own, owned by the provider's process.
  const warmText = await runCauseEffect(
    agents.generateWorkflowText({ provider: "codex", model: null, effort: null, prompt: "Say ok." }),
  );
  const warmPid = protocol().find((line) => line.method === "thread/start" && line.profileGeneration)?.pid;
  if (!warmPid) throw new Error("The warm generation started no process.");
  check("the warm generation answers on a process of its own", warmText === "Done." && alive(warmPid), {
    warmText,
    warmPid,
  });

  // The agent on the provider's own sign-in comes last, so its compaction is the one that the loss
  // ends. Its first turn reports 90% of the context window, so the compaction starts with it.
  const sharedAgent = await create("Shared", `${USAGE_MARK} First message.`);
  const threadId = await waitFor("the first turn to end", () => {
    const session = store.activeProviderSession(sharedAgent.id)?.externalSessionId;
    const started = protocol().some((line) => line.method === "turn/start" && line.threadId === session);
    return session && started && idle(sharedAgent.id) ? session : null;
  });
  await waitFor("the compaction to start", () =>
    protocol().some((line) => line.method === "thread/compact/start" && line.threadId === threadId),
  );
  await waitFor("the fake Codex to exit", () => existsSync(exitedPath));

  // The message that waits in the queue of the lost provider reaches a final state.
  const receipt = await runCauseEffect(agents.sendMessage({ agentId: sharedAgent.id, text: "Second message." }));
  const deliveryId = receipt.deliveries[0]?.id;
  if (!deliveryId) throw new Error("The second message has no delivery.");
  const finalStatus = await settle("the second message to end", () => {
    const status = mailbox.getDelivery(deliveryId)?.delivery.status;
    return status === "completed" || status === "failed" || status === "interrupted" ? status : null;
  });
  // The message ends as "completed" or as "interrupted": boot recovery orphans every unresolved
  // delivery of the provider. The check proves only that the queue does not wait for a compaction that
  // cannot end.
  check("the queued message reaches a final state after the loss", finalStatus !== null, {
    finalStatus,
    error: mailbox.getDelivery(deliveryId)?.delivery.error ?? null,
  });

  const failed = await settle("the compaction to end as failed", () =>
    agents
      .getRuntimeSnapshot()
      .contextStates?.find((state) => state.agentId === sharedAgent.id && state.compaction?.status === "failed"),
  );
  check("the compaction of the lost process ends as failed", Boolean(failed), {
    contextStates: agents.getRuntimeSnapshot().contextStates?.filter((state) => state.agentId === sharedAgent.id),
  });
  check("the loss of one process does not clear the browser controls of every agent", calls.clearControls === 0, {
    clearControls: calls.clearControls,
  });

  // The warm generation belongs to the lost process, so it ends with it.
  check("the warm generation process of the lost provider stops", await ended(warmPid), {
    warmPid,
    alive: alive(warmPid),
  });

  // The agents of the other processes keep their work.
  const confinedState = agents.getRuntimeSnapshot().contextStates?.find((state) => state.agentId === confinedAgent.id);
  check(
    "the compaction of the agent on its own process survives the loss",
    confinedState?.compaction?.status === "running",
    {
      compaction: confinedState?.compaction ?? null,
    },
  );
  const browserThreadId = store.list().find((agent) => agent.id === browserAgent.id)?.threadId ?? null;
  const session = [...controlSessions.values()].find((entry) => entry.threadId === browserThreadId);
  check("the browser control session of the agent on its own process survives the loss", Boolean(session), {
    browserThreadId,
    session: session ?? null,
    sessions: [...controlSessions.values()],
    endControl: [...calls.endControl],
  });
  report.passed = checks.every((entry) => entry.passed);
} catch (error) {
  report.error = error instanceof Error ? (error.stack ?? error.message) : String(error);
  report.log = readFileSync(logPath, "utf8");
} finally {
  await runCauseEffect(agents.stop()).catch(() => {});
  database.close();
  mkdirSync(OUT, { recursive: true });
  writeFileSync(join(OUT, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  await rm(root, { recursive: true, force: true });
  for (const entry of checks) process.stdout.write(`${entry.passed ? "PASS" : "FAIL"} ${entry.name}\n`);
  if (report.error) process.stdout.write(`${report.error}\n`);
  process.stdout.write(`Report: ${join(OUT, "report.json")}\n`);
  if (!report.passed) process.exitCode = 1;
}
