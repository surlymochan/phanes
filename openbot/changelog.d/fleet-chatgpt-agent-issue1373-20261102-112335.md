### Fixed

- Let the language checker scan examples outside the OpenBot workspace with repeated `--example-root` options. Keep required paths and unused translation checks strict when the legacy examples directory is absent.
- Scan the optional sibling examples directory from the default language check command. Test default calls, standalone checkouts, and unused keys through the real CLI.

### Review repair mapping

| Blocker | Change | Regression check | Command and result |
| --- | --- | --- | --- |
| B1: The default language check misses the moved example. | `scripts/i18n-check.ts` scans optional sibling `../examples` when no explicit root is given. Explicit roots and unused-key checks remain strict. | `default-package-command-scans-sibling-example` runs the real package command without extra options; the sibling-only key is found. | From `openbot/`: `bun run i18n:check` passed; `bun scripts/i18n-check-e2e.ts --example-root ../examples/org-health-local` passed all 7 cases. |
| B1: Keep unused translation checks active after default discovery. | The fix changes source discovery only; it does not exempt catalog keys. | `default-package-command-rejects-unused-key` and `unused-key-from-external-example` remove a source reference and require an unused-key failure. | The same E2E command passed both negative cases with the expected checker exit 1 and unused-key diagnostic. The E2E runner exited 0. |
| B1: Preserve standalone and explicit-root behavior. | Missing default example directories remain optional; explicit roots replace sibling discovery and stay required. | `default-package-command-without-sibling-root`, `repeatable-example-roots`, and `missing-explicit-root` check these boundaries. | The same 7-case E2E run passed. Saved results: `.openbot-build/i18n-check-e2e/report.json`. |

The 7-case E2E results above were recorded on `a85acdc9b4a72598b101638df269e27e4921da5b`.
The rebase for this repair leaves the checker, regression script, runtime packages, and migrated example unchanged.
This repair adds the requested B1 mapping; it does not change the implementation or tests.
