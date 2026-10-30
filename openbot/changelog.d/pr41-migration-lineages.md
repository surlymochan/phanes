### Fixed

- Keep the migration history of a database when it is upgraded. The earlier repair of the upgrade
  path rewrote the marker of a migration that the database had already applied, so the version of
  the database no longer described what the database held. The repair is now append only. The
  upgrade adds the missing tables after the markers that are already there, keeps the time of each
  existing marker, and stops before it writes when the tables of the database do not match the
  tables that the migrations create. A routine backoff table that the database already has keeps its
  rows, and a database that comes from the upstream release keeps its browser profiles, workflows
  and side threads.
