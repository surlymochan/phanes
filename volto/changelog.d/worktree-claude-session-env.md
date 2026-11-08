### Fixed

- Start Claude agents as new sessions when a Claude Code session opens volto, for example with
  `open -a volto`. Before, the agents got the session ID, entry point and messaging token of that
  session, and each new chat wrote its prompt cache again.
