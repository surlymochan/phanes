// Opens a copy of a real `openbot.db` from upstream migration 47 through the desktop host startup path,
// then restarts the host. Upstream 47 already has projection_agent_threads and has dropped
// projection_agent_tasks. The source file is opened read-only (the app must be closed) and copied; only
// the copy changes. A forced failure at migration 48 must leave the upstream rows unchanged. Checks:
// sessions and legacy task ids survive both host starts, every other upstream row is retained, migration
// markers stay stable on restart, and the integrity and foreign-key checks pass.
//   bun test ./scripts/agent-threads-upgrade-e2e.ts
//   bun scripts/agent-threads-upgrade-e2e.ts --run-e2e --source=<openbot.db>
// Writes .volto-build/agent-threads-upgrade-e2e/report.json with counts only, no message text or titles.
import { test } from "bun:test";
import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { copyFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { isDeepStrictEqual, promisify } from "node:util";
import { _electron, type ElectronApplication } from "playwright-core";
import { AGENT_THREAD_MIGRATION_SQL } from "../src/backend/agent-threads/agent-thread-schema";
import { BROWSER_PROFILE_SCHEMA_SQL } from "../src/backend/browser-profiles/browser-profile-schema";
import { migrateVoltoDatabase } from "../src/backend/volto-database-schema";
import { WORKFLOW_SCHEMA_SQL } from "../src/backend/workflows/workflow-schema";
import { electronSafeStorageLaunchArguments, withoutElectronRuntimeFlags } from "./electron-spawn-env";

const OUT = resolve(import.meta.dirname, "../.volto-build/agent-threads-upgrade-e2e");
const ROOT = resolve(import.meta.dirname, "..");
const BASELINE_VERSION = 8;
const UPSTREAM_VERSION = 47;
const THREAD_VERSION = UPSTREAM_VERSION + 1;
const LATEST_VERSION = THREAD_VERSION + 1;
const E2E_TIMEOUT_MS = 240_000;
let build: Promise<unknown> | undefined;

/** Launch the real application entry, including createApplicationServices and AgentService.initialize. */
async function startHost(profile: string): Promise<ElectronApplication> {
  // The host acceptance command installs dependencies only. Build once if this is a fresh checkout.
  if (!existsSync(join(ROOT, "out/main/index.js"))) {
    build ??= promisify(execFile)(process.execPath, ["run", "build"], { cwd: ROOT, timeout: 180_000 });
    await build;
  }
  const host = await _electron.launch({
    executablePath: createRequire(import.meta.url)("electron"),
    args: [
      ...electronSafeStorageLaunchArguments(),
      ...(process.platform === "linux" ? ["--no-sandbox"] : []),
      ROOT,
      `--user-data-dir=${profile}`,
    ],
    cwd: ROOT,
    env: Object.fromEntries(
      Object.entries(withoutElectronRuntimeFlags(process.env)).flatMap(([name, value]) =>
        value === undefined ? [] : [[name, value]],
      ),
    ),
    timeout: 30_000,
  });
  try {
    assert.equal(await host.evaluate(({ app }) => app.getPath("userData")), profile);
    const page = await host.firstWindow();
    // This mark follows service initialization, IPC registration, and loading the real renderer.
    await page.waitForFunction(() => Boolean(window.volto), undefined, { timeout: 30_000 });
    const deadline = Date.now() + 30_000;
    while (!(await host.evaluate(() => performance.getEntriesByName("volto:renderer-loaded").length > 0))) {
      assert.ok(Date.now() < deadline, "the real host did not finish application startup");
      await new Promise((done) => setTimeout(done, 100));
    }
    return host;
  } catch (error) {
    await host.close();
    throw error;
  }
}
const THREAD_COLUMNS =
  "thread_id, agent_id, legacy_task_id, origin, title, title_source, outcome, error, created_at, updated_at, archived_at, last_event_sequence";

interface SourceReport {
  source: string;
  passed: boolean;
  error?: string;
  sourceVersion?: number;
  tables?: number;
  rows?: number;
  tasks?: number;
  orphanTasks?: number;
  archivedThreads?: number;
  taskMessages?: number;
  taskMessagesWithoutThread?: number;
  rolledBack?: boolean;
  hostStarted?: boolean;
  hostPid?: number;
  restartedHostPid?: number;
  hostRestarted?: boolean;
  retainedSessions?: number;
}

function version(db: DatabaseSync): number {
  const row = db.prepare("SELECT MAX(version) AS version FROM schema_migrations").get();
  return Number(row?.version ?? 0);
}

function tableNames(db: DatabaseSync): string[] {
  return db
    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    .all()
    .map((row) => String(row.name));
}

/** A hash of every row of each table, in a stable order, so that two states compare without the data. */
function tableHashes(db: DatabaseSync): Map<string, { rows: number; hash: string }> {
  const hashes = new Map<string, { rows: number; hash: string }>();
  for (const name of tableNames(db)) {
    if (name === "schema_migrations") continue;
    const rows = db.prepare(`SELECT * FROM "${name}"`).all();
    const lines = rows.map((row) =>
      JSON.stringify(row, (_key, value) => (typeof value === "bigint" ? `${value}` : value)),
    );
    lines.sort();
    hashes.set(name, { rows: rows.length, hash: createHash("sha256").update(lines.join("\n")).digest("hex") });
  }
  return hashes;
}

/** Runs every migration up to `last`; the next one must fail and roll back. */
function migrateUpTo(db: DatabaseSync, last: number): void {
  db.exec(`CREATE TEMP TRIGGER stop_at_version BEFORE INSERT ON schema_migrations WHEN new.version > ${last}
    BEGIN SELECT RAISE(ABORT, 'upgrade smoke stop'); END;`);
  try {
    assert.throws(() => migrateVoltoDatabase(db), new RegExp(`migration to version ${last + 1} failed`));
  } finally {
    db.exec("DROP TRIGGER stop_at_version");
  }
}

async function upgrade(source: string, workDirectory: string, index: number): Promise<SourceReport> {
  const report: SourceReport = { source, passed: false };
  const profile = join(workDirectory, `profile-${index}`);
  await mkdir(profile, { recursive: true });
  const copy = join(profile, "openbot.db");
  // A WAL database opens read-only only with its `-shm` file. A running app holds both files, and
  // `.backup` copies under a read lock. A closed app removes them: `immutable=1` then reads with no lock
  // and no file of its own, which is safe because no one writes.
  const uri = existsSync(`${source}-wal`) ? source : `file:${encodeURI(source)}?immutable=1`;
  try {
    if (!existsSync(`${source}-wal`)) await copyFile(source, copy);
    else execFileSync("sqlite3", ["-readonly", uri, `.backup '${copy.replaceAll("'", "''")}'`], { stdio: "pipe" });
  } catch (error) {
    report.error = `copy failed: ${error instanceof Error ? error.message : String(error)}`;
    return report;
  }

  const db = new DatabaseSync(copy);
  let application: ElectronApplication | undefined;
  let connection: DatabaseSync | undefined;
  try {
    report.sourceVersion = version(db);
    assert.equal(report.sourceVersion, UPSTREAM_VERSION, "the source is not released upstream 47");
    const sourceTables = tableNames(db);
    assert.ok(sourceTables.includes("projection_agent_threads"), "upstream 47 has agent threads");
    assert.ok(!sourceTables.includes("projection_agent_tasks"), "upstream 47 dropped the task table");
    assert.ok(!sourceTables.includes("routine_backoff"), "upstream 47 has no fork backoff table");

    const before = tableHashes(db);
    const retainedRows = new Map(tableNames(db).map((name) => [name, db.prepare(`SELECT * FROM "${name}"`).all()]));
    const sessions = db.prepare(`SELECT ${THREAD_COLUMNS} FROM projection_agent_threads ORDER BY thread_id`).all();
    assert.ok(sessions.length > 0, "the upstream database has no session data to preserve");
    assert.ok(
      sessions.some((session) => session.legacy_task_id !== null),
      "the upstream database has no legacy task data to preserve",
    );
    const threadIds = new Set(
      db
        .prepare("SELECT thread_id FROM projection_threads")
        .all()
        .map((row) => String(row.thread_id)),
    );
    const kept = sessions.filter((session) => threadIds.has(String(session.thread_id)));
    report.tasks = kept.filter((session) => session.legacy_task_id !== null).length;
    report.orphanTasks = sessions.length - kept.length;
    report.tables = before.size;
    report.rows = [...before.values()].reduce((sum, table) => sum + table.rows, 0);

    // A failure before recording 48 leaves the upstream database unchanged.
    migrateUpTo(db, UPSTREAM_VERSION);
    assert.equal(version(db), UPSTREAM_VERSION);
    assert.deepEqual(tableHashes(db), before, "a failed migration 48 changed a table");
    report.rolledBack = true;

    db.close();

    application = await startHost(profile);
    // Observe only: migrations and startup must run in the application process.
    connection = new DatabaseSync(copy, { readOnly: true });
    report.hostStarted = true;
    report.hostPid = application.process().pid;
    assert.notEqual(report.hostPid, process.pid, "the host must run in its own application process");
    const latestVersion = version(connection);
    assert.equal(latestVersion, LATEST_VERSION, `host stopped at migration ${latestVersion}`);
    assert.equal(
      connection.prepare("SELECT COUNT(*) AS count FROM projection_agent_threads").get()?.count,
      kept.length,
      "the host did not retain every upstream session",
    );
    report.retainedSessions = kept.length;

    // Startup can add import receipts and events. Every upstream row must still have the same values.
    for (const [name, rows] of retainedRows) {
      const columns = connection
        .prepare(`PRAGMA table_info("${name}")`)
        .all()
        .map((row) => String(row.name));
      const actual = connection.prepare(`SELECT * FROM "${name}"`).all();
      for (const row of rows) {
        assert.ok(
          actual.some((candidate) => columns.every((column) => isDeepStrictEqual(candidate[column], row[column]))),
          `host startup changed an upstream row in ${name}`,
        );
      }
    }
    const checkHostTasks = async () => {
      assert.ok(application);
      const page = await application.firstWindow();
      for (const agentId of new Set(kept.map((session) => String(session.agent_id)))) {
        const snapshot = await page.evaluate((id) => window.volto.agent.listTasks(id), agentId);
        assert.deepEqual(
          snapshot.tasks.map((task) => task.id).sort(),
          kept
            .filter((session) => session.agent_id === agentId)
            .map((session) => String(session.legacy_task_id ?? session.thread_id))
            .sort(),
          "the real host API did not retain upstream session and task ids",
        );
      }
    };
    await checkHostTasks();

    const threads = new Map(
      connection
        .prepare(`SELECT ${THREAD_COLUMNS} FROM projection_agent_threads`)
        .all()
        .map((row) => [String(row.thread_id), row]),
    );
    assert.equal(threads.size, kept.length, "the host changed the upstream side-thread count");
    for (const session of kept) {
      const thread = threads.get(String(session.thread_id));
      assert.ok(thread, "the host removed an upstream side thread");
      assert.deepEqual(
        [
          thread.thread_id,
          thread.agent_id,
          thread.legacy_task_id,
          thread.origin,
          thread.title,
          thread.title_source,
          thread.outcome,
          thread.error,
          thread.created_at,
          thread.updated_at,
          thread.archived_at,
          thread.last_event_sequence,
        ],
        [
          session.thread_id,
          session.agent_id,
          session.legacy_task_id,
          session.origin,
          session.title,
          session.title_source,
          session.outcome,
          session.error,
          session.created_at,
          session.updated_at,
          session.archived_at,
          session.last_event_sequence,
        ],
        "the host changed an upstream side thread",
      );
    }
    report.archivedThreads = [...threads.values()].filter((thread) => thread.archived_at !== null).length;

    const taskIds = connection
      .prepare(
        "SELECT json_extract(message_json, '$.taskId') AS task_id FROM projection_mailbox_messages WHERE json_extract(message_json, '$.taskId') IS NOT NULL",
      )
      .all()
      .map((row) => String(row.task_id));
    report.taskMessages = taskIds.length;
    // The app's boot step interrupts queued delivery to a missing task (`AgentTasks.endWorkOfGoneTasks`).
    const sessionIds = new Set(
      kept.flatMap((session) => [
        String(session.thread_id),
        ...(session.legacy_task_id ? [String(session.legacy_task_id)] : []),
      ]),
    );
    report.taskMessagesWithoutThread = taskIds.filter((id) => !sessionIds.has(id)).length;

    const integrity = connection.prepare("PRAGMA integrity_check").all();
    assert.deepEqual(
      integrity.map((row) => row.integrity_check),
      ["ok"],
    );
    assert.equal(connection.prepare("PRAGMA foreign_key_check").all().length, 0, "a foreign key has no row");
    const migratedData = tableHashes(connection);
    const migrationMarkers = connection.prepare("SELECT * FROM schema_migrations ORDER BY version").all();
    assert.deepEqual(
      migrationMarkers.map((marker) => marker.version),
      Array.from({ length: LATEST_VERSION - BASELINE_VERSION + 1 }, (_value, index) => index + BASELINE_VERSION),
      "the host did not retain the complete migration history",
    );
    connection.close();
    connection = undefined;
    await application.close();
    application = undefined;

    application = await startHost(profile);
    connection = new DatabaseSync(copy, { readOnly: true });
    report.hostRestarted = true;
    report.restartedHostPid = application.process().pid;
    assert.notEqual(report.restartedHostPid, report.hostPid, "restart must launch a new host process");
    await checkHostTasks();
    assert.equal(version(connection), latestVersion, "the restarted host changed migration state");
    assert.equal(
      connection.prepare("SELECT COUNT(*) AS count FROM projection_agent_threads").get()?.count,
      kept.length,
      "the restarted host did not retain upstream sessions",
    );
    assert.deepEqual(connection.prepare("PRAGMA foreign_key_check").all(), []);
    assert.deepEqual(
      connection
        .prepare("PRAGMA integrity_check")
        .all()
        .map((row) => row.integrity_check),
      ["ok"],
    );
    assert.deepEqual(
      connection.prepare("SELECT * FROM schema_migrations ORDER BY version").all(),
      migrationMarkers,
      "the restarted host changed migration markers",
    );
    for (const name of ["projection_threads", "projection_agent_threads"]) {
      assert.deepEqual(tableHashes(connection).get(name), migratedData.get(name), `restart changed ${name}`);
    }
    report.passed = true;
  } catch (error) {
    report.error = error instanceof Error ? error.message : String(error);
  } finally {
    connection?.close();
    await application?.close();
    try {
      db.close();
    } catch {}
  }
  return report;
}

async function createUpstreamFixture(path: string): Promise<void> {
  const db = new DatabaseSync(path);
  try {
    db.exec("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)");
    migrateUpTo(db, 44);
    db.prepare(`INSERT INTO projection_threads
      (thread_id, agent_id, title, active_turn_id, created_at, updated_at, last_event_sequence)
      VALUES ('upstream-session-1', 'upstream-agent', 'Retained upstream task', NULL, ?, ?, 12)`).run(
      "2026-10-01T08:00:00.000Z",
      "2026-10-02T08:00:00.000Z",
    );
    db.prepare(`INSERT INTO projection_agent_tasks
      (task_id, agent_id, thread_id, origin, title, outcome, error, created_at, updated_at, closed_at, last_event_sequence)
      VALUES ('upstream-task-1', 'upstream-agent', 'upstream-session-1', 'user', 'Retained upstream task',
        'completed', NULL, '2026-10-01T08:00:00.000Z', '2026-10-02T08:00:00.000Z', NULL, 12)`).run();
    db.exec(BROWSER_PROFILE_SCHEMA_SQL + WORKFLOW_SCHEMA_SQL + AGENT_THREAD_MIGRATION_SQL);
    db.exec("INSERT INTO schema_migrations VALUES (45, 'upstream'), (46, 'upstream'), (47, 'upstream')");
  } finally {
    db.close();
  }
}

test("the upstream 47 fixture matches the released schema", async () => {
  await mkdir(OUT, { recursive: true });
  const directory = await mkdtemp(join(OUT, "fixture-"));
  const path = join(directory, "upstream-47.db");
  try {
    await createUpstreamFixture(path);
    const db = new DatabaseSync(path, { readOnly: true });
    try {
      const tables = tableNames(db);
      assert.equal(version(db), 47);
      assert.ok(tables.includes("projection_agent_threads"), "released upstream 47 has agent threads");
      assert.ok(!tables.includes("projection_agent_tasks"), "released upstream 47 dropped the task table");
      assert.ok(!tables.includes("routine_backoff"), "released upstream 47 has no fork backoff table");
      assert.ok(Number(db.prepare("SELECT COUNT(*) AS count FROM projection_agent_threads").get()?.count ?? 0) > 0);
    } finally {
      db.close();
    }
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

async function runE2E(): Promise<void> {
  await mkdir(OUT, { recursive: true });
  const workDirectory = await mkdtemp(join(OUT, "work-"));
  let source = process.argv.find((arg) => arg.startsWith("--source="))?.slice("--source=".length);
  try {
    if (!source) {
      source = join(workDirectory, "upstream-47.db");
      await createUpstreamFixture(source);
    }
    const reports = [await upgrade(resolve(source), workDirectory, 0)];
    const passed = reports.every((report) => report.passed);
    await writeFile(join(OUT, "report.json"), `${JSON.stringify({ passed, reports }, null, 2)}\n`);
    process.stdout.write(`${JSON.stringify({ passed, reports }, null, 2)}\n`);
    assert.ok(
      passed,
      reports
        .map((report) => report.error)
        .filter(Boolean)
        .join("\n"),
    );
  } finally {
    await rm(workDirectory, { recursive: true, force: true });
  }
}

if (process.argv.includes("--run-e2e")) await runE2E();
else test("real host upgrades and restarts with upstream agent task data", runE2E, E2E_TIMEOUT_MS);
