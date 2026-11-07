# Review fixes

- [x] Restore original sound paths and all platform consumers.
- [x] Restore external release URLs and protected baseline documents.
- [x] Add and verify Compose/Docker legacy source-commit fallback.
- [x] Audit and preserve stored data and external integration identities.
- [x] Check existing assets and public content URLs.
- [x] Install both trees with frozen locks and no install hooks.
- [x] Run all 15 project typechecks sequentially and both unit suites with one worker in both trees.
- [x] Fix the two initial Windows migration-fixture failures; rerun the final full current checks.
- [x] Publish baseline/current failure lists, category inventory and changed-file manifest.

# Final review fixes

- [x] Align release asset names and retained installed identity documentation.
- [x] Reproduce baseline and current checks in isolated environments.
- [x] Regenerate tracked name counts and final review evidence.

# Rebase onto main-latest

- [x] Rebase the single rename commit onto `main-latest` and preserve main additions.
- [x] Rename main-latest documentation and path references; refresh inventory and changed-file manifest.
- [x] Run baseline and current Volto checks, plus focused Swrm tests.

# Safe-storage review fixes

- [x] Restrict environment detection and cover inactive values.
- [x] Add flags to all six standalone browser harnesses.
- [x] Run real Electron isolation probe and unit tests.
- [x] Complete changed-file lint and node typecheck.
- [x] Run browser user-control E2E: 27 cases passed.

# Safe-storage second review fixes

- [x] Add explicit basic storage and a test marker to both Linux package launches.
- [x] Verify the macOS mock backend with independent evidence: 14 real Electron cases passed.
- [x] Run isolated launch checks and changed-file lint; confirm 96 node type errors match the baseline. Full Linux package verification needs Linux.
