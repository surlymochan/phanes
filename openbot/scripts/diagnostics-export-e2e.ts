// Checks the diagnostics export of the account menu. The real `exportDiagnostics` runs on a real
// profile: the real agent service, the real mailbox, the real trace file, the real resource
// monitor and the real update service. The profile holds two agents, a queued request, a running
// request and a failed request, and an MCP server with a header that carries a token. Each of them
// holds a marker value that the privacy promise names: an agent name, a description, a message, an
// email address, a visited URL, a token, a file path, a failure text and the path of the profile.
// The check reads the report that the export wrote and holds that it counts the agents, the queues,
// the MCP servers, the browser tabs and the trace spans, that it carries the privacy promise, that
// it writes the file for the user alone, and that it names none of the marker values. A last step
// puts a marker into a copy of the report and holds that the same scan finds it, so the scan cannot
// pass because it looks for nothing.
// The check also runs the real `exportOpenBotData` on the same temporary profile and holds that
// the ZIP it writes opens and holds the manifest below the backup folder. A last step writes the
// ZIP of the Linux path from a folder of synthetic data and holds that it needs no tool of the
// system, that it holds the folder itself and that it writes the file for the user alone. Linux
// shipped without the `ditto` tool that the export used on every platform that was not Windows.
// Run: `bun scripts/diagnostics-export-e2e.ts`.
// Writes .openbot-build/diagnostics-export-e2e/report.json.

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { mkdirSync, statSync, writeFileSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { BrowserControlSession, BrowserTab } from "@openbot/contracts/ipc";
import { translateFor } from "@openbot/i18n";
import { plugin } from "bun";
import { Effect } from "effect";
import { strFromU8, unzipSync } from "fflate";
import type { AgentBrowserHost } from "../src/backend/agent/turn-lifecycle";
import { AgentService } from "../src/backend/agent-service";
import { AgentStore } from "../src/backend/agent-store";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { MailboxStore } from "../src/backend/mailbox-store";
import { OpenBotDatabase } from "../src/backend/openbot-database";
import { SidebarLayoutStore } from "../src/backend/sidebar-layout-store";
import { StatementTimings } from "../src/main/resource-summary";
import { TraceFile } from "../src/main/trace-file";
import { createDisabledUpdateAdapter, UpdateService } from "../src/main/update-service";

const OUT = resolve(import.meta.dirname, "../.openbot-build/diagnostics-export-e2e");
const HOST_VERSION = "0.0.0-e2e";
/** The save dialog answers with the path in this variable. The Electron stub reads it. */
const SAVE_PATH_ENV = "OPENBOT_E2E_SAVE_PATH";
/** The IPC channel the trace records. A channel name is a fixed string, never user text. */
const TRACE_CHANNEL = "maintenance:export-diagnostics";
/** The name the manifest of a backup has inside the archive. */
const MANIFEST_NAME = "OpenBot Backup/openbot-data.json";

interface Step {
  name: string;
  passed: boolean;
  detail?: unknown;
  error?: string;
}

const report: { passed: boolean; steps: Step[] } = { passed: false, steps: [] };

async function step<T>(
  name: string,
  run: () => Promise<T>,
  summarize: (value: T) => unknown = (value) => value,
): Promise<T> {
  process.stdout.write(`- ${name}\n`);
  try {
    const value = await run();
    report.steps.push({ name, passed: true, detail: summarize(value) });
    return value;
  } catch (error) {
    report.steps.push({ name, passed: false, error: error instanceof Error ? error.message : String(error) });
    throw error;
  }
}

/** A value that `JSON.parse` gives. */
type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

function isRecord(value: unknown): value is { [key: string]: Json } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function object(value: unknown, label: string): { [key: string]: Json } {
  assert.ok(isRecord(value), `${label} is not an object.`);
  return value;
}

function member(value: { [key: string]: Json }, key: string, label: string): Json {
  const found = value[key];
  assert.ok(found !== undefined, `${label} has no ${key}.`);
  return found;
}

function list(value: unknown, label: string): Json[] {
  assert.ok(Array.isArray(value), `${label} is not a list.`);
  return value;
}

/** A marker value that the report must never name. Each one names the field it stands for. */
const marker = (label: string): string => `obe2e-${label}-${randomBytes(6).toString("hex")}`;

const CANARY = {
  agentName: marker("name"),
  agentDescription: marker("description"),
  message: marker("message"),
  email: `${marker("mail")}@example.invalid`,
  url: `https://example.invalid/${marker("url")}`,
  token: marker("token"),
  path: `/tmp/${marker("path")}/notes.txt`,
  mcpName: marker("mcp"),
  failure: marker("failure"),
};

/** The request the user sent. It holds the address, the page and the file of the promise. */
const MESSAGE_TEXT = `Send ${CANARY.email} the page ${CANARY.url} and the file ${CANARY.path}. ${CANARY.message}`;

/** The names of the markers that the report holds. The scan reports the field, not the value. */
function leaks(text: string): string[] {
  return Object.entries(CANARY)
    .filter(([, value]) => text.includes(value))
    .map(([label]) => label);
}

// The main process imports Electron for its windows and its dialogs. No check here opens a window:
// the save dialog answers with the path of the temporary profile, and the browser host of the
// report is a stand-in that counts two tabs and one control session.
plugin({
  name: "diagnostics-export-electron-boundary",
  setup(build) {
    class Inert {}
    build.module("electron", () => ({
      loader: "object",
      exports: {
        app: {
          getPath: () => tmpdir(),
          getVersion: () => HOST_VERSION,
          isPackaged: false,
          on: () => undefined,
          off: () => undefined,
        },
        BrowserWindow: Object.assign(class extends Inert {}, { getAllWindows: () => [] }),
        clipboard: {},
        dialog: {
          showSaveDialog: () => {
            const path = process.env[SAVE_PATH_ENV];
            if (!path) throw new Error(`The save dialog needs ${SAVE_PATH_ENV}.`);
            return { canceled: false, filePath: path };
          },
        },
        Menu: { buildFromTemplate: () => ({}) },
        powerMonitor: { on: () => undefined, off: () => undefined },
        session: { fromPartition: () => ({}) },
        WebContentsView: class extends Inert {},
        webContents: { getAllWebContents: () => [] },
      },
    }));
  },
});

// The modules below import Electron, so they load after the stub above.
const { BrowserHost } = await import("../src/backend/browser-host");
const { createBackupArchive } = await import("../src/main/backup-archive");
const { exportDiagnostics, exportOpenBotData } = await import("../src/main/maintenance-service");
const { ResourceMonitor } = await import("../src/main/resource-monitor");

const unused = () => Effect.die(new Error("Not used by this check."));
const agentBrowser: AgentBrowserHost = {
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
};

/** The tabs of the browser. The report counts them, and the address of a tab stays in the tab. */
const TABS: BrowserTab[] = [
  {
    id: "tab-e2e-1",
    title: CANARY.message,
    url: CANARY.url,
    loading: false,
    ownerThreadId: null,
    ownerAgentId: null,
  },
  {
    id: "tab-e2e-2",
    title: "Second tab",
    url: "https://example.invalid/second",
    loading: false,
    ownerThreadId: null,
    ownerAgentId: null,
  },
];

/** The one session that controls a tab. The report counts the sessions, it holds none of them. */
const CONTROL_SESSIONS: BrowserControlSession[] = [
  {
    id: "control-e2e-1",
    threadId: "thread-e2e-1",
    turnId: "turn-e2e-1",
    callId: "call-e2e-1",
    tabId: "tab-e2e-1",
    action: "click",
    phase: "acting",
    startedAt: new Date().toISOString(),
  },
];

const root = await mkdtemp(join(tmpdir(), "openbot-diagnostics-export-e2e-"));
const userData = join(root, "user-data");
const home = join(root, "home");
const savePath = join(root, "diagnostics.json");
/** The path the save dialog answers with when the backup export asks for one. */
const backupPath = join(root, "backup.zip");
process.env[SAVE_PATH_ENV] = savePath;

interface Host {
  agents: AgentService;
  mailbox: MailboxStore;
  database: OpenBotDatabase;
  /** The ids of the two agents of the profile, in the order they were created. */
  agentIds: string[];
}

let host: Host | null = null;

/** The running profile of this check. */
function current(): Host {
  assert.ok(host, "The profile is not running.");
  return host;
}

/** Stops the agent service and closes the database of the profile. */
async function stopHost(): Promise<void> {
  if (!host) return;
  await runCauseEffect(host.agents.stop()).catch(() => undefined);
  host.database.close();
}

try {
  await step("the profile holds two agents, three requests and an MCP server", async () => {
    const database = new OpenBotDatabase(userData);
    const store = new AgentStore(userData, home, database);
    const mailbox = new MailboxStore(userData, store.sharedRoot, database);
    const sidebar = new SidebarLayoutStore(join(userData, "sidebar-layout.json"));
    await runCauseEffect(sidebar.initialize());
    await runCauseEffect(store.initialize());
    await runCauseEffect(mailbox.initialize());
    const agents = new AgentService({ store, mailbox, browser: agentBrowser, sidebarLayout: sidebar });
    host = { agents, mailbox, database, agentIds: [] };

    const first = await runCauseEffect(
      agents.createAgentProfile({
        name: CANARY.agentName,
        description: CANARY.agentDescription,
        avatarSeed: "diagnostics-a",
        avatarHue: null,
      }),
    );
    const second = await runCauseEffect(
      agents.createAgentProfile({
        name: "Second agent",
        description: "A second agent for the queue counts",
        avatarSeed: "diagnostics-b",
        avatarHue: null,
      }),
    );
    host.agentIds = [first.id, second.id];

    const queued = await runCauseEffect(
      mailbox.enqueue({ sender: { kind: "user" }, recipientAgentIds: [first.id], text: MESSAGE_TEXT }),
    );
    const running = await runCauseEffect(
      mailbox.enqueue({ sender: { kind: "user" }, recipientAgentIds: [first.id], text: "A second request" }),
    );
    const failed = await runCauseEffect(
      mailbox.enqueue({ sender: { kind: "user" }, recipientAgentIds: [first.id], text: "A third request" }),
    );
    const queuedId = queued.deliveries[0]?.id;
    const runningId = running.deliveries[0]?.id;
    const failedId = failed.deliveries[0]?.id;
    assert.ok(queuedId && runningId && failedId, "Each request has a delivery.");
    await runCauseEffect(mailbox.markStarting(runningId));
    await runCauseEffect(mailbox.markRunning(runningId, "turn-e2e-1"));
    await runCauseEffect(mailbox.markStarting(failedId));
    await runCauseEffect(mailbox.markRunning(failedId, "turn-e2e-2"));
    await runCauseEffect(
      mailbox.markTerminal(failedId, "failed", `The provider refused the request ${CANARY.failure}`),
    );

    await runCauseEffect(
      agents.saveMcpServer({
        config: {
          id: "",
          name: CANARY.mcpName,
          transport: "http",
          enabled: true,
          command: "",
          args: [],
          env: [],
          envPassthrough: [],
          workingDirectory: "",
          url: CANARY.url,
          headers: [{ key: "Authorization", value: `Bearer ${CANARY.token}` }],
        },
      }),
    );
    return { agents: host.agentIds };
  });

  await step("the profile holds every marker of the privacy promise", async () => {
    const service = current().agents;
    const listed = service.listAgents();
    const first = listed.find((agent) => agent.id === current().agentIds[0]);
    assert.ok(first, "The profile holds an agent.");
    assert.equal(first.name, CANARY.agentName, "The agent does not hold its name.");
    assert.equal(first.description, CANARY.agentDescription, "The agent does not hold its description.");
    const queue = service.listQueue(first.id);
    const texts = queue.deliveries.map((delivery) => delivery.text);
    assert.ok(
      texts.some((text) => text.includes(CANARY.email)),
      "The queue does not hold the address.",
    );
    assert.ok(
      texts.some((text) => text.includes(CANARY.url)),
      "The queue does not hold the address of the page.",
    );
    assert.ok(
      texts.some((text) => text.includes(CANARY.path)),
      "The queue does not hold the path of the file.",
    );
    assert.ok(
      texts.some((text) => text.includes(CANARY.message)),
      "The queue does not hold the message.",
    );
    const failure = queue.deliveries.find((delivery) => delivery.status === "failed");
    assert.ok(failure?.error, "The failed request holds no failure text.");
    assert.ok(failure.error.includes(CANARY.failure), "The failure text does not hold its marker.");
    const servers = service.listMcpServers();
    assert.equal(servers.length, 1, "The profile holds no MCP server.");
    const server = servers[0];
    assert.ok(server, "The profile holds no MCP server.");
    assert.equal(server.name, CANARY.mcpName, "The MCP server does not hold its name.");
    assert.ok(
      server.headers.some((header) => header.value.includes(CANARY.token)),
      "The MCP server does not hold the token.",
    );
    return { agents: listed.length, deliveries: queue.deliveries.length, servers: servers.length };
  });

  const reportText = await step(
    "the export writes the report where the save dialog chose",
    async () => {
      const service = current().agents;
      const trace = new TraceFile({ directory: userData });
      trace.record({ kind: "ipc", name: TRACE_CHANNEL, durationMs: 12, outcome: "ok" });
      trace.record({ kind: "turn", name: "user", durationMs: 40, outcome: "completed" });
      const resources = new ResourceMonitor({
        userDataPath: userData,
        server: false,
        trace,
        statementTimings: new StatementTimings(),
        tabProcessIds: () => [],
      });
      const updater = new UpdateService(createDisabledUpdateAdapter(), {
        currentVersion: HOST_VERSION,
        enabled: true,
        autoDownload: false,
        beforeInstall: async () => undefined,
      });
      // The browser host of the report: it counts the tabs and the control sessions, and no check
      // here opens a tab.
      const browser: InstanceType<typeof BrowserHost> = Object.create(BrowserHost.prototype);
      browser.listTabs = () => TABS;
      browser.getControlState = () => ({ sessions: CONTROL_SESSIONS });

      const result = await runCauseEffect(
        exportDiagnostics({
          service,
          browser,
          updater,
          trace,
          resources,
          parentWindow: null,
          translate: translateFor("en"),
        }),
      );
      assert.equal(result.saved, true, "The export saved nothing.");
      const mode = statSync(savePath).mode & 0o777;
      assert.equal(mode, 0o600, `The report is readable by others: ${mode.toString(8)}.`);
      const text = await readFile(savePath, "utf8");
      assert.ok(text.length > 0, "The report is empty.");
      return text;
    },
    (text) => ({ bytes: text.length, mode: "600" }),
  );

  await step("the report counts the agents, the queues, the servers, the tabs and the spans", async () => {
    const written = object(JSON.parse(reportText), "The report");
    assert.equal(member(written, "schemaVersion", "The report"), 4, "The report has another schema version.");
    const application = object(member(written, "application", "The report"), "The application of the report");
    assert.equal(member(application, "version", "The application"), HOST_VERSION);
    assert.equal(member(application, "platform", "The application"), process.platform);
    assert.equal(member(application, "packaged", "The application"), false);

    const agent = object(member(written, "agent", "The report"), "The agent of the report");
    assert.equal(member(agent, "agentCount", "The agent"), 2, "The report counts another number of agents.");
    const queues = list(member(agent, "queues", "The agent"), "The queues of the report");
    assert.equal(queues.length, 2, "The report holds another number of queues.");
    const counted = queues.map((row) => object(row, "A queue of the report"));
    const busy = counted.find((row) => member(row, "agentId", "A queue") === current().agentIds[0]);
    assert.ok(busy, "The report holds no queue of the first agent.");
    const deliveries = object(member(busy, "deliveries", "A queue"), "The deliveries of a queue");
    assert.equal(member(deliveries, "queued", "The deliveries"), 1, "The report counts another queued request.");
    assert.equal(member(deliveries, "running", "The deliveries"), 1, "The report counts another running request.");
    assert.equal(member(deliveries, "failed", "The deliveries"), 1, "The report counts another failed request.");
    assert.equal(member(deliveries, "completed", "The deliveries"), 0);
    const idle = counted.find((row) => member(row, "agentId", "A queue") === current().agentIds[1]);
    assert.ok(idle, "The report holds no queue of the second agent.");
    const idleDeliveries = object(member(idle, "deliveries", "A queue"), "The deliveries of an idle queue");
    assert.equal(
      Object.values(idleDeliveries).every((count) => count === 0),
      true,
      "The report counts a request of an agent that holds none.",
    );

    const servers = object(member(written, "mcpServers", "The report"), "The MCP servers of the report");
    assert.equal(member(servers, "count", "The MCP servers"), 1, "The report counts another number of servers.");
    assert.equal(member(servers, "enabledCount", "The MCP servers"), 1);
    const rows = list(member(servers, "servers", "The MCP servers"), "The servers of the report");
    assert.equal(rows.length, 1, "The report holds another number of servers.");
    const row = object(rows[0], "A server of the report");
    // A name is user text and a configuration holds headers and environment values, so the report
    // holds the id, the transport and nothing else.
    assert.deepEqual(Object.keys(row).sort(), ["enabled", "mcpServerId", "transport"]);

    const browser = object(member(written, "browser", "The report"), "The browser of the report");
    assert.equal(member(browser, "tabCount", "The browser"), 2, "The report counts another number of tabs.");
    assert.equal(member(browser, "activeControlCount", "The browser"), 1);

    const resource = object(member(written, "resources", "The report"), "The resources of the report");
    const device = object(member(resource, "device", "The resources"), "The device of the report");
    assert.equal(member(device, "hostKind", "The device"), "desktop");
    const today = object(member(resource, "today", "The resources"), "The day of the report");
    assert.match(String(member(today, "day", "The day")), /^\d{4}-\d{2}-\d{2}$/);

    const update = object(member(written, "update", "The report"), "The update of the report");
    assert.equal(member(update, "phase", "The update"), "idle");
    assert.equal(member(update, "currentVersion", "The update"), HOST_VERSION);

    const spans = list(member(written, "trace", "The report"), "The trace of the report");
    assert.ok(spans.length > 0, "The report holds no trace span.");
    const names = spans.map((span) => String(member(object(span, "A span of the report"), "name", "A span")));
    assert.ok(names.includes(TRACE_CHANNEL), "The report holds no span of the recorded channel.");
    return { agents: 2, queues: queues.length, servers: rows.length, spans: spans.length };
  });

  await step("the report carries the privacy promise", async () => {
    const written = object(JSON.parse(reportText), "The report");
    const privacy = String(member(written, "privacy", "The report"));
    for (const promise of ["conversations", "URLs", "email addresses", "tokens", "file contents", "file paths"]) {
      assert.ok(privacy.includes(promise), `The privacy promise does not name ${promise}.`);
    }
    return { privacy };
  });

  await step("the report names no marker of the profile", async () => {
    const found = leaks(reportText);
    assert.deepEqual(found, [], `The report names ${found.join(", ")}.`);
    assert.ok(!reportText.includes(root), "The report names the path of the profile.");
    return { markers: Object.keys(CANARY).length };
  });

  await step("the scan finds a marker that a report holds", async () => {
    // The control: the same scan must fail when the report holds a marker, so the step above
    // cannot pass because the scan looks for nothing.
    const doctored = reportText.replace('"privacy"', `"privacy", "${CANARY.agentName}"`);
    assert.ok(doctored !== reportText, "The copy of the report holds no marker.");
    const found = leaks(doctored);
    assert.deepEqual(found, ["agentName"], "The scan did not find the marker of the copy.");
    return { found };
  });

  await step("the backup export writes a ZIP that a reader can open", async () => {
    process.env[SAVE_PATH_ENV] = backupPath;
    const result = await runCauseEffect(
      exportOpenBotData({
        service: current().agents,
        mailbox: current().mailbox,
        parentWindow: null,
        translate: translateFor("en"),
      }),
    );
    assert.equal(result.saved, true, "The backup export saved nothing.");
    const bytes = new Uint8Array(await readFile(backupPath));
    const entries = unzipSync(bytes);
    const manifest = entries[MANIFEST_NAME];
    assert.ok(manifest, `The archive holds no ${MANIFEST_NAME}.`);
    const written = object(JSON.parse(strFromU8(manifest)), "The manifest of the archive");
    assert.equal(member(written, "schemaVersion", "The manifest"), 4, "The manifest has another schema version.");
    const agents = list(member(written, "agents", "The manifest"), "The agents of the manifest");
    assert.equal(agents.length, 2, "The manifest counts another number of agents.");
    const queues = list(member(written, "queues", "The manifest"), "The queues of the manifest");
    assert.equal(queues.length, 2, "The manifest counts another number of queues.");
    return { bytes: bytes.byteLength, entries: Object.keys(entries) };
  });

  await step("the Linux backup archive needs no system tool and stays private", async () => {
    // The folder holds only synthetic data: a manifest and a file below it. The Linux path writes
    // the ZIP in the process, so the check holds that it needs no tool of the system and that the
    // archive holds the folder itself, as `ditto --keepParent` and `Compress-Archive` do.
    const folder = join(root, "synthetic", "OpenBot Backup");
    await mkdir(join(folder, "attachments"), { recursive: true });
    await writeFile(join(folder, "openbot-data.json"), `{"schemaVersion":4,"synthetic":true}\n`, "utf8");
    await writeFile(join(folder, "attachments", "note.txt"), "synthetic attachment\n", "utf8");
    const archive = join(root, "synthetic.zip");
    await runCauseEffect(createBackupArchive("linux", folder, archive));
    const bytes = new Uint8Array(await readFile(archive));
    const entries = unzipSync(bytes);
    const names = Object.keys(entries).sort();
    const manifestEntry = entries[MANIFEST_NAME];
    const attachmentEntry = entries["OpenBot Backup/attachments/note.txt"];
    assert.ok(manifestEntry, `The archive holds no ${MANIFEST_NAME}.`);
    assert.ok(attachmentEntry, "The archive holds no attachment.");
    assert.ok(
      names.every((name) => name.startsWith("OpenBot Backup/")),
      `The archive holds an entry outside the folder: ${names.join(", ")}.`,
    );
    assert.equal(
      strFromU8(manifestEntry),
      `{"schemaVersion":4,"synthetic":true}\n`,
      "The archive holds another manifest.",
    );
    assert.equal(strFromU8(attachmentEntry), "synthetic attachment\n", "The archive holds another attachment.");
    const mode = statSync(archive).mode & 0o777;
    assert.equal(mode, 0o600, `The archive is readable by others: ${mode.toString(8)}.`);
    return { mode: "600", entries: names.length };
  });

  report.passed = true;
} finally {
  await stopHost();
  await rm(root, { recursive: true, force: true });
  delete process.env[SAVE_PATH_ENV];
  mkdirSync(OUT, { recursive: true });
  writeFileSync(join(OUT, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  process.stdout.write(`Report: ${join(OUT, "report.json")}\n`);
}
