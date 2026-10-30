# Database migrations

`openbot.db` is the source of truth of the desktop application. Nothing copies the file before an
upgrade. Every migration is therefore an irreversible operation on user data, and it must keep every
row that a released version wrote.

This note describes the migration runner in `src/backend/openbot-database-schema.ts`, the two schema
lineages that the runner supports, and the checks that guard an upgrade.

## The runner

- The registry in `src/backend/openbot-database-schema.ts` lists the migrations in numeric order.
  The first entry is the frozen version 8 compatibility baseline. The versions are contiguous, and
  the runner stops the startup when the registry has a gap or a repeat.
- The table `schema_migrations` holds one row per applied version: the version number and the time
  `applied_at` when that version ran on that computer.
- The runner refuses a database whose newest marker is above the newest version of the registry,
  because it cannot know what that version did. It also refuses a history with a hole between the
  baseline and the newest marker.
- Each migration runs in one transaction with its marker. A failure rolls back the transaction, so
  the tables and the marker of that version stay as they were. The next start runs the same version
  again.
- `PRAGMA foreign_key_check` runs before the commit of each migration. `PRAGMA quick_check(20)` runs
  after the migrations, or after a stop that did not close the file cleanly. The check reads every
  page, so it takes minutes on a large file.
- A new database does not run the migrations. `createLatestDatabase` execs `LATEST_SCHEMA_SQL` and
  writes every marker as applied. `openbot-database-schema-parity.test.ts` compares the two paths.

## Two lineages, one version number

The OpenBot fork and the upstream release number their last migrations differently.

| Operation | Upstream release | OpenBot fork |
| --- | --- | --- |
| `browser_profiles` table | 45 | 46 |
| Workflow tables | 46 | 47 |
| Side thread table | 47 | 48 |
| `routine_backoff` table | none | 45, and 49 for an imported history |

No inspected upstream release runs a routine backoff operation. The upstream history that was
inspected ends at version 47. A later upstream release can add the operation. It then gets its own
upstream version and its own fork version.

The fork keeps its own version 45. A database that comes from the upstream release already has a
marker 45, and that marker means the browser profile table there. The runner cannot use one number
for two operations, so the fork appends the upstream operations at the next versions.

## Why a marker is never rewritten

A marker is a record of the past. It says which operation ran on the computer and when it ran.

- A shipped migration must not change. The file `openbot-database-schema-history.json` records the
  schema change of every version, and the parity test fails when a recorded entry changes. A marker
  with another meaning is a change to a shipped migration.
- The time of a marker is data about the machine. Rewriting the marker replaces the time of the
  operation with the time of the repair.
- The markers are append only. The runner requires every version from the baseline to the newest
  marker, so a marker cannot be removed to make room for another meaning.

The first attempt at this repair deleted the marker of version 45 and wrote it again with the meaning
of the other lineage. That is a rewrite. The runner now never writes a marker for a version that
the database already has. It only appends new versions, and it keeps the time of every marker that
the database already has.

## What the fork appends

| Version | Operation | Skips the table when |
| --- | --- | --- |
| 45 | Creates `routine_backoff`. | Never skips. |
| 46 | Creates `browser_profiles`. | The table is already there. |
| 47 | Creates the workflow tables. | The `workflows` table is already there. |
| 48 | Creates `projection_agent_threads`, copies the task rows into it, and drops `projection_agent_tasks`. | The `projection_agent_threads` table is already there. |
| 49 | Creates `routine_backoff`. | The table is already there. |

Version 45 uses `CREATE TABLE IF NOT EXISTS`, so a database that already has a routine backoff table
keeps its rows. Version 49 gives that table to a database that comes from the upstream release. No
inspected upstream release runs a routine backoff operation, so such a database has no backoff
table. Version 48 does not copy a task row whose conversation is gone, because that row has no
thread to move to.

## The schema evidence

The runner does not trust a table name. Before it writes, it builds an empty database in memory and
executes the five reserved schema parts in it. It then compares each declaration of that reference
with the declaration in the database of the user. The comparison uses the token sequence of the
`CREATE` text of `sqlite_master`, not the text itself. White space and comments are dropped. A
word and a quoted name become upper case, so the case of a name or a keyword does not matter. A
string literal keeps its case, because it is data. `IF NOT EXISTS` is dropped, because only one
path writes it. The lookup of a name uses `COLLATE NOCASE`, so a table that was written in another
case is still found. A table that matches in name but not in columns, checks, keys or indexes does
not match.

The five reserved parts are:

- `routine_backoff`.
- `browser_profiles`.
- The workflow tables and their indexes.
- `projection_agent_threads` and its two indexes.
- `projection_agent_tasks` and its index.

The runner stops before it writes when:

- A declaration differs from the reference: `OpenBot database has an unknown reserved schema: <name>.`
- A part is only half there: `OpenBot database has an incomplete reserved schema.`
- The database has an object with a reserved name that the reference does not have:
  `OpenBot database has an unknown reserved schema object.`
- The shape of the database does not match the marker history:
  `OpenBot database has a contradictory reserved schema and migration history.`

The check runs twice. It runs before the first pending migration. It runs again inside the
transaction of each migration from version 46, while the write lock is held and before the
migration skips an imported table. The second run reads the same state that the migration acts on,
because no other process can write while the lock is held.

## The states the runner accepts

The shape of the reserved tables must match the marker. The `fork` label means a database of the
OpenBot fork. The `imported` label means a database that came from the upstream release. The runner
accepts these states:

| Marker | Shape |
| --- | --- |
| 8 to 37 | No reserved table at all. |
| 38 to 44 | The task table only. |
| 45, fork | `routine_backoff` and the task table. |
| 45, imported | `browser_profiles` and the task table. |
| 46, fork | `routine_backoff`, `browser_profiles`, and the task table. |
| 46, imported | `browser_profiles` and the task table, with or without the workflow tables. |
| 47, fork | `routine_backoff`, `browser_profiles`, the workflow tables, and the task table. |
| 47, imported | `browser_profiles` and the workflow tables, with either the task table or the side thread table. |
| 48, fork | `routine_backoff`, `browser_profiles`, the workflow tables, and the side thread table. |
| 48, imported | `browser_profiles`, the workflow tables, and the side thread table. |
| 49 | The same shape as the fork at 48. |

The states between the two lineages are retry states, not marker rewrites. An imported database at
marker 45 can commit the fork marker 46 without the workflow tables, because the browser profiles
were already there. An imported database at marker 46 can commit the fork marker 47 without the side
thread table. An imported database at marker 47 can commit the fork marker 48 before the runner
adds the routine backoff table at 49. An imported database that already has the routine backoff
table has the same shape as the fork at version 48, and the runner accepts that shape as well.

## A failed migration

A migration that fails rolls back. The tables and the marker of that version stay as they were, and
the next start runs the same version again. The runner keeps the foreign-key setting of the
database, and it reports the failure with the version that failed.

The tests cover this for every supported source. A trigger refuses the marker of a later version,
the runner reports the failure, the data of the source stays the same, and the next start finishes
the upgrade. The routine backoff rows of a fork database stay the same through the failure and
through the retry.

## A new database and an upgraded database

A new database and an upgraded database must end with the same tables, columns, indexes, foreign
keys and markers. The runner therefore keeps two declarations of the schema: `LATEST_SCHEMA_SQL`
for a new database, and the migration list for an upgrade. The parity test builds a database both
ways and compares them.

The migration to version 49 does not change the schema of a database that already has the routine
backoff table, so the two paths agree at version 49.

## Source files and tests

- `src/backend/openbot-database-schema.ts` holds the migration registry, the reserved schema check,
  and the two build paths.
- `src/backend/openbot-database-schema-history.json` holds the recorded schema change of every
  version of the fork. Each entry names the objects that the version added, changed or dropped, and
  holds the text of the declaration. A dropped object has the value `null`. The value is the
  declaration itself and not a hash of it.
- `src/backend/openbot-database-upstream-schema-history.json` holds the recorded schema change of
  the upstream versions 45, 46 and 47. The parity test finds each of these changes at the next fork
  version: the upstream 45 at 46, the upstream 46 at 47 and the upstream 47 at 48.
- `src/backend/routine-backoff-migration.test.ts` covers the backoff table, the fork and the
  imported registries, the retry after a failure, and the rejection of a wrong shape.
- `src/backend/browser-profile-migration.test.ts` covers the profile table for every released
  version.
- `src/backend/workflow-migration.test.ts` covers the workflow tables for every released version.
- `src/backend/agent-thread-migration.test.ts` covers the move of the task rows into the side thread
  table.
- `src/backend/openbot-database.test.ts` covers the upgrade of the whole database.
- `src/backend/openbot-database-schema-parity.test.ts` covers the two build paths and the two
  history files.

## Limits

- The newest marker that the runner accepts is the newest version of the registry, which is 49 at the
  time of this note. A marker above it stops the startup, and the user data stays as it is.
- The markers must run from the version 8 baseline to the newest marker without a hole.
- The runner reads every page of the file for the integrity check. The check therefore runs only
  after a migration, or after a stop that did not close the file cleanly.
- No migration makes a copy of `openbot.db`. The upgrade must be safe on its own, on every computer
  that runs it.
