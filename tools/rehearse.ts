// Hub upgrade rehearsal: migrate a copy of the fork-45 hub database with the code of this checkout.
// Usage: bun .openbot-build/hub-upgrade-rehearsal/rehearse.ts <backup.db> <out-dir>
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { migrateOpenBotDatabase } from "../../src/backend/openbot-database-schema";

const [source, outDir] = process.argv.slice(2);
if (!source || !outDir) throw new Error("usage: rehearse.ts <backup.db> <out-dir>");
mkdirSync(outDir, { recursive: true });
const copy = join(outDir, "hub-copy.db");
copyFileSync(source, copy);

type Snapshot = Map<string, { rows: number; hash: string }>;

function tables(db: DatabaseSync): string[] {
  return (
    db
      .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
      .all() as { name: string }[]
  ).map((row) => row.name);
}

function snapshot(db: DatabaseSync, skip: Set<string>): Snapshot {
  const result: Snapshot = new Map();
  for (const name of tables(db)) {
    if (skip.has(name)) continue;
    const rows = db.prepare(`SELECT * FROM "${name}"`).all();
    const lines = rows.map((row) => JSON.stringify(row)).sort();
    const hash = createHash("sha256").update(lines.join("\n")).digest("hex");
    result.set(name, { rows: rows.length, hash });
  }
  return result;
}

function columns(db: DatabaseSync): Record<string, string[]> {
  const result: Record<string, string[]> = {};
  for (const name of tables(db)) {
    if (name === "schema_migrations") continue;
    result[name] = (db.prepare(`PRAGMA table_info("${name}")`).all() as { name: string; type: string; notnull: number; pk: number }[])
      .map((c) => `${c.name}:${c.type}:${c.notnull}:${c.pk}`)
      .sort();
  }
  return result;
}

function versions(db: DatabaseSync): { version: number; applied_at: string }[] {
  return db.prepare("SELECT version, applied_at FROM schema_migrations ORDER BY version").all() as {
    version: number;
    applied_at: string;
  }[];
}

const db = new DatabaseSync(copy);
db.exec("PRAGMA journal_mode = WAL");
db.exec("PRAGMA foreign_keys = ON");

// The hub has no routine yet, so seed three routines and their backoff rows into the copy only.
const agent =
  (db.prepare("SELECT agent_id FROM projection_agent_routines LIMIT 1").get() as { agent_id?: string } | undefined)
    ?.agent_id ?? "rehearsal-agent";
const now = "2026-10-28T10:00:00.000Z";
db.exec("BEGIN");
for (let i = 1; i <= 3; i += 1) {
  db.prepare(
    `INSERT INTO projection_agent_routines
       (routine_id, agent_id, name, instruction, active, timezone, created_at, updated_at, last_event_sequence)
     VALUES (?, ?, ?, ?, 1, 'Asia/Shanghai', ?, ?, 0)`,
  ).run(`rehearsal-routine-${i}`, agent, `Rehearsal ${i}`, "Rehearsal routine", now, now);
  db.prepare(
    `INSERT INTO routine_backoff (routine_id, fingerprint, streak, backoff_steps, backoff_until, updated_at)
     VALUES (?, ?, ?, ?, ?, ?)`,
  ).run(`rehearsal-routine-${i}`, `fp-${i}`, i, i - 1, i === 1 ? null : `2026-10-29T0${i}:00:00.000Z`, now);
}
// Two tasks on existing conversations, so that the task-to-thread copy of migration 48 has rows.
const threadIds = (
  db.prepare("SELECT thread_id FROM projection_threads ORDER BY thread_id LIMIT 2").all() as { thread_id: string }[]
).map((row) => row.thread_id);
threadIds.forEach((threadId, i) => {
  db.prepare(
    `INSERT INTO projection_agent_tasks
       (task_id, agent_id, thread_id, origin, title, outcome, error, created_at, updated_at, closed_at, last_event_sequence)
     VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0)`,
  ).run(`rehearsal-task-${i + 1}`, agent, threadId, i === 0 ? "user" : "agent", `Task ${i + 1}`, i === 0 ? null : "completed", now, now, i === 0 ? null : now);
});
db.exec("COMMIT");
const tasksBefore = db
  .prepare("SELECT task_id, thread_id, title, closed_at FROM projection_agent_tasks ORDER BY task_id")
  .all() as { task_id: string; thread_id: string; title: string; closed_at: string | null }[];

const beforeVersions = versions(db);
const beforeTables = tables(db);
const before = snapshot(db, new Set(["schema_migrations"]));
const backoffBefore = db.prepare("SELECT * FROM routine_backoff ORDER BY routine_id").all();

const started = Date.now();
migrateOpenBotDatabase(db, { closedCleanly: true });
const migrateMs = Date.now() - started;

const afterVersions = versions(db);
const afterTables = tables(db);
const after = snapshot(db, new Set(["schema_migrations"]));
const backoffAfter = db.prepare("SELECT * FROM routine_backoff ORDER BY routine_id").all();

const changed: string[] = [];
const removed: string[] = [];
for (const [name, value] of before) {
  const next = after.get(name);
  if (!next) removed.push(name);
  else if (next.hash !== value.hash) changed.push(`${name} (${value.rows} -> ${next.rows})`);
}
const added = afterTables.filter((name) => !beforeTables.includes(name));
const keptMarkers = beforeVersions.every((row) =>
  afterVersions.some((next) => next.version === row.version && next.applied_at === row.applied_at),
);

const tasksAsThreads = db
  .prepare(
    "SELECT legacy_task_id AS task_id, thread_id, title, archived_at AS closed_at FROM projection_agent_threads WHERE legacy_task_id IS NOT NULL ORDER BY legacy_task_id",
  )
  .all();
const tasksCopied = tasksBefore.length > 0 && JSON.stringify(tasksAsThreads) === JSON.stringify(tasksBefore);
const integrity = db.prepare("PRAGMA integrity_check").all();
const foreignKeys = db.prepare("PRAGMA foreign_key_check").all();

// A second start must change nothing.
const secondBefore = snapshot(db, new Set());
migrateOpenBotDatabase(db, { closedCleanly: true });
const secondAfter = snapshot(db, new Set());
const idempotent = [...secondBefore].every(([name, value]) => secondAfter.get(name)?.hash === value.hash);

// The upgraded schema must match the schema of a new database.
const fresh = new DatabaseSync(":memory:");
fresh.exec("PRAGMA foreign_keys = ON");
migrateOpenBotDatabase(fresh);
const upgradedColumns = columns(db);
const freshColumns = columns(fresh);
const schemaDiff = [...new Set([...Object.keys(upgradedColumns), ...Object.keys(freshColumns)])]
  .filter((name) => JSON.stringify(upgradedColumns[name]) !== JSON.stringify(freshColumns[name]))
  .map((name) => ({ table: name, upgraded: upgradedColumns[name] ?? null, fresh: freshColumns[name] ?? null }));
const freshVersion = (fresh.prepare("SELECT MAX(version) AS v FROM schema_migrations").get() as { v: number }).v;
db.close();
fresh.close();

const report = {
  source,
  migrateMs,
  versionBefore: Math.max(...beforeVersions.map((row) => row.version)),
  versionAfter: Math.max(...afterVersions.map((row) => row.version)),
  freshVersion,
  markersBefore: beforeVersions.length,
  markersAfter: afterVersions.length,
  keptEarlierMarkers: keptMarkers,
  tablesBefore: beforeTables.length,
  tablesAfter: afterTables.length,
  addedTables: added,
  removedTables: removed,
  changedTables: changed,
  agentTasks: { before: tasksBefore.length, copiedToThreads: tasksAsThreads.length, identical: tasksCopied },
  routineBackoffRows: { before: backoffBefore.length, after: backoffAfter.length },
  routineBackoffIdentical: JSON.stringify(backoffBefore) === JSON.stringify(backoffAfter),
  integrityCheck: integrity,
  foreignKeyViolations: foreignKeys.length,
  secondStartIdempotent: idempotent,
  schemaDiffAgainstFresh: schemaDiff,
};
const pass =
  report.versionAfter === freshVersion &&
  keptMarkers &&
  JSON.stringify(removed) === JSON.stringify(["projection_agent_tasks"]) &&
  tasksCopied &&
  changed.length === 0 &&
  report.routineBackoffIdentical &&
  backoffAfter.length === 3 &&
  JSON.stringify(integrity) === JSON.stringify([{ integrity_check: "ok" }]) &&
  foreignKeys.length === 0 &&
  idempotent &&
  schemaDiff.length === 0;
writeFileSync(join(outDir, "report.json"), `${JSON.stringify({ pass, ...report }, null, 2)}\n`);
console.log(JSON.stringify({ pass, ...report }, null, 2));
process.exit(pass ? 0 : 1);
