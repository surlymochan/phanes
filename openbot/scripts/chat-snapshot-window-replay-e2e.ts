// Replays the stored conversation events of real threads through the desktop chat's snapshot merge,
// and checks that the chat keeps every message that arrives while it shows only the latest page.
// Read-only: it opens a copy of the database. Make the copy with
// `sqlite3 "file:<profile>/openbot.db?mode=ro" ".backup <copy>"` while the app runs, then run
// `bun scripts/chat-snapshot-window-replay-e2e.ts --db=<copy> [--threads=<id>,<id>] [--page=60]`.
// Without `--threads`, it replays the five threads with the most messages.
// A running turn sorts above a routine marker that follows it, so its later plan, answer and steer
// arrive above a loaded message. The merge must keep them.
// Writes .openbot-build/chat-snapshot-window-replay-e2e/report.json.

import { Database } from "bun:sqlite";
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { sortConversationMessages } from "../packages/contracts/src/conversation-order";
import type { ConversationMessage } from "../packages/contracts/src/ipc-conversation-messages";
import { windowedSnapshotMessages } from "../src/renderer/src/features/conversation/conversation-merge";

const OUT = resolve(import.meta.dirname, "../.openbot-build/chat-snapshot-window-replay-e2e");

function option(name: string): string | undefined {
  return process.argv.find((arg) => arg.startsWith(`--${name}=`))?.slice(name.length + 3);
}

const databasePath = option("db");
assert.ok(databasePath, "Pass --db=<path> with a copy of openbot.db.");
const pageSize = Number(option("page") ?? 60);
const database = new Database(databasePath, { readonly: true });

const threads =
  option("threads")?.split(",") ??
  database
    .query<{ thread_id: string }, []>(
      `SELECT thread_id FROM projection_thread_messages GROUP BY thread_id ORDER BY COUNT(*) DESC LIMIT 5`,
    )
    .all()
    .map((row) => row.thread_id);

interface ThreadResult {
  updates: number;
  /** Updates after which the chat did not show a message that the thread had in its window. */
  updatesWithLostMessages: number;
  firstLost?: { sequence: number; author: string; createdAt: string | undefined };
}

const report: { threads: Record<string, ThreadResult>; passed: boolean } = { threads: {}, passed: false };
/* The chat does not draw a queued or cancelled message, except a routine's. */
const drawn = (message: ConversationMessage) =>
  Boolean(message.routine) || (message.delivery?.status !== "queued" && message.delivery?.status !== "cancelled");

interface ConversationChange {
  changedMessages?: ConversationMessage[];
  removedMessageIds?: string[];
}

/* The events that change the messages of a thread carry one of the two lists. Others carry neither. */
function isConversationChange(value: unknown): value is ConversationChange {
  if (typeof value !== "object" || value === null) return false;
  return (
    ("changedMessages" in value && Array.isArray(value.changedMessages)) ||
    ("removedMessageIds" in value && Array.isArray(value.removedMessageIds))
  );
}

for (const threadId of threads) {
  const events = database
    .query<{ sequence: number; payload_json: string }, [string]>(
      `SELECT sequence, payload_json FROM orchestration_events
       WHERE aggregate_type = 'thread' AND aggregate_id = ? ORDER BY sequence`,
    )
    .all(threadId);
  const stored = new Map<string, ConversationMessage>();
  const result: ThreadResult = { updates: 0, updatesWithLostMessages: 0 };
  let loaded: ConversationMessage[] | undefined;
  for (const event of events) {
    const payload = JSON.parse(event.payload_json);
    if (!isConversationChange(payload)) continue;
    for (const message of payload.changedMessages ?? []) stored.set(message.id, message);
    for (const messageId of payload.removedMessageIds ?? []) stored.delete(messageId);
    const snapshot = sortConversationMessages([...stored.values()]).filter(drawn);
    // The chat opens on the latest page, with older pages still to load.
    if (!loaded) {
      loaded = snapshot.slice(-pageSize);
      continue;
    }
    const firstLoadedId = loaded[0]?.id;
    const next = windowedSnapshotMessages(loaded, snapshot, { mode: "latest", capped: true });
    const nextIds = new Set(next.map((message) => message.id));
    const lost = snapshot
      .slice(
        Math.max(
          0,
          snapshot.findIndex((message) => message.id === firstLoadedId),
        ),
      )
      .find((message) => !nextIds.has(message.id));
    result.updates += 1;
    if (lost) {
      result.updatesWithLostMessages += 1;
      result.firstLost ??= { sequence: Number(event.sequence), author: lost.author, createdAt: lost.createdAt };
    }
    loaded = next;
  }
  report.threads[threadId] = result;
}

mkdirSync(OUT, { recursive: true });
const failures = Object.entries(report.threads).filter(([, result]) => result.updatesWithLostMessages > 0);
report.passed = failures.length === 0;
writeFileSync(resolve(OUT, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
process.stdout.write(`${JSON.stringify(report)}\n`);
assert.equal(failures.length, 0, `The chat lost messages in ${failures.map(([threadId]) => threadId).join(", ")}.`);
