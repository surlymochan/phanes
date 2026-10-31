// Shared by migration v45 and the separate new-database schema. IF NOT EXISTS throughout, because
// this text is both the migration and the tail of the latest schema.
//
// `routine_backoff`: one row per agent routine that has run at least once with a stored answer. It
// keeps the answer fingerprint of the last run, how many runs in a row gave that same fingerprint, and
// the backoff that a long unchanged streak started. A routine with no row runs on its own schedule.
// The row ends with its routine, and a routine that is only paused keeps it.
export const ROUTINE_BACKOFF_SCHEMA_SQL = `
  CREATE TABLE IF NOT EXISTS routine_backoff (
    routine_id TEXT PRIMARY KEY REFERENCES projection_agent_routines(routine_id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    streak INTEGER NOT NULL CHECK (streak >= 1),
    backoff_steps INTEGER NOT NULL CHECK (backoff_steps >= 0),
    backoff_until TEXT,
    updated_at TEXT NOT NULL
  );
`;
