# Issue #1869 cleanup inventory / card-comment attachment

This is the local, reviewable card-comment attachment; no GitHub write was performed.
Scanned tracked trees: local `main` (7869dad74), batch base `1f0346dc6`, and reviewed head `8f2bca363`.
Commands: `git ls-tree -r <ref> --name-only -- openswarm/tests swrm/tests tools`; inspect Markdown/publish inputs and fixture paths; check artifact names below.
Current head has 5 openswarm/tests files, 68 swrm/tests files and 255 tools files (before this attachment).
Old openswarm executor filtering maps to `swrm/bin/swrm`; regression coverage maps to `swrm/tests/test_executor_artifact_filter.py`.

## Removal checklist

| Path | Decision / evidence |
| --- | --- |
| openswarm/tests/openswarm-card-412ygy9g.md | Removed before batch base; absent at reviewed head. Old main still has `card two`. The retained regression excludes this exact name. |
| openswarm/tests/.dde4fddfefb6d431-00000000.so | Removed before batch base (#1941); absent at reviewed head. |
| openswarm/tests/.dde4fddffebe9e3e-00000000.so | Removed before batch base (#1941); absent at reviewed head. |
| openswarm/tests/.dde4fdffffb79e7f-00000000.so | Removed before batch base (#1941); absent at reviewed head. |
| openswarm/tests/.dde4fdffffbfd53a-00000000.so | Removed before batch base (#1941); absent at reviewed head. |

No additional tracked `openswarm-card-*`, `.so`, `.pyc`, `__pycache__`, `.tmp`, `.bak` or `.log` artifacts remain in the scanned head paths.
No further deletion is warranted: the other non-code files below have substantive task, documentation, publishing or fixture content, rather than the leaked one-line sample.

## tools/cards/: all 85 Markdown files inspected

Each row is retained as a task specification or reusable template; the final column records its concrete purpose.
These are intentional repository task documents: `tools/mkcard.py` builds scoped cards, `tools/dispatch.sh` accepts cards via `PHANES_CARDS_DIR`.
`docs/audit/issue-17-card-baselines.md` and `docs/reviews/issue17-regression-cards.md` explicitly cite c1-58/c1-58b as audit evidence.
`docs/audit/swrm-merge-review-2.md` also records maintaining the two swrm task cards. Filenames containing “test” are task documents, not executable tests.

| Path (under tools/cards/) | Decision | Purpose from document |
| --- | --- | --- |
| b1-err.md | Keep | M10 #63: catalog keys for raw error messages (errors 4, 8, 11) |
| b1-test.md | Keep | M10 #66: E2E for channel chat Stop, Resume, Reassign, Archive, Restore (tests 3) |
| b1-test11.md | Keep | M10 #66: E2E for signing in to a server with volto login (tests 11) |
| b1-test11b.md | Keep | M10 #66: E2E for signing in to a server with volto login (tests 11) |
| b1-test2.md | Keep | M10 #66: E2E for channel chat Stop, Resume, Reassign, Archive, Restore (tests 3) |
| b2-err.md | Keep | M10 #63: bounded retries and visible failures (errors 6, 9, 13) |
| b2-err2.md | Keep | M10 #63: bounded retries and visible failures (errors 6, 9, 13) |
| b2-rebase38.md | Keep | Re-apply PR #38 (dispatch section of the agents architecture note) on the new main |
| b2-test.md | Keep | M10 #66: E2E for importing a Grok Bot export (tests 2) |
| b3-mobilegaps.md | Keep | M10 #65: mobile gaps below the top 15 (client-only, up to 3) |
| b3-rebase33.md | Keep | Re-apply PR #33 (iOS self-build guide) and PR #36 (PRIVACY note on coding dispatch) on the new main |
| b3-rebase33b.md | Keep | Re-apply PR #33 (iOS self-build guide) and PR #36 (PRIVACY note on coding dispatch) on the new main |
| b3-test.md | Keep | M10 #66: E2E for media attachments and size limits (tests 10) |
| b3-test2.md | Keep | M10 #66: E2E for media attachments and size limits (tests 10) |
| b3-test3.md | Keep | M10 #66: E2E for a signed webhook request that starts a routine (tests 1) |
| b3-test4.md | Keep | M10 #66: E2E for a signed webhook request that starts a routine (tests 1) |
| b3-test7.md | Keep | M10 #66: E2E for answering a Telegram chat (tests 7) |
| b4-ratchet.md | Keep | Fix the ratcheted TypeScript errors in three new E2E scripts |
| b4-test.md | Keep | M10 #66: E2E for creating an agent and editing its profile (tests 4) |
| b4-test2.md | Keep | M10 #66: E2E for creating, revising and installing a local skill (tests 6) |
| b4-test3.md | Keep | M10 #66: E2E for installing a plugin and opening volto://plugins/<slug> (tests 12) |
| b4-test5.md | Keep | M10 #66: E2E for exporting the local data (tests 5) |
| b4-test5b.md | Keep | M10 #66: E2E for exporting the local data (tests 5) |
| b4-test9.md | Keep | M10 #66: E2E for downloading a provider runtime (tests 9) |
| c1-58.md | Keep | agent-knowledge #58: make two regression cards prove a change (verify fails on base, passes after) |
| c1-58b.md | Keep | agent-knowledge #58: make two regression cards prove a change (verify fails on base, passes after) |
| c1-fork-e2e.md | Keep | Two fork E2E checks fail after the upstream sync: steer/interrupt/queue pause and backup model failover |
| c1-fork-e2e2.md | Keep | Continue: two fork E2E checks still fail after the upstream sync |
| c1-fork-e2e3.md | Keep | Fix two fork E2E regressions after the upstream sync (third attempt: make a code change) |
| c1-fork-e2e4.md | Keep | Fix the last two fork E2E failures (CI shows the exact errors; the fake provider CLI cannot start in your sandbox, so work from code and these logs) |
| c1-fork-e2e5.md | Keep | Fix a regression that your last change caused in the token budget E2E |
| c1-mix.md | Keep | M10 #64/#63: browser tab sweep overlap (reliability 15) and error codes in Team API error bodies (errors 10) |
| c1-perf-typecheck.md | Keep | Make the renderer performance changes pass `bun run typecheck:node` |
| c1-pr40.md | Keep | PR nibiru-labs/volto#40: re-apply the auth-claim E2E and its process-group cleanup on the new main |
| c1-proto-merge.md | Keep | Combine two Team API capabilities and their phone UI on the new main |
| c1-protocol-review.md | Keep | Read-only review: two proposed Team API additions from mobile parity work |
| c1-rel.md | Keep | M10 #64: fix backend reliability findings 1-3 |
| c1-rel2.md | Keep | M10 #64: fix backend reliability findings 4-5 |
| c1-rel3.md | Keep | M10 #64: fix backend reliability findings 6-8 |
| c1-rel4.md | Keep | M10 #64: finish backend reliability findings 6 and 8 |
| c1-rel5.md | Keep | M10 #64: verify finding 8, then fix reliability findings 9-11 |
| c1-rel6.md | Keep | M10 #64: verify finding 8, then fix reliability findings 9-11 |
| c1-sweep.md | Keep | M10 #64: E2E for the browser tab sweep overlap guard (reliability 15) |
| fix-ctx.md | Keep | Fix the context-usage-v1 Team API addition before it can ship |
| fix-pause.md | Keep | Fix the pause/continue Team API addition before it can ship |
| fix-pause2.md | Keep | Fix the pause/continue Team API addition before it can ship |
| k-a11y.md | Keep | M10 #62: keyboard access to context menus (a11y findings 1 and 15) |
| k-a11y2.md | Keep | M10 #62: sidebar keyboard reorder and accessible names (a11y 2-3) |
| k-a11y3.md | Keep | M10 #62: selection toolbar focus and tab roles (a11y 4-5) |
| k-a11y4.md | Keep | M10 #62: routine status, toolbar arrow keys, onboarding focus order (a11y 6-8) |
| k-a11y5.md | Keep | M10 #62: reorder shortcut, aria-controls, dialog button state (a11y 9-11) |
| k-err.md | Keep | M10 #63: keep error codes through runCauseEffect and agent error events (errors 1-2) |
| k-err2.md | Keep | M10 #63: Team API routes must not forward raw internal messages (errors 3) |
| k-err3.md | Keep | M10 #63: bounded hosted-server start retry and visible dead CDP sessions (errors 12, 14) |
| k-err4.md | Keep | M10 #63: queued message failure reason without raw provider text (errors 15) |
| k-perf.md | Keep | M10 #61: fix renderer performance findings 1-2 |
| k-perf2.md | Keep | M10 #61: make the streaming parse change pay off, or revert it (perf finding 2) |
| k2-issue8.md | Keep | Issue #8 acceptance: real-renderer E2E for non-empty response process notes and the working avatar |
| k2-issue8b.md | Keep | Issue #8 acceptance (continue): real-renderer E2E for non-empty response process notes and the working avatar |
| k2-perf3.md | Keep | M10 #61: renderer performance findings 3, 6 and 7 |
| k2-perf4.md | Keep | M10 #61: renderer performance findings 4 and 12 |
| k2-perf5.md | Keep | M10 #61: renderer performance findings 8 and 9 |
| k3-cli-regress.md | Keep | Fix two failing checks of the Local scripts CLI E2E on the errors branch |
| k3-mobile.md | Keep | M10 #65: mobile composer skill picker and context usage meter (mobile 10, 12) |
| k3-mobile2.md | Keep | M10 #65: mobile interactive prompt block and in-chat search (mobile 1, 14) |
| k3-perf.md | Keep | M10 #61: renderer performance findings 13-15 |
| k3-perf10.md | Keep | M10 #61: sidebar draws every chat row (perf 10) |
| k3-rebase37.md | Keep | Re-apply PR #37 and #39 (dispatch E2E scripts) on the new main |
| k3-rel.md | Keep | M10 #64: backend reliability findings 12-14 |
| k3-rel3.md | Keep | M10 #64: the watcher of a dispatch run never ends (reliability 3) |
| k4-a11y6.md | Keep | M10 #62: accessibility findings 12-14 |
| k4-a11yfix.md | Keep | Fix the type errors in the two accessibility E2E scripts |
| k4-err.md | Keep | M10 #63: provider status messages and the hourly provider update check (errors 5, 7) |
| k4-mobile-merge.md | Keep | Combine four mobile commits on the new main and resolve their conflicts |
| k4-mobile.md | Keep | M10 #65: mobile paused-agent marker with Continue, and readable turn process notes (mobile 2-3) |
| k4-mobile2.md | Keep | M10 #65: mobile file preview for other files, unread divider and day separators (mobile 4, 15) |
| k4-mobile3.md | Keep | M10 #65: mobile skill and MCP tags, dropped markers, chat visual page (mobile 6, 7, 9) |
| k4-perf11.md | Keep | M10 #61: file list draws every file row (perf 11) |
| k4-perf5.md | Keep | M10 #61: @volto/ui barrel keeps every primitive in the main chunk (perf 5), as a re-runnable codemod |
| k4-rebase32.md | Keep | Re-apply PR #32 (architecture docs back in line with the code) on the new main |
| k4-test14.md | Keep | M10 #66: E2E for publishing this computer to the account service (tests 14) |
| k4-test15.md | Keep | M10 #66: E2E for signing in and sending a message in the browser client (tests 15) |
| k4-test8.md | Keep | M10 #66: E2E for checking, downloading and relaunching for an update (tests 8) |
| os-fixes.md | Keep | swrm: three executor reliability fixes (roadmap #6) |
| verify-intent-template.md | Keep | Verify intent card template |

## tools/pr/: all 32 publish inputs inspected

These contain commit subjects/details and structured PR descriptions (linked issue, changes, verification, risk), consumed as message/body files by `pub.sh` / `pub2.sh`.
They are retained publishing records, not executor session output. `mkpr.py` documents the `.msg` / `.body` format. Grouped pairs below list every path.

| Path(s) under tools/pr/ | Decision / content |
| --- | --- |
| a11y.body, a11y.msg | Keep: Give the context menus, sidebar rows and controls a keyboard path and accessible names |
| docs3336.body, docs3336.msg | Keep: Document the iOS self-build and what a coding dispatch sends |
| errors.body, errors.msg | Keep: Show catalog messages and codes for failures that showed raw text or nothing |
| fix2.msg | Keep: Fix usage-partial stop and held turn after interrupts |
| fix47b.msg | Keep: Keep the original cause when the automation server reports a refusal |
| fork-e2e-fix.body, fork-e2e-fix.msg | Keep: Restore fork behavior lost in the upstream sync |
| k2-renderer.body, k2-renderer.msg | Keep: Add a real-renderer E2E for the response process view |
| login-e2e.msg | Keep: Add login E2E script |
| m32.body, m32.msg | Keep: Bring the architecture docs back in line with the code |
| m37.body, m37.msg | Keep: Cover approval deny, timeout, grant and revoke in the dispatch E2E |
| m40.body, m40.msg | Keep: Prove server claims with only a local account service |
| mobile.body, mobile.msg | Keep: Bring the phone chat closer to the desktop: prompts, search, markers and dividers |
| perf.body, perf.msg | Keep: Cut the renderer cost of long chats, big lists and the sidebar |
| proto.body, proto.msg | Keep: Let a phone continue a paused agent and read the context usage |
| ratchet-fix.msg | Keep: Fix exactOptionalPropertyTypes errors in new E2E scripts |
| rel.body, rel.msg | Keep: Bound the backend resources that grew without a limit and end work on close |
| rel3.body, rel3.msg | Keep: End the watcher of a dispatch run that never finishes |
| tests.body, tests.msg | Keep: Add E2E scripts for the flows that had no test |

## Other scanned files

| Paths | Decision / evidence |
| --- | --- |
| openswarm/tests/fixtures/card_thin_552.md, card_thin_560.md, card_thin_568.md | Keep: five-line, issue-specific thin-card fixtures; not the one-line executor leak. No modification under the deprecated directory. |
| openswarm/tests/test_dispatch_leak_sweep.py, test_fusion_agreed_v02.py | Keep: real unittest suites for #1493 / #1481, even though the former references removed modules. Not cleanup artifacts. |
| tools/autodispatch-backend-fallback.md | Keep: queue selection policy, with runtime and test references. |
| tools/ci-swrm.md (old main: tools/ci-openswarm.md) | Keep: CI gate contract, migrated name. |
| tools/deployment-scripts.md | Keep: deployment script configuration reference. |
| tools/lane-autodispatch/README.md | Keep: autodispatch operator documentation. |
| tools/deployment.env.example, tools/lane-autodispatch/start-control-plane.example | Keep: deployment configuration examples. |
| tools/tests/fixtures/ | Keep all 12 files: 6 tester-risk issue/PR JSON inputs, 2 evalid-role JSON inputs, 2 native-atom Python fixtures, mobile-account-service harness/loader MJS. Used by corresponding tools/tests suites. |
| swrm/tests/fixtures/, swrm/tests/golden/ | Keep all 8 fixtures and 6 golden text files: fake executor/gh scripts, ocgo policy snapshot, state input, help/service output assertions. |
| Old main openswarm/tests/fixtures/ and golden/ other than listed card fixtures | Real test inputs (fleet JSON/status, pause marker, state and CLI goldens); removed or migrated by #1894 before this batch. No new cleanup needed. |
| Remaining tracked .py / .sh / .ts files under the scanned paths | Keep: implementation, command-line scripts and executable tests, including tools/rehearse.ts (database migration rehearsal); no non-code temporary output found. |

## Verification / handoff

Keep the original ad12adea4 reproduction and the finalize_workspace integration test. This revision only adds the missing inventory.
Narrow coverage passed: `cd swrm/tests && python3 -m unittest test_executor_artifact_filter test_workspace -v` — 30 tests, OK (TMPDIR inside workspace).
Run `git diff --check`. On reviewed head, `git ls-tree -r HEAD --name-only | grep openswarm-card-` emits nothing (grep exit 1 is expected).
Issue command `git ls-tree -r main --name-only | grep openswarm-card-` still prints the sample on the old local main; repeat on target main after merge.
Full migrated OpenSwarm tests: `cd swrm && python3 -m unittest discover -s tests -v` (host/CI lane, per dispatch prohibition on full discovery here).
Planner can attach this inventory to the card comment when authorized; this execution makes no GitHub writes.

Inventory completeness check passed: every tools/cards and tools/pr filename appears above; batch additions including this attachment total 235 lines (limit 250).
