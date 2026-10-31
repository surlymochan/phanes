// Drives the routine backoff with the real SQLite database and migrations, in a temporary directory.
// Three unchanged answers start a one-hour backoff. Each further unchanged run doubles it, up to four
// hours. A changed answer ends it. An occurrence inside the backoff is skipped, and so is a held streak
// with no new thread activity. The state survives a reopen of the database.
//
// Run: bun scripts/routine-backoff-e2e.ts
// It writes .openbot-build/routine-backoff-e2e/report.json and exits non-zero on a failure.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { OpenBotDatabase } from "../src/backend/openbot-database";
import {
  answerFingerprint,
  backoffDurationMs,
  RoutineBackoffStore,
} from "../src/backend/routine-backoff/routine-backoff-store";

const ROOT = resolve(import.meta.dirname, "..");
const OUT = join(ROOT, ".openbot-build", "routine-backoff-e2e");
const HOUR = 60 * 60_000;
const ROUTINE = "routine-backoff-fixture";

const cases: { name: string; passed: boolean; detail: string }[] = [];

function check(name: string, condition: () => void): void {
  try {
    condition();
    cases.push({ name, passed: true, detail: "ok" });
  } catch (error) {
    cases.push({ name, passed: false, detail: error instanceof Error ? error.message : String(error) });
  }
}

function insertRoutine(database: OpenBotDatabase): void {
  const stamp = "2026-10-01T07:00:00.000Z";
  database.connection
    .prepare(
      `INSERT INTO projection_agent_routines (routine_id, agent_id, name, instruction, active, timezone, created_at,
         updated_at, last_event_sequence) VALUES (?, 'agent-fixture', 'Status check', 'Check the issues.', 1,
         'UTC', ?, ?, 1)`,
    )
    .run(ROUTINE, stamp, stamp);
}

const root = await mkdtemp(join(tmpdir(), "openbot-routine-backoff-"));
let database = new OpenBotDatabase(join(root, "state"));
await runCauseEffect(database.initialize());
insertRoutine(database);
let store = new RoutineBackoffStore({ database });

const start = new Date("2026-10-21T00:00:00.000Z");
const same = answerFingerprint("open issues 141, closed 0, merged 0");
const hours = (value: number) => new Date(start.getTime() + value * HOUR);

check("a routine with no run has no backoff and runs", () => {
  assert.equal(store.get(ROUTINE), null);
  assert.equal(store.decide(ROUTINE, hours(0), false), "run");
});

check("timestamps do not change the fingerprint", () => {
  assert.equal(
    answerFingerprint("open issues 141, closed 0 (04:15–04:45 SGT)"),
    answerFingerprint("open issues 141, closed 0 (05:15–05:45 SGT)"),
  );
  assert.notEqual(answerFingerprint("open issues 141"), answerFingerprint("open issues 139"));
});

store.recordRun(ROUTINE, same, hours(0));
store.recordRun(ROUTINE, same, hours(1));

check("two unchanged runs do not yet start a backoff", () => {
  const record = store.get(ROUTINE);
  assert.equal(record?.streak, 2);
  assert.equal(record?.backoffUntil, null);
});

store.recordRun(ROUTINE, same, hours(2));

check("the third unchanged run starts a one-hour backoff", () => {
  const record = store.get(ROUTINE);
  assert.equal(record?.streak, 3);
  assert.equal(record?.backoffSteps, 1);
  assert.equal(record?.backoffUntil, new Date(hours(3).getTime()).toISOString());
});

check("an occurrence inside the backoff is skipped with no model turn", () => {
  assert.equal(store.decide(ROUTINE, hours(2.5), true), "skip-backoff");
});

check("an occurrence after the backoff runs again when there is activity", () => {
  assert.equal(store.decide(ROUTINE, hours(3.5), true), "run");
});

check("a held streak with no new activity skips the occurrence", () => {
  assert.equal(store.decide(ROUTINE, hours(3.5), false), "skip-quiet");
});

store.recordRun(ROUTINE, same, hours(4));

check("each further unchanged run doubles the backoff", () => {
  const record = store.get(ROUTINE);
  assert.equal(record?.backoffSteps, 2);
  assert.equal(record?.backoffUntil, new Date(hours(6).getTime()).toISOString());
});

for (let index = 0; index < 4; index += 1) store.recordRun(ROUTINE, same, hours(10 + index * 5));

check("the backoff never exceeds four hours", () => {
  assert.equal(backoffDurationMs(10), 4 * HOUR);
  const record = store.get(ROUTINE);
  assert.ok(record !== null);
  assert.ok(Date.parse(record.backoffUntil ?? "") - Date.parse(record.updatedAt) <= 4 * HOUR);
});

store.recordRun(ROUTINE, answerFingerprint("open issues 140, closed 1, merged 0"), hours(40));

check("a changed answer ends the streak and the backoff", () => {
  const record = store.get(ROUTINE);
  assert.equal(record?.streak, 1);
  assert.equal(record?.backoffSteps, 0);
  assert.equal(record?.backoffUntil, null);
  assert.equal(store.decide(ROUTINE, hours(40.5), false), "run");
});

store.recordRun(ROUTINE, same, hours(41));
store.recordRun(ROUTINE, same, hours(42));
store.recordRun(ROUTINE, same, hours(43));
database.close();

database = new OpenBotDatabase(join(root, "state"));
await runCauseEffect(database.initialize());
store = new RoutineBackoffStore({ database });

check("the backoff state survives a database reopen", () => {
  const record = store.get(ROUTINE);
  assert.equal(record?.streak, 3);
  assert.equal(record?.backoffSteps, 1);
  assert.equal(store.decide(ROUTINE, hours(43.5), true), "skip-backoff");
});

database.connection.prepare("DELETE FROM projection_agent_routines WHERE routine_id = ?").run(ROUTINE);

check("deleting the routine removes its backoff row", () => {
  assert.equal(store.get(ROUTINE), null);
});

database.close();
await rm(root, { recursive: true, force: true });

const passed = cases.every((item) => item.passed);
await mkdir(OUT, { recursive: true });
await writeFile(join(OUT, "report.json"), `${JSON.stringify({ passed, cases }, null, 2)}\n`);
process.stdout.write(`${passed ? "PASS" : "FAIL"}: report in ${join(OUT, "report.json")}\n`);
if (!passed) process.exitCode = 1;
