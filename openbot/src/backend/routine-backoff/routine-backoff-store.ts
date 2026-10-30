/**
 * The no-progress backoff of agent routines. Owns the `routine_backoff` rows and the rule that
 * sets them. It reads and writes only that table: the routine, run and thread tables belong to
 * their own stores.
 *
 * Rule: a run whose answer fingerprint equals the previous run's extends the streak. At
 * `UNCHANGED_RUNS_BEFORE_BACKOFF` runs in a row, each further unchanged run doubles the wait before
 * the next run, from one hour, up to four hours. A run with a new fingerprint ends the streak and the
 * backoff. While the streak holds and the thread has no new activity, a due occurrence is skipped
 * without a model turn.
 */
import { createHash } from "node:crypto";
import {
  databaseRow,
  optionalStringColumn,
  requiredNumberColumn,
  requiredStringColumn,
} from "../database/database-rows";
import type { OpenBotDatabase } from "../openbot-database";

const UNCHANGED_RUNS_BEFORE_BACKOFF = 3;
const FIRST_BACKOFF_MS = 60 * 60_000;
const MAX_BACKOFF_MS = 4 * 60 * 60_000;

export interface RoutineBackoffRecord {
  fingerprint: string;
  streak: number;
  backoffSteps: number;
  backoffUntil: string | null;
  updatedAt: string;
}

export type RoutineBackoffDecision = "run" | "skip-backoff" | "skip-quiet";

export interface RoutineBackoffStoreOptions {
  database: OpenBotDatabase;
}

/** The wait after `steps` unchanged backoff steps: one hour, doubling, capped at four hours. */
export function backoffDurationMs(steps: number): number {
  if (steps < 1) return 0;
  return Math.min(MAX_BACKOFF_MS, FIRST_BACKOFF_MS * 2 ** (steps - 1));
}

/** The fingerprint of one run's answer. Text that is only a time stamp does not change it. */
export function answerFingerprint(text: string): string {
  const normalized = text
    .replace(/\b\d{1,2}:\d{2}(?::\d{2})?\b/gu, " ")
    .replace(/\b\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+Z?)?\b/gu, " ")
    .replace(/\b(?:SGT|UTC|GMT|CST|PST|PDT|CET)\b/giu, " ")
    .replace(/\s+/gu, " ")
    .trim()
    .toLowerCase();
  return createHash("sha256").update(normalized).digest("hex");
}

export class RoutineBackoffStore {
  readonly #database: OpenBotDatabase;

  constructor(options: RoutineBackoffStoreOptions) {
    this.#database = options.database;
  }

  get(routineId: string): RoutineBackoffRecord | null {
    const row = databaseRow(
      this.#database.connection
        .prepare(
          "SELECT fingerprint, streak, backoff_steps, backoff_until, updated_at FROM routine_backoff WHERE routine_id = ?",
        )
        .get(routineId),
    );
    if (!row) return null;
    return {
      fingerprint: requiredStringColumn(row, "fingerprint"),
      streak: requiredNumberColumn(row, "streak"),
      backoffSteps: requiredNumberColumn(row, "backoff_steps"),
      backoffUntil: optionalStringColumn(row, "backoff_until"),
      updatedAt: requiredStringColumn(row, "updated_at"),
    };
  }

  /**
   * Decides whether a due occurrence runs. `scheduledFor` is the occurrence's time, and
   * `hasNewActivity` says whether the thread changed since the last run. A backoff that is still open
   * at the occurrence skips it. A held streak with no new activity skips it too.
   */
  decide(routineId: string, scheduledFor: Date, hasNewActivity: boolean): RoutineBackoffDecision {
    const record = this.get(routineId);
    if (!record) return "run";
    if (record.backoffUntil !== null && Date.parse(record.backoffUntil) > scheduledFor.getTime()) return "skip-backoff";
    if (record.streak >= UNCHANGED_RUNS_BEFORE_BACKOFF && !hasNewActivity) return "skip-quiet";
    return "run";
  }

  /** Records a finished run's answer fingerprint and applies the rule. */
  recordRun(routineId: string, fingerprint: string, now: Date): RoutineBackoffRecord {
    const previous = this.get(routineId);
    const sameAsBefore = previous !== null && previous.fingerprint === fingerprint;
    const streak = sameAsBefore ? previous.streak + 1 : 1;
    const steps = sameAsBefore && streak > UNCHANGED_RUNS_BEFORE_BACKOFF ? previous.backoffSteps + 1 : 0;
    const backoffSteps = sameAsBefore && streak >= UNCHANGED_RUNS_BEFORE_BACKOFF ? Math.max(steps, 1) : 0;
    const backoffUntil =
      backoffSteps > 0 ? new Date(now.getTime() + backoffDurationMs(backoffSteps)).toISOString() : null;
    const updatedAt = now.toISOString();
    this.#database.connection
      .prepare(
        `INSERT INTO routine_backoff (routine_id, fingerprint, streak, backoff_steps, backoff_until, updated_at)
         VALUES (?, ?, ?, ?, ?, ?)
         ON CONFLICT(routine_id) DO UPDATE SET fingerprint = excluded.fingerprint, streak = excluded.streak,
           backoff_steps = excluded.backoff_steps, backoff_until = excluded.backoff_until,
           updated_at = excluded.updated_at`,
      )
      .run(routineId, fingerprint, streak, backoffSteps, backoffUntil, updatedAt);
    return { fingerprint, streak, backoffSteps, backoffUntil, updatedAt };
  }
}
