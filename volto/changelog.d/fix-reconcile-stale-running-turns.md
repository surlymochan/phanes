### Fixed

- A finished turn no longer stays `running` in the conversation database. Before, when the chat had
  already saved the cleared turn, the turn's end was skipped as "no change" and its row stayed
  `running` forever. Startup now also closes rows left behind this way.
