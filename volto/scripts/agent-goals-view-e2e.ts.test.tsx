// Bun only discovers a test file whose name carries `.test` or `.spec`, and the acceptance command
// of #1991 filters by the path of the checks themselves. This file carries both: it is the file
// `bun test scripts/agent-goals-view-e2e.ts` finds, and it runs the checks of that file.
import "./agent-goals-view-e2e.ts";
