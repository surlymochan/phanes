// @vitest-environment node
import { DatabaseSync, type SQLOutputValue } from "node:sqlite";
import { afterEach, describe, expect, it } from "vitest";
import { migrateOpenBotDatabase, tokenizeSql } from "./openbot-database-schema";
import recordedSchemaHistory from "./openbot-database-schema-history.json";

type TableRow = Record<string, SQLOutputValue>;

// The versions a version 14 database holds: the baseline and every version after it, up to 14.
const VERSION_14_VERSIONS = ["8", "9", "10", "11", "12", "13", "14"];

// The history file records what each shipped version added, changed or dropped, so the entries of
// these versions are the whole object set of a version 14 database.
const recordedHistory: Record<string, Record<string, string | null>> = recordedSchemaHistory;

// The columns of a message that every version holds. Later versions add columns, so only these are
// compared before and after the upgrade.
const MESSAGE_COLUMNS = [
  "thread_id",
  "message_id",
  "turn_id",
  "author",
  "status",
  "item_type",
  "created_at",
  "ordinal",
  "message_json",
  "last_event_sequence",
];

const databases: DatabaseSync[] = [];
afterEach(() => {
  for (const db of databases.splice(0)) db.close();
});

describe("agent usage migration", () => {
  it("upgrades a version 14 database with rows, and rolls back and retries a failed upgrade", () => {
    const db = databaseAt(14);

    // The fixture has to hold the object set of the release it claims to be. A database built from
    // the latest schema and rewound to version 14 keeps the tables and the columns that the later
    // versions added, so the upgrade would run against a shape no release ever wrote.
    expect(declarations(db)).toEqual(recordedVersion14Declarations());
    expect(columnNames(db, "projection_thread_messages")).not.toContain("group_start");

    seedConversation(db);
    const messages = pick(rows(db, "projection_thread_messages"), MESSAGE_COLUMNS);
    const agents = rows(db, "projection_agents");
    expect(messages).toHaveLength(2);
    expect(agents).toHaveLength(1);

    // The squatted name is the index's, not a table's: the migration creates its tables with
    // IF NOT EXISTS, and SQLite refuses an index whose name a table already holds however the
    // statement is spelled. What is under test is the rollback, not which object collides.
    db.exec("CREATE TABLE agent_usage_date (conflict TEXT)");
    expect(() => migrateOpenBotDatabase(db)).toThrow("migration to version 15 failed");
    // The failed migration leaves nothing behind: no marker, no table, and the rows of the version
    // 14 database are the rows it had before the upgrade started.
    expect(db.prepare("SELECT version FROM schema_migrations WHERE version = 15").get()).toBeUndefined();
    expect(db.prepare("SELECT name FROM sqlite_master WHERE name = 'agent_usage_records'").get()).toBeUndefined();
    expect(db.prepare("SELECT MAX(version) AS latest FROM schema_migrations").get()).toEqual({ latest: 14 });
    expect(pick(rows(db, "projection_thread_messages"), MESSAGE_COLUMNS)).toEqual(messages);
    expect(rows(db, "projection_agents")).toEqual(agents);

    db.exec("DROP TABLE agent_usage_date");
    migrateOpenBotDatabase(db);

    expect(db.prepare("SELECT MAX(version) AS latest FROM schema_migrations").get()).toEqual({ latest: 49 });
    expect(db.prepare("SELECT name FROM sqlite_master WHERE name = 'agent_usage_records'").get()).toBeDefined();
    expect(db.prepare("SELECT name FROM sqlite_master WHERE name = 'agent_usage_checkpoints'").get()).toBeDefined();
    expect(db.prepare("SELECT name FROM sqlite_master WHERE name = 'agent_usage_activity'").get()).toBeDefined();
    // The rows of the version 14 database are the rows the upgrade leaves behind, and the columns
    // of version 40 arrive with it: the fixture held none of them.
    expect(pick(rows(db, "projection_thread_messages"), MESSAGE_COLUMNS)).toEqual(messages);
    expect(rows(db, "projection_agents")).toEqual(agents);
    expect(columnNames(db, "projection_thread_messages")).toContain("group_start");
    // A table of the user's own, which no migration knows about.
    expect(rows(db, "preservation")).toEqual([{ value: "keep" }]);
    // A host-wide report constrains the date alone, so it can only seek on an index that
    // leads with `occurred_at`. Without these a report scans all retained history, and
    // the parity check cannot notice: it compares the two build paths to each other.
    expect(
      db.prepare("SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE '%_occurred' ORDER BY name").all(),
    ).toEqual([{ name: "agent_usage_activity_occurred" }, { name: "agent_usage_occurred" }]);
    expect(db.prepare("PRAGMA integrity_check").get()).toEqual({ integrity_check: "ok" });
    expect(db.prepare("PRAGMA foreign_key_check").all()).toEqual([]);
  });
});

/** The schema as of `version`, with the migration to the next version refused by a trigger. */
function databaseAt(version: number): DatabaseSync {
  const db = new DatabaseSync(":memory:");
  databases.push(db);
  db.exec(`CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
    CREATE TEMP TRIGGER stop_at_version BEFORE INSERT ON schema_migrations WHEN new.version > ${version}
    BEGIN SELECT RAISE(ABORT, 'fixture stop'); END;`);
  expect(() => migrateOpenBotDatabase(db)).toThrow(`migration to version ${version + 1} failed`);
  db.exec("DROP TRIGGER stop_at_version");
  return db;
}

/** One conversation of a version 14 database: an agent, its thread, a turn, two messages and a read. */
function seedConversation(db: DatabaseSync): void {
  db.exec(`
    INSERT INTO projection_agents (agent_id, thread_id, model, updated_at, sort_order, agent_json, last_event_sequence)
      VALUES ('agent-1', 'thread-1', 'gpt-5', '2026-10-01T10:00:00.000Z', 0, '{"id":"agent-1","name":"Tester"}', 3);
    INSERT INTO projection_threads (thread_id, agent_id, title, active_turn_id, created_at, updated_at, last_event_sequence)
      VALUES ('thread-1', 'agent-1', 'Keep this chat', 'turn-1', '2026-10-01T10:00:00.000Z', '2026-10-01T10:05:00.000Z', 3);
    INSERT INTO projection_turns (turn_id, thread_id, provider_session_id, status, started_at, completed_at, last_event_sequence)
      VALUES ('turn-1', 'thread-1', NULL, 'completed', '2026-10-01T10:00:00.000Z', '2026-10-01T10:05:00.000Z', 3);
    INSERT INTO projection_thread_messages (thread_id, message_id, turn_id, author, status, item_type, created_at, ordinal, message_json, last_event_sequence)
      VALUES ('thread-1', 'message-1', 'turn-1', 'user', 'completed', NULL, '2026-10-01T10:01:00.000Z', 1, '{"id":"message-1","text":"Hello"}', 1);
    INSERT INTO projection_thread_messages (thread_id, message_id, turn_id, author, status, item_type, created_at, ordinal, message_json, last_event_sequence)
      VALUES ('thread-1', 'message-2', 'turn-1', 'assistant', 'completed', NULL, '2026-10-01T10:02:00.000Z', 2, '{"id":"message-2","text":"Hi"}', 2);
    INSERT INTO projection_thread_reads (thread_id, member_id, through_message_id, updated_at)
      VALUES ('thread-1', 'user-1', 'message-1', '2026-10-01T10:03:00.000Z');
    INSERT INTO projection_thread_read_baselines (thread_id, through_message_id, initialized_at)
      VALUES ('thread-1', 'message-1', '2026-10-01T10:00:00.000Z');
    CREATE TABLE preservation(value TEXT);
    INSERT INTO preservation VALUES ('keep');
  `);
}

/** The declarations of the database, normalized the way the migration runner compares them. */
function declarations(db: DatabaseSync): Record<string, string> {
  const result: Record<string, string> = {};
  for (const row of db
    .prepare(
      `SELECT type, name, sql FROM sqlite_master
       WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'`,
    )
    .all()) {
    result[`${String(row.type)} ${String(row.name)}`] = tokenizeSql(String(row.sql)).join(" ");
  }
  return result;
}

/** The declarations of a version 14 database, as the shipped history records them. */
function recordedVersion14Declarations(): Record<string, string> {
  const result: Record<string, string> = {};
  for (const version of VERSION_14_VERSIONS) {
    for (const [name, sql] of Object.entries(recordedHistory[version] ?? {})) {
      if (sql === null) delete result[name];
      else result[name] = sql;
    }
  }
  return result;
}

function columnNames(db: DatabaseSync, table: string): string[] {
  return db
    .prepare(`PRAGMA table_info(${table})`)
    .all()
    .map((row) => String(row.name));
}

function rows(db: DatabaseSync, table: string): TableRow[] {
  return db.prepare(`SELECT * FROM ${table}`).all();
}

function pick(list: TableRow[], columns: string[]): TableRow[] {
  return list.map((row) => Object.fromEntries(columns.map((column) => [column, row[column] ?? null])));
}
