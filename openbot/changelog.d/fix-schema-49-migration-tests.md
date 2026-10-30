### Fixed

- Align the remaining database migration tests with schema version 49. The routine backoff migration
  for imported histories added version 49, but these tests still expected a fully upgraded database
  to stop at version 48, and their newer-than-supported fixtures used version 49 as the future
  version. The tests now expect version 49 and use version 50 as the future version. The fixtures
  that stand for an older release keep their own version.
- Correct the expected table set of the agent task migration. The fixture stops at version 46, so
  the database has the task, role, backoff and browser profile tables, but no workflow table and no
  side thread table. The comparison now names the tables that the migrations from 38 to 46 create,
  and it stays a whole-database check. No migration, schema definition, or user data changes.
