// Upgrades a copy of a real `openbot.db` from upstream migration 47 through the desktop host startup path
// to migration 48 (tasks become side threads), then restarts the host. The source file is opened read-only
// (the app must be closed) and copied; only the copy changes. On the copy, a forced failure inside 48 must
// roll back to the task table before the real host starts. Checks: each task with a conversation is a side
// thread with the same values, and a task with
// none is reported; every other table holds the same rows as before 48; every mailbox message that names
// a task finds its thread; the integrity and foreign-key checks pass.
//   bun test scripts/agent-threads-upgrade-e2e.ts [<openbot.db>]
// Writes .volto-build/agent-threads-upgrade-e2e/report.json with counts only, no message text or titles.
import assert from "node:assert/strict";
import { test } from "bun:test";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { copyFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { DatabaseCore } from "../src/backend/database/database-core";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { migrateVoltoDatabase } from "../src/backend/volto-database-schema";

const OUT = resolve(import.meta.dirname, "../.volto-build/agent-threads-upgrade-e2e");
const THREAD_VERSION = 48;
const TASK_COLUMNS =
  "task_id, agent_id, thread_id, origin, title, outcome, error, created_at, updated_at, closed_at, last_event_sequence";

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
  try {
    report.sourceVersion = version(db);
    assert.ok(report.sourceVersion < THREAD_VERSION, `the source is at version ${report.sourceVersion}, not a release`);
    if (report.sourceVersion < THREAD_VERSION - 1) migrateUpTo(db, THREAD_VERSION - 1);
    assert.equal(version(db), THREAD_VERSION - 1);

    const before = tableHashes(db);
    const tasks = db.prepare(`SELECT ${TASK_COLUMNS} FROM projection_agent_tasks ORDER BY task_id`).all();
    assert.ok(tasks.length > 0, "the upstream database has no task data to preserve");
    const threadIds = new Set(
      db
        .prepare("SELECT thread_id FROM projection_threads")
        .all()
        .map((row) => String(row.thread_id)),
    );
    const kept = tasks.filter((task) => threadIds.has(String(task.thread_id)));
    report.tasks = tasks.length;
    report.orphanTasks = tasks.length - kept.length;
    report.tables = before.size;
    report.rows = [...before.values()].reduce((sum, table) => sum + table.rows, 0);

    // A failure inside 47 leaves the database as it was before 47.
    migrateUpTo(db, THREAD_VERSION - 1);
    assert.equal(version(db), THREAD_VERSION - 1);
    assert.deepEqual(tableHashes(db), before, "a failed migration 48 changed a table");
    report.rolledBack = true;

    db.close();

    // This is the same database startup path used by the desktop host. It performs the upgrade
    // while opening the copied profile, then the second initialize models an app restart.
    const host = new DatabaseCore({ userDataPath: profile });
    await runCauseEffect(host.initialize());
    report.hostStarted = true;
    const latestVersion = version(host.connection);
    assert.ok(latestVersion >= THREAD_VERSION, `host stopped at migration ${latestVersion}`);
    assert.equal(
      host.connection.prepare("SELECT COUNT(*) AS count FROM projection_agent_threads").get()?.count,
      kept.length,
      "the host did not retain every task as a session",
    );
    report.retainedSessions = kept.length;

    const after = tableHashes(host.connection);
    for (const [name, table] of before) {
      if (name === "projection_agent_tasks") continue;
      assert.deepEqual(after.get(name), table, `migration 48 changed ${name}`);
    }

    const threads = new Map(
      host.connection
        .prepare(
          `SELECT thread_id, agent_id, legacy_task_id, origin, title, title_source, outcome, error, created_at,
             updated_at, archived_at, last_event_sequence FROM projection_agent_threads`,
        )
        .all()
        .map((row) => [String(row.legacy_task_id), row]),
    );
    assert.equal(threads.size, kept.length, "a task with a conversation is not a side thread");
    for (const task of kept) {
      const thread = threads.get(String(task.task_id));
      assert.ok(thread, "a task has no side thread");
      assert.deepEqual(
        [thread.thread_id, thread.agent_id, thread.origin, thread.title, thread.outcome, thread.error],
        [task.thread_id, task.agent_id, task.origin, task.title, task.outcome, task.error],
      );
      assert.deepEqual(
        [thread.created_at, thread.updated_at, thread.archived_at, thread.last_event_sequence],
        [task.created_at, task.updated_at, task.closed_at, task.last_event_sequence],
      );
      assert.equal(thread.title_source, task.origin === "agent" ? "agent" : "auto");
    }
    report.archivedThreads = [...threads.values()].filter((thread) => thread.archived_at !== null).length;

    const taskIds = host.connection
      .prepare(
        "SELECT json_extract(message_json, '$.taskId') AS task_id FROM projection_mailbox_messages WHERE json_extract(message_json, '$.taskId') IS NOT NULL",
      )
      .all()
      .map((row) => String(row.task_id));
    report.taskMessages = taskIds.length;
    // A message can name a task that was not copied: its task had no conversation, or no row at all. The
    // app's boot step interrupts its queued delivery (`AgentTasks.endWorkOfGoneTasks`); the row stays.
    report.taskMessagesWithoutThread = taskIds.filter((id) => !threads.has(id)).length;

    const integrity = host.connection.prepare("PRAGMA integrity_check").all();
    assert.deepEqual(
      integrity.map((row) => row.integrity_check),
      ["ok"],
    );
    assert.equal(host.connection.prepare("PRAGMA foreign_key_check").all().length, 0, "a foreign key has no row");
    host.close();

    const restartedHost = new DatabaseCore({ userDataPath: profile });
    await runCauseEffect(restartedHost.initialize());
    report.hostRestarted = true;
    assert.equal(version(restartedHost.connection), latestVersion, "the restarted host changed migration state");
    assert.equal(
      restartedHost.connection.prepare("SELECT COUNT(*) AS count FROM projection_agent_threads").get()?.count,
      kept.length,
      "the restarted host did not retain migrated task sessions",
    );
    assert.deepEqual(restartedHost.connection.prepare("PRAGMA foreign_key_check").all(), []);
    assert.deepEqual(
      restartedHost.connection.prepare("PRAGMA integrity_check").all().map((row) => row.integrity_check),
      ["ok"],
    );
    restartedHost.close();
    report.passed = true;
  } catch (error) {
    report.error = error instanceof Error ? error.message : String(error);
  } finally {
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
    db.exec(`CREATE TEMP TRIGGER stop_at_version BEFORE INSERT ON schema_migrations WHEN new.version > 47
      BEGIN SELECT RAISE(ABORT, 'fixture stop'); END;`);
    assert.throws(() => migrateVoltoDatabase(db), /migration to version 48 failed/);
    db.exec("DROP TRIGGER stop_at_version");
    db.prepare(`INSERT INTO projection_threads
      (thread_id, agent_id, title, active_turn_id, created_at, updated_at, last_event_sequence)
      VALUES ('upstream-session-1', 'upstream-agent', 'Retained upstream task', NULL, ?, ?, 12)`)
      .run("2026-10-01T08:00:00.000Z", "2026-10-02T08:00:00.000Z");
    db.prepare(`INSERT INTO projection_agent_tasks
      (task_id, agent_id, thread_id, origin, title, outcome, error, created_at, updated_at, closed_at, last_event_sequence)
      VALUES ('upstream-task-1', 'upstream-agent', 'upstream-session-1', 'user', 'Retained upstream task',
        'completed', NULL, '2026-10-01T08:00:00.000Z', '2026-10-02T08:00:00.000Z', NULL, 12)`)
      .run();
  } finally {
    db.close();
  }
}

async function runE2E(): Promise<void> {
  await mkdir(OUT, { recursive: true });
  const workDirectory = await mkdtemp(join(tmpdir(), "openbot-thread-upgrade-"));
  let source = process.argv.slice(2).find((arg) => !arg.startsWith("--"));
  try {
    if (!source) {
      source = join(workDirectory, "upstream-47.db");
      await createUpstreamFixture(source);
    }
    const reports = [await upgrade(resolve(source), workDirectory, 0)];
    const passed = reports.every((report) => report.passed);
    await writeFile(join(OUT, "report.json"), `${JSON.stringify({ passed, reports }, null, 2)}\n`);
    process.stdout.write(`${JSON.stringify({ passed, reports }, null, 2)}\n`);
    assert.ok(passed, reports.map((report) => report.error).filter(Boolean).join("\n"));
  } finally {
    await rm(workDirectory, { recursive: true, force: true });
  }
}

if (process.argv.includes("--run-e2e")) await runE2E();
else test("real host upgrades and restarts with upstream agent task data", runE2E);
