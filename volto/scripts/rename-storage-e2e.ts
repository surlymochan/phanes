// Run: bun scripts/rename-storage-e2e.ts
// Opens a released-profile database through the real database boundary, with no network or Electron.
// Writes .volto-build/rename-storage-e2e/report.json. TMPDIR must be inside this worktree.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { Effect } from "effect";
import { DatabaseCore } from "../src/backend/database/database-core";
import { developmentUserDataName } from "../src/main/development-profile";
import { HOST_MANAGER_DIRECTORY } from "../src/main/host-update-files";

const output = resolve(import.meta.dirname, "../.volto-build/rename-storage-e2e");
const worktree = resolve(import.meta.dirname, "../..");
assert.ok(resolve(tmpdir()).startsWith(`${worktree}/.tmp/test-tmp`), "use the worktree TMPDIR");
const root = await mkdtemp(join(tmpdir(), "rename-storage-"));
const checks: string[] = [];
let core: DatabaseCore | undefined;
try {
  const legacy = new DatabaseSync(join(root, "openbot.db"));
  legacy.exec("CREATE TABLE rename_fixture (value TEXT NOT NULL); INSERT INTO rename_fixture VALUES ('saved history')");
  legacy.close();
  core = new DatabaseCore({ userDataPath: root });
  await Effect.runPromise(core.initialize());
  assert.equal(core.path, join(root, "openbot.db"));
  assert.equal(core.connection.prepare("SELECT value FROM rename_fixture").get()?.value, "saved history");
  checks.push("startup loads the existing openbot.db data");
  core.connection.prepare("INSERT INTO rename_fixture VALUES (?)").run("new history");
  core.close();
  await Effect.runPromise(core.initialize());
  assert.equal(core.connection.prepare("SELECT COUNT(*) AS total FROM rename_fixture").get()?.total, 2);
  assert.equal(core.connection.prepare("PRAGMA integrity_check").get()?.integrity_check, "ok");
  assert.equal(existsSync(join(root, "volto.db")), false);
  checks.push("restart keeps old and new writes and does not create volto.db");
  assert.equal(developmentUserDataName("app"), "OpenBot Dev");
  assert.equal(HOST_MANAGER_DIRECTORY, "/Library/Application Support/OpenBot/HostManager");
  checks.push("released development profile and host manager paths stay stable");
  // Read and update the released shared database through the actual isolated host process.
  const sharedPath = join(root, "agent-data.db");
  const shared = new DatabaseSync(sharedPath);
  shared.exec(`
    CREATE TABLE openbot_metadata (table_name TEXT PRIMARY KEY, owner_agent_id TEXT NOT NULL, created_at TEXT NOT NULL);
    INSERT INTO openbot_metadata VALUES ('contacts', 'saved-owner', '2026-01-01');
    CREATE TABLE contacts (name TEXT);
    INSERT INTO contacts VALUES ('saved contact');
  `);
  shared.close();
  const statements = [
    { sql: "SELECT owner_agent_id FROM openbot_metadata WHERE table_name = 'contacts'", mode: "read" },
    { sql: "INSERT INTO contacts VALUES ('new contact')", mode: "write" },
    { sql: "SELECT name FROM contacts ORDER BY rowid", mode: "read" },
    { sql: "UPDATE openbot_metadata SET owner_agent_id = 'other'", mode: "write" },
  ];
  const messages = statements.map((statement, id) => ({
    kind: "statement",
    id,
    databasePath: sharedPath,
    ...statement,
    provision: false,
    protectedTables: [],
    parameters: [],
    limits: { maxRows: 10, maxResultBytes: 10000, maxCellChars: 1000, maxPageCount: 65536, busyTimeoutMs: 2000 },
  }));
  const child = spawnSync("node", [resolve(import.meta.dirname, "../src/backend/agent-data/agent-database-host.ts")], {
    input: `${messages.map((message) => JSON.stringify(message)).join("\n")}\n`,
    encoding: "utf8",
    timeout: 30000,
  });
  assert.equal(child.status, 0, child.stderr);
  const responses = child.stdout
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  assert.equal(responses.length, 4);
  assert.deepEqual(responses[0]?.result?.rows, [["saved-owner"]]);
  assert.equal(responses[1]?.ok, true);
  assert.deepEqual(responses[2]?.result?.rows, [["saved contact"], ["new contact"]]);
  assert.equal(responses[3]?.ok, false, "agents cannot overwrite released ownership metadata");
  const restarted = new DatabaseSync(sharedPath);
  assert.equal(restarted.prepare("SELECT COUNT(*) AS total FROM contacts").get()?.total, 2);
  assert.equal(restarted.prepare("SELECT owner_agent_id FROM openbot_metadata").get()?.owner_agent_id, "saved-owner");
  assert.equal(
    restarted.prepare("SELECT COUNT(*) AS total FROM sqlite_master WHERE name = 'volto_metadata'").get()?.total,
    0,
  );
  restarted.close();
  checks.push(
    "shared database host preserves existing ownership, reads and writes data, and protects ownership metadata",
  );
  await mkdir(output, { recursive: true });
  await writeFile(join(output, "report.json"), `${JSON.stringify({ passed: true, checks }, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify({ passed: true, checks })}\n`);
} finally {
  core?.close();
}
