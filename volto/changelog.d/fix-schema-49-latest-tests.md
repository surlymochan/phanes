### Fixed

- Align the database migration tests with schema version 49. The routine backoff migration for imported
  histories added version 49, but the migration tests were still written against version 48 as the
  latest: they expected a fully upgraded database to stop at version 48, fixtures that delete migration
  markers above a target version left the version 49 marker in place, the newer-than-supported guard
  fixtures used 49 as the future version, and the foreign-key rollback and missing-marker guards stopped
  at version 48. The tests now expect version 49, use 50 as the future version, and cover the rollback
  of version 49 and the missing version 48 marker. Two fixtures that stand for an older release now drop
  the tables that the later migrations created, so the database looks like the release it claims to be.
  No migration, schema definition, or user data changes.
