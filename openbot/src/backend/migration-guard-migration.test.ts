// @vitest-environment node
import { DatabaseSync } from "node:sqlite";
import { afterEach, describe, expect, it } from "vitest";
import { migrateOpenBotDatabase } from "./openbot-database-schema";

// Failure modes: a migration commits a database with a broken foreign key; a failed migration leaves foreign-key
// enforcement off; a history with a gap at any version runs the migrations after the gap.
const databases: DatabaseSync[] = [];
afterEach(() => {
  for (const db of databases.splice(0)) db.close();
});

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

function latestDatabase(): DatabaseSync {
  const db = new DatabaseSync(":memory:");
  databases.push(db);
  migrateOpenBotDatabase(db);
  return db;
}

// A message and a turn whose thread and session do not exist. Only a database written with foreign keys off can
// hold them.
function seedOrphans(db: DatabaseSync): void {
  db.exec("PRAGMA foreign_keys = OFF");
  db.exec(`
    INSERT INTO projection_thread_messages (thread_id, message_id, turn_id, author, status, item_type, created_at,
      ordinal, message_json, last_event_sequence)
    VALUES ('openbot-thread-gone', 'message-1', NULL, 'user', 'completed', NULL, '2026-10-01T10:00:00.000Z', 0,
      '{"id":"message-1","text":"Still here."}', 1);
    INSERT INTO projection_threads
      (thread_id, agent_id, title, active_turn_id, created_at, updated_at, last_event_sequence)
    VALUES ('openbot-thread-chief', 'chief', 'Chief', NULL, '2026-10-01T09:00:00.000Z', '2026-10-01T09:00:00.000Z', 1);
    INSERT INTO projection_turns
      (turn_id, thread_id, provider_session_id, status, started_at, completed_at, last_event_sequence)
    VALUES ('turn-1', 'openbot-thread-chief', 'session-gone', 'completed', '2026-10-01T09:00:00.000Z', NULL, 1);`);
  db.exec("PRAGMA foreign_keys = ON");
}

function contents(db: DatabaseSync) {
  return {
    messages: db.prepare("SELECT * FROM projection_thread_messages ORDER BY message_id").all(),
    turns: db.prepare("SELECT * FROM projection_turns ORDER BY turn_id").all(),
  };
}

function versions(db: DatabaseSync): number[] {
  return db
    .prepare("SELECT version FROM schema_migrations ORDER BY version")
    .all()
    .map((row) => Number(row.version));
}

function range(from: number, to: number): number[] {
  return Array.from({ length: to - from + 1 }, (_, index) => from + index);
}

describe("migration guards", () => {
  // Migration 28 turns foreign keys off for its rebuild, and migrations 43 to 49 do not.
  it.each([28, 43, 44, 45, 46, 47, 48, 49])(
    "rolls back migration %i when the database has a broken foreign key",
    (version) => {
      const db = databaseAt(version - 1);
      seedOrphans(db);
      const before = contents(db);
      const schemaBefore = db.prepare("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").all();

      expect(() => migrateOpenBotDatabase(db)).toThrow(
        expect.objectContaining({
          message: `OpenBot database migration to version ${version} failed.`,
          cause: expect.objectContaining({
            message: "OpenBot database migration produced 2 foreign-key violation(s).",
          }),
        }),
      );
      expect(versions(db)).toEqual(range(8, version - 1));
      expect(db.prepare("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").all()).toEqual(schemaBefore);
      expect(contents(db)).toEqual(before);
      expect(db.prepare("PRAGMA foreign_keys").get()).toEqual({ foreign_keys: 1 });

      // The next start meets the same rows and stops again. It deletes nothing to get past them.
      expect(() => migrateOpenBotDatabase(db)).toThrow(`migration to version ${version} failed`);
      expect(contents(db)).toEqual(before);

      db.exec("DELETE FROM projection_thread_messages; DELETE FROM projection_turns");
      migrateOpenBotDatabase(db);
      expect(versions(db)).toEqual(range(8, 49));
      expect(db.prepare("PRAGMA foreign_key_check").all()).toEqual([]);
      expect(db.prepare("PRAGMA integrity_check").get()).toEqual({ integrity_check: "ok" });
    },
  );

  it.each(range(8, 48))("rejects a history that is missing version %i and changes nothing", (missing) => {
    const db = latestDatabase();
    db.prepare("DELETE FROM schema_migrations WHERE version = ?").run(missing);
    const schemaBefore = db.prepare("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").all();

    expect(() => migrateOpenBotDatabase(db)).toThrow(
      `OpenBot database migration history is missing version ${missing}.`,
    );
    expect(versions(db)).toEqual(range(8, 49).filter((version) => version !== missing));
    expect(db.prepare("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").all()).toEqual(schemaBefore);
  });
});
