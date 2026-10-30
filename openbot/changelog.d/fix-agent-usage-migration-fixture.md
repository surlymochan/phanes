### Fixed

- Repair the fixture of the agent usage migration. The fixture stood for a database of version 14
  but was built from the latest schema with the later tables removed, so it carried the columns
  that the later versions added: a shape no release ever wrote. The fixture is now a database of
  version 14 as the migrations built it, it holds the rows of a conversation, and the upgrade runs
  from it to version 49. A failed upgrade still rolls back and still retries. The production guard,
  the migration versions and the schema definitions stay the same. No migration, schema definition,
  or user data changes.
