// @vitest-environment node
import { DatabaseSync, type SQLOutputValue } from "node:sqlite";
import { afterEach, describe, expect, it } from "vitest";
import { AGENT_THREAD_MIGRATION_SQL, AGENT_THREAD_SCHEMA_SQL } from "./agent-threads/agent-thread-schema";
import { BROWSER_PROFILE_SCHEMA_SQL } from "./browser-profiles/browser-profile-schema";
import { migrateOpenBotDatabase } from "./openbot-database-schema";
import upstreamSchemaHistory from "./openbot-database-upstream-schema-history.json";
import { WORKFLOW_SCHEMA_SQL } from "./workflows/workflow-schema";

// Failure modes: an upgrade loses or changes an agent routine or its trigger; the new table is missing
// on an upgraded database; a backoff row survives its deleted routine; a row with a zero streak or a
// negative step is accepted; a retry of the migration fails because the table already exists.
const databases: DatabaseSync[] = [];
afterEach(() => {
  for (const db of databases.splice(0)) db.close();
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

/** One agent routine with a daily trigger, in the columns every released version has. */
function seedRoutine(db: DatabaseSync): void {
  const stamp = "2026-10-01T07:00:00.000Z";
  db.prepare(
    `INSERT INTO projection_agent_routines (routine_id, agent_id, name, instruction, active, timezone, created_at,
       updated_at, last_event_sequence) VALUES ('routine-1', 'agent-1', 'Morning brief', 'Collect the news.', 1,
       'Europe/Warsaw', ?, ?, 1)`,
  ).run(stamp, stamp);
  db.prepare(
    `INSERT INTO projection_routine_triggers (trigger_id, routine_id, schedule_json, next_run_at, created_at,
       updated_at, last_event_sequence) VALUES ('trigger-1', 'routine-1', '{"kind":"daily","time":"09:00"}',
       '2026-10-02T07:00:00.000Z', ?, ?, 2)`,
  ).run(stamp, stamp);
}

type TableRow = Record<string, SQLOutputValue>;

function rows(db: DatabaseSync, table: string): TableRow[] {
  return db.prepare(`SELECT * FROM ${table}`).all();
}

function pick(list: TableRow[], columns: string[]): TableRow[] {
  return list.map((row) => Object.fromEntries(columns.map((column) => [column, row[column] ?? null])));
}

/** The released versions whose routines the upgrade reads: the baseline, and each later table change. */
const RELEASED_VERSIONS = Array.from({ length: 41 }, (_, index) => index + 8);

describe("routine backoff migration", () => {
  for (const version of RELEASED_VERSIONS) {
    it(`keeps the routines of a version ${version} database and adds an empty backoff table`, () => {
      const db = databaseAt(version);
      seedRoutine(db);
      // Upgrades add columns to old rows, so only the columns every version has are compared.
      const before = {
        routines: pick(rows(db, "projection_agent_routines"), [
          "routine_id",
          "agent_id",
          "name",
          "instruction",
          "active",
          "timezone",
        ]),
        triggers: pick(rows(db, "projection_routine_triggers"), [
          "trigger_id",
          "routine_id",
          "schedule_json",
          "next_run_at",
        ]),
      };

      migrateOpenBotDatabase(db);

      expect(pick(rows(db, "projection_agent_routines"), Object.keys(before.routines[0] ?? {}))).toEqual(
        before.routines,
      );
      expect(pick(rows(db, "projection_routine_triggers"), Object.keys(before.triggers[0] ?? {}))).toEqual(
        before.triggers,
      );
      expect(rows(db, "routine_backoff")).toEqual([]);
      expect(db.prepare("SELECT MAX(version) AS latest FROM schema_migrations").get()).toEqual({ latest: 49 });
    });
  }

  it("cascades a backoff row with its routine and rejects impossible values", () => {
    const db = databaseAt(44);
    seedRoutine(db);
    migrateOpenBotDatabase(db);
    db.exec("PRAGMA foreign_keys = ON");
    const stamp = "2026-10-21T00:00:00.000Z";
    db.prepare(
      "INSERT INTO routine_backoff (routine_id, fingerprint, streak, backoff_steps, backoff_until, updated_at) VALUES ('routine-1', 'a', 3, 1, NULL, ?)",
    ).run(stamp);

    expect(() =>
      db
        .prepare(
          "INSERT INTO routine_backoff (routine_id, fingerprint, streak, backoff_steps, backoff_until, updated_at) VALUES ('routine-2', 'a', 0, 0, NULL, ?)",
        )
        .run(stamp),
    ).toThrow();
    expect(() =>
      db.prepare("UPDATE routine_backoff SET backoff_steps = -1 WHERE routine_id = 'routine-1'").run(),
    ).toThrow();

    db.prepare("DELETE FROM projection_agent_routines WHERE routine_id = 'routine-1'").run();
    expect(rows(db, "routine_backoff")).toEqual([]);
  });

  it("is not applied twice when the migration runs again on an upgraded database", () => {
    const db = databaseAt(44);
    seedRoutine(db);
    migrateOpenBotDatabase(db);

    expect(() => migrateOpenBotDatabase(db)).not.toThrow();
    expect(rows(db, "projection_agent_routines")).toHaveLength(1);
  });
});

/** Upstream markers retain their original timestamps and meaning. */
function importedAt(version: number): DatabaseSync {
  const db = databaseAt(44);
  seedRoutine(db);
  db.exec(BROWSER_PROFILE_SCHEMA_SQL);
  db.exec("INSERT INTO browser_profiles VALUES ('0123456789abcdef0123456789abcdef', 'Imported', 30, 'old', 'old')");
  if (version >= 46) {
    db.exec(WORKFLOW_SCHEMA_SQL);
    db.exec("INSERT INTO workflows (workflow_id,title,created_at,updated_at) VALUES ('kept','Imported','old','old')");
  }
  if (version >= 47) db.exec(AGENT_THREAD_MIGRATION_SQL);
  for (let marker = 45; marker <= version; marker += 1) {
    db.prepare("INSERT INTO schema_migrations VALUES (?, 'upstream')").run(marker);
  }
  return db;
}

interface MigrationSnapshot {
  schema: TableRow[];
  data: (string | TableRow[])[][];
}

function snapshot(db: DatabaseSync): MigrationSnapshot {
  return {
    schema: db.prepare("SELECT * FROM sqlite_master ORDER BY name").all(),
    data: db
      .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
      .all()
      .map((row) => [String(row.name), rows(db, String(row.name))]),
  };
}

describe("fork and imported migration registry", () => {
  it.each([46, 47, 48, 49])("keeps fork backoff rows and marker 45 after marker %i fails", (failure) => {
    const db = databaseAt(45);
    seedRoutine(db);
    db.exec("INSERT INTO routine_backoff VALUES ('routine-1', 'kept', 3, 2, 'later', 'old')");
    const backoff = rows(db, "routine_backoff");
    const marker = db.prepare("SELECT * FROM schema_migrations WHERE version=45").get();
    db.exec(`CREATE TEMP TRIGGER fail_marker BEFORE INSERT ON schema_migrations WHEN new.version=${failure}
      BEGIN SELECT RAISE(ABORT, 'late failure'); END;`);
    expect(() => migrateOpenBotDatabase(db)).toThrow(`migration to version ${failure} failed`);
    expect(rows(db, "routine_backoff")).toEqual(backoff);
    expect(db.prepare("SELECT * FROM schema_migrations WHERE version=45").get()).toEqual(marker);
    expect(db.prepare("SELECT * FROM schema_migrations WHERE version >= ?").all(failure)).toEqual([]);
    db.exec("DROP TRIGGER fail_marker");
    migrateOpenBotDatabase(db);
    expect(rows(db, "routine_backoff")).toEqual(backoff);
    expect(db.prepare("SELECT * FROM schema_migrations WHERE version=45").get()).toEqual(marker);
    expect(db.prepare("PRAGMA foreign_keys").get()).toEqual({ foreign_keys: 1 });
    expect(db.prepare("PRAGMA foreign_key_check").all()).toEqual([]);
    expect(db.prepare("PRAGMA integrity_check").get()).toEqual({ integrity_check: "ok" });
  });

  for (const source of [45, 46, 47]) {
    for (const failure of [46, 47, 48, 49].filter((version) => version > source)) {
      it(`retries imported ${source} after marker ${failure} fails without changing source data`, () => {
        const db = importedAt(source);
        const markers = rows(db, "schema_migrations");
        const routine = rows(db, "projection_agent_routines");
        const profiles = rows(db, "browser_profiles");
        const workflows = source >= 46 ? rows(db, "workflows") : [];
        db.exec(`CREATE TEMP TRIGGER fail_marker BEFORE INSERT ON schema_migrations WHEN new.version=${failure}
          BEGIN SELECT RAISE(ABORT, 'late failure'); END;`);
        expect(() => migrateOpenBotDatabase(db)).toThrow(`migration to version ${failure} failed`);
        expect(db.prepare("SELECT * FROM schema_migrations WHERE version <= ? ORDER BY version").all(source)).toEqual(
          markers,
        );
        expect(db.prepare("SELECT * FROM schema_migrations WHERE version >= ?").all(failure)).toEqual([]);
        if (failure === 49)
          expect(db.prepare("SELECT name FROM sqlite_master WHERE name='routine_backoff'").all()).toEqual([]);
        expect(rows(db, "projection_agent_routines")).toEqual(routine);
        expect(rows(db, "browser_profiles")).toEqual(profiles);
        expect(db.prepare("PRAGMA foreign_keys").get()).toEqual({ foreign_keys: 1 });
        db.exec("DROP TRIGGER fail_marker");
        migrateOpenBotDatabase(db);
        expect(rows(db, "projection_agent_routines")).toEqual(routine);
        expect(rows(db, "browser_profiles")).toEqual(profiles);
        expect(rows(db, "workflows")).toEqual(workflows);
        expect(rows(db, "routine_backoff")).toEqual([]);
        expect(db.prepare("PRAGMA foreign_key_check").all()).toEqual([]);
        expect(db.prepare("PRAGMA integrity_check").get()).toEqual({ integrity_check: "ok" });
        expect(db.prepare("SELECT MAX(version) AS version FROM schema_migrations").get()).toEqual({ version: 49 });
        const beforeRetry = snapshot(db);
        migrateOpenBotDatabase(db);
        expect(snapshot(db)).toEqual(beforeRetry);
      });
    }
  }

  it.each([45, 46, 47])("rejects missing and newer imported %i histories without writes", (version) => {
    const db = importedAt(version);
    db.exec("INSERT INTO schema_migrations VALUES (50, 'future')");
    let before = snapshot(db);
    expect(() => migrateOpenBotDatabase(db)).toThrow("newer than this application supports");
    expect(snapshot(db)).toEqual(before);
    db.exec("DELETE FROM schema_migrations WHERE version=50; DELETE FROM schema_migrations WHERE version=20");
    before = snapshot(db);
    expect(() => migrateOpenBotDatabase(db)).toThrow("missing version 20");
    expect(snapshot(db)).toEqual(before);
  });

  it("rejects a backoff table without its released marker", () => {
    const db = databaseAt(45);
    db.exec("DELETE FROM schema_migrations WHERE version=45");
    const before = snapshot(db);
    expect(() => migrateOpenBotDatabase(db)).toThrow(/contradictory reserved schema/);
    expect(snapshot(db)).toEqual(before);
  });

  it.each([
    "CREATE TABLE browser_profiles (profile_id TEXT)",
    "CREATE TABLE workflow_unknown (payload TEXT)",
    WORKFLOW_SCHEMA_SQL,
    AGENT_THREAD_MIGRATION_SQL,
    "DROP INDEX agent_tasks_agent",
  ])("rejects an unknown or contradictory reserved structure before any writes: %s", (change) => {
    const db = databaseAt(45);
    seedRoutine(db);
    db.exec(change);
    const before = snapshot(db);
    expect(() => migrateOpenBotDatabase(db)).toThrow(/reserved schema/);
    expect(snapshot(db)).toEqual(before);
  });

  it("rejects a malformed imported workflow index before any writes", () => {
    const db = importedAt(46);
    db.exec("DROP INDEX workflow_nodes_agent; CREATE INDEX workflow_nodes_agent ON workflow_nodes(node_id)");
    const before = snapshot(db);
    expect(() => migrateOpenBotDatabase(db)).toThrow(/unknown reserved schema/);
    expect(snapshot(db)).toEqual(before);
  });
});

// The runner compares a reserved declaration with the one its own reference builds. SQLite keeps the
// text as it was written, so two databases can hold the same schema in different text. A database
// that comes from the upstream release holds text that this repository does not record, so a
// difference in the text alone must not stop the app, and a difference in the schema must.
describe("reserved schema guard", () => {
  const upstreamDeclaration = upstreamSchemaHistory["45"]["table browser_profiles"];

  // The same table as `BROWSER_PROFILE_SCHEMA_SQL`, written with other formatting: no
  // `IF NOT EXISTS`, no space around `=`, no space after a comma.
  const reformattedDeclaration = `
    CREATE TABLE browser_profiles (
      profile_id TEXT NOT NULL PRIMARY KEY CHECK(length(profile_id)=32),
      name TEXT NOT NULL,
      hue INTEGER NOT NULL CHECK(hue IN (0,30,55,100,150,185,215,245,280,320)),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
  `;

  it.each([
    ["the text the upstream release recorded", upstreamDeclaration],
    ["the same table written with other formatting", reformattedDeclaration],
  ])("accepts a reserved table declared with %s", (_label, declaration) => {
    const db = databaseAt(44);
    db.exec(declaration);
    db.prepare("INSERT INTO browser_profiles VALUES (?, ?, ?, ?, ?)").run(
      "0123456789abcdef0123456789abcdef",
      "Imported",
      30,
      "old",
      "old",
    );
    db.prepare("INSERT INTO schema_migrations VALUES (?, 'upstream')").run(45);

    migrateOpenBotDatabase(db);

    expect(rows(db, "browser_profiles")).toHaveLength(1);
    expect(rows(db, "routine_backoff")).toEqual([]);
    expect(db.prepare("SELECT MAX(version) AS latest FROM schema_migrations").get()).toEqual({ latest: 49 });
    expect(db.prepare("PRAGMA foreign_key_check").all()).toEqual([]);
    expect(db.prepare("PRAGMA integrity_check").get()).toEqual({ integrity_check: "ok" });
  });

  it("accepts an uppercase reserved table that declares the same schema and keeps its rows", () => {
    const db = databaseAt(44);
    db.exec(`
      CREATE TABLE ROUTINE_BACKOFF (
        routine_id TEXT PRIMARY KEY REFERENCES projection_agent_routines(routine_id) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL,
        streak INTEGER NOT NULL CHECK (streak >= 1),
        backoff_steps INTEGER NOT NULL CHECK (backoff_steps >= 0),
        backoff_until TEXT,
        updated_at TEXT NOT NULL
      );
    `);
    db.prepare("INSERT INTO schema_migrations VALUES (?, 'fork')").run(45);
    seedRoutine(db);
    db.prepare("INSERT INTO routine_backoff VALUES ('routine-1', 'kept', 3, 2, 'later', 'old')").run();

    migrateOpenBotDatabase(db);

    expect(rows(db, "routine_backoff")).toEqual([
      {
        routine_id: "routine-1",
        fingerprint: "kept",
        streak: 3,
        backoff_steps: 2,
        backoff_until: "later",
        updated_at: "old",
      },
    ]);
    expect(db.prepare("SELECT MAX(version) AS latest FROM schema_migrations").get()).toEqual({ latest: 49 });
    expect(db.prepare("PRAGMA foreign_key_check").all()).toEqual([]);
    expect(db.prepare("PRAGMA integrity_check").get()).toEqual({ integrity_check: "ok" });
    // The table that the runner accepted is the one it compared, so it keeps the checks of the
    // reference and refuses the values that the reference refuses.
    expect(() =>
      db.prepare("INSERT INTO routine_backoff VALUES ('routine-2', 'a', 0, -5, NULL, 'old')").run(),
    ).toThrow();
  });

  it("rejects an uppercase reserved table whose declaration differs before any writes", () => {
    const db = databaseAt(45);
    db.exec(
      "DROP TABLE routine_backoff; CREATE TABLE ROUTINE_BACKOFF (routine_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)",
    );
    const before = snapshot(db);

    expect(() => migrateOpenBotDatabase(db)).toThrow(
      "OpenBot database has an unknown reserved schema: routine_backoff.",
    );
    expect(snapshot(db)).toEqual(before);
  });

  it("rejects a reserved table whose string literal differs in case before any writes", () => {
    const db = databaseAt(48);
    db.exec("DROP TABLE projection_agent_threads");
    // The literal is a value, not syntax: a table that allows 'USER' does not allow 'user'.
    db.exec(AGENT_THREAD_SCHEMA_SQL.replace("'user'", "'USER'"));
    const before = snapshot(db);

    expect(() => migrateOpenBotDatabase(db)).toThrow(
      "OpenBot database has an unknown reserved schema: projection_agent_threads.",
    );
    expect(snapshot(db)).toEqual(before);
  });

  it("rejects a reserved table whose checks or foreign key are missing before any writes", () => {
    const db = databaseAt(45);
    db.exec(`
      DROP TABLE routine_backoff;
      CREATE TABLE routine_backoff (
        routine_id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        streak INTEGER NOT NULL,
        backoff_steps INTEGER NOT NULL,
        backoff_until TEXT,
        updated_at TEXT NOT NULL
      );
    `);
    const before = snapshot(db);

    expect(() => migrateOpenBotDatabase(db)).toThrow(
      "OpenBot database has an unknown reserved schema: routine_backoff.",
    );
    expect(snapshot(db)).toEqual(before);
  });
});
