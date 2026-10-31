### Fixed

- Start Claude agents as new sessions when a Claude Code session opens OpenBot, for example with
  `open -a OpenBot`. Before, the agents got the session ID, entry point and messaging token of that
  session, and each new chat wrote its prompt cache again.
