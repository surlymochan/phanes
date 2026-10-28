# openswarm: three executor reliability fixes (roadmap #6)

Repository: openswarm (Python CLI `bin/openswarm`, tests under tests/ run with
`cd tests && python3 -m unittest <module>`). Read README.md sections on lanes, run status and risk first.

## Problems observed on 2026-10-28

1. **Stale Codex lane after a Codex update.** Codex lanes run a long-lived `codex app-server`. After the Codex binary
   was updated (ChatGPT.app auto-update), the old app-server returned exit code 0 with empty output for every shell
   command; runs then failed or drifted. Fix: when a lane accepts an assignment, compare the resolved Codex binary
   path + version (or mtime) with what the lane's app-server started with; if different, restart the app-server
   before the turn and record it in the run events. Also: if a Codex turn sees N consecutive command executions with
   exit 0 and null output, end the run as `failed` with error_class `executor_io_lost` instead of continuing.
2. **Computer Use inside executor lanes.** With shell output missing, a Codex lane called the `cua_repl` Computer Use
   MCP tool to drive Terminal/iTerm. Executor runs must not have Computer Use. Fix: start Codex executors (lane and
   one-shot) with the computer-use MCP/plugin disabled (use Codex config overrides on the command line; find the
   right key in `codex --help` / `codex app-server --help` or config docs), and treat any `mcpToolCall` with server
   `cua_repl` (or a computer-use tool surface) as an enforced risk `computer use` that stops the run.
3. **False done.** A run that ended `status=done` with no commit and no changed files (and no verify) was reported
   as done and the watch printed `DONE`. Fix: when the executor says done but there is no commit and no diff, and
   the task is not marked read-only (add a `--read-only` run/lane-assign flag for review tasks), set status
   `partial` with reason `no_change_evidence`.

## Rules

Keep changes small and in the existing style. Add tests for each fix in the matching tests/test_*.py. Run
`cd tests && python3 -m unittest test_lane test_guardrails test_watch test_workspace test_cli_golden` (update help
goldens only if you add a flag). Do not push. Do not commit (the runner commits). Report each fix and test result.
