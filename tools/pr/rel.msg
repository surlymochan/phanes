Bound the backend resources that grew without a limit and end work on close

Provider account reads use the start budget, the shared database queue and the dispatch records have limits, slow providers keep their long budget, script origins, warnings and the MCP lookup cache are bounded, shutdown waits for temporary folder removal, the MCP bridge aborts active calls on close, interrupted Codex turns are bounded, and the browser tab sweep cannot overlap.
