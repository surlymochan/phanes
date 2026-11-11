# FLAKY-ROOT investigation

Baseline: `cb8a8235d6f0dd110652a48d1cf46cf285658439` (verified before edits).

## Culprit and reproduction

`test_agy.py:155-156` (`AgyBackend.test_success_parses_result_and_records_usage`)
→ `dispatch` (`test_agy.py:102`) → `cmd_run` (`bin/swrm:5942`)
→ `run_executor_with_rate_limit` (`4400`) → `exec_agy` (`5229`)
→ `run_proc` (`4885`) → `cleanup_executor_tmp` (`4685-4690`)
→ `executor_worktree_root` (`4630-4636`) → `git rev-parse --show-toplevel`.

The fixture's `work` directory (`test_agy.py:73-74`) has no Git repository.
When TMPDIR is inside the checkout, Git discovers the enclosing real repository.
Cleanup consequently removes the real checkout's entire `.swrm-tmp`. This is
not a direct `rmtree(SCRATCH)` in the test. Cursor/Pi and other executor tests
with unversioned fixture directories can reach the same helper.

Before edits, `python3 "$TMPDIR/repro.py"` used two forked processes and a pipe
barrier: the old pause setup from `2432a545e` created its store, then the actual
Agy success test ran in the child. Actual output:

```
killer failures/errors: (0, 0)
test_legacy_filename_can_be_read_and_resumed REPRODUCED FileNotFoundError
killer failures/errors: (0, 0)
test_independent_holds_match_only_their_dispatch_target is_paused = False
```

Both symptoms have the same cause. The legacy test writes into a removed
`pauses` directory. The independent-holds test reads a missing directory as an
empty store and returns False. The barrier deterministically selects the
otherwise timing-dependent interleaving; this is not a random stress test.
The worktree remained at the requested HEAD; only the victim source was loaded
from the parent revision. An entire historical checkout was not run.

The retained reproducer is `tools/reproduce_test_tmp_race.py`; run
`python3 tools/reproduce_test_tmp_race.py --expect-isolated` after this fix.
It produced two zero-error Agy results, `isolated`, and `is_paused = True`.

## Resource inventory and handling

Line numbers below identify the original baseline unless marked as new.
Search scope: every Python source in `swrm/tests` and `tools/tests`, including
helpers and generated fixture programs. Searches included `rmtree`, `rm -rf`,
`.tmp`, `.swrm-tmp`, `short_tmp_root`, environment assignments/pop/update,
`chdir`, `bind`, listening ports and absolute temporary/socket paths.

| Location | Risk | Handling |
| --- | --- | --- |
| `swrm/tests/_broker_wait.py:9-13` | Returns shared `/tmp` itself | Every invocation now owns an unpredictable directory in ignored `.t`; registered owner PID and allocation registry restrict cleanup; shared parent is never removed |
| `swrm/tests/_load.py:8-20` | Import mutates process environment; unversioned fixtures discover real checkout | Preserve original environment for exit restoration; Git ceiling prevents enclosing checkout discovery; cleanup wrapper rejects real checkout even under mocked Git discovery |
| `tools/tests/test_ci_swrm_gate.py:20,92-94` | Shared repo scratch parent | Common private allocator; per-test TemporaryDirectory retained |
| `tools/tests/test_autodispatch_host_recovery.py:61,210,302,386` | Shared scratch parent | Common private allocator; cleanup remains on allocated children |
| `tools/tests/test_hot_preflight_1721.py:59,298` | Shared scratch parent | Common private allocator |
| `tools/tests/test_card_pre_review_1732.py:147-149,258` | Shared scratch parent | Common private allocator |
| `tools/tests/test_disk_watermark_guard.py:70-71,78` | Fixed `.tmp/disk-watermark-tests` | Common private allocator |
| `tools/tests/test_impl_card_task_book.py:83-85` | Shared `.tmp` parent | Common private allocator |
| `tools/tests/test_fix_round_brief.py:104-106` | Shared `.tmp` parent | Common private allocator |
| `tools/tests/test_executor_task_health.py:406-411` | Shared `.tmp` parent | Common private allocator; cleanup still removes only own child |
| `tools/tests/test_mm_burn_rate.py:126-127` | Shared `.swrm-tmp` parent | Common private allocator |
| `swrm/tests/test_workspace.py:434,450-463` | Deletes pre-existing plan env values; pop cleanup loses original values | Snapshot/restore entire environment in CredentialOptInTest |
| `swrm/tests/test_lane_delivery_map.py:67` | Deletes pre-existing validation mode | Snapshot/restore environment |
| `tools/tests/test_lane_pr_validation.py:274-287` | Deletes/overwrites pre-existing validation mode | Snapshot/restore; clear inherited mode before testing defaults |
| `swrm/tests/test_bench.py:31,56,81` | mkdtemp fixtures never cleaned | Register owned-directory cleanup |
| `swrm/tests/test_agy.py:75-90,148-153`; `test_cursor.py:59-76,122-140`; `test_pi.py:70-85`; `test_consult.py:122-140`; `test_bus.py:47-75` | Global env mutation | Existing saved-value/finally/tearDown restoration; Git ceiling covers their unversioned executor fixtures |
| `swrm/tests/test_autodispatch_lanes_snapshot.py:57-66`, `test_fleet_health_cli.py:57-70,398-416`; `tools/tests/test_lanes_status_snapshot.py:57-69`, `test_lane_pr_validation.py:43-54`, `test_disk_watermark_guard.py:159-170` | Temporary process env mutation | Existing finally/context-manager restoration retained |
| `swrm/tests/test_fleet_capacity.py:85-97`; `test_fleet_health_pause.py:126-150`; `test_dispatch_pause.py:25-27`; `tools/tests/test_autodispatch_host_recovery.py:230-237`, `test_hot_preflight_1721.py:301-311` | Temporary env mutation | Existing saved-value or patch.dict restoration retained |
| `swrm/tests/test_lane.py:286,304` | Absolute Darwin fallback socket | Hash derived from each private randomly allocated fixture; production-path contract, not a globally fixed socket; existing platform condition retained |
| `swrm/tests/test_pi_isolation.py:89,104,143,194,240`; `test_lane.py:506`; `test_fleet_auto.py:201`; `test_pi_broker_service.py:86,102`; `test_pi_worker_proxy.py:93,109`; `test_pi_broker_stdio.py:42,91`; `test_copilot_gui_run.py:92`; `tools/tests/test_mm_burn_rate_hourly.py:106` | Socket collisions | Socket filenames are inside unique fixture directories; no shared listener path |
| `swrm/tests/test_pi_local_http_bridge.py:38,94`, broker HTTP fixtures | Listening port collisions | Existing OS-assigned ports retained; guard rejects literal nonzero bind/server ports |
| `swrm/tests/test_guardrails.py:235,263,269`; `tools/tests/test_forbidden_paths.py:49,134`; `test_mm_burn_rate.py:331` | Shared-looking literal paths | Assertion/parser inputs or mocked paths, not real shared resource allocations; retained |
| `swrm/tests/test_consult.py:422,426,435`; other rmtree calls | Cleanup ownership | Allocated fixture child directories or scratch Git metadata; no direct shared root deletion found |
| Both suites, `os.chdir` search | Process cwd leakage | No direct calls found; subprocess `cwd=` does not mutate parent cwd |

## Guard mechanism

`tools/test_isolation_guard.py` scans executable Python expressions through
both existing suite entry points. It rejects repository scratch constructions,
shared literal rmtree targets, nonzero literal listening ports, and fixed
literal `/tmp` or `/run` socket binds. Self-tests intentionally violate each
rule and require detection. Contract strings and private repo fixture paths
remain valid. This is an AST guard for these forms, not a complete Python
alias/interprocedural analyzer. `_load.load()` additionally intercepts actual
executor cleanup of the enclosing checkout. Behavioral tests exercise sibling
survival, foreign-PID cleanup, unregistered cleanup rejection, Git discovery
isolation, and the cleanup rejection path.

## Environment failures and validation limits

The four requested tests were run individually with default Python (3.14 here):

* `test_evalid_roles_wiring.py::EvalidRoleWiringTests::test_one_shot_and_resident_finalization_collect_before_cleanup_and_after_verify`: passed, 17 subtests. Uses private Git fixture, real verification subprocess, mocked executor/routing/cleanup. Historical host failure not reproduced.
* `test_fleet.py::FleetTests::test_text_status_uses_reachable_snapshot_without_lanes`: passed. Fleet DATA is private and host_run is mocked. Historical host failure not reproduced.
* `test_fleet_health_cli.py::LanesStatusHealthProbeTest::test_the_probe_never_touches_the_dispatcher`: passed. Uses private control-plane snapshot and compares mtimes. Historical host failure not reproduced.
* `test_pi_isolation.py::PiEnvironmentTests::test_missing_variables_use_existing_defaults`: failed at Unix `socket.bind`, EPERM, before launching wrapper. HOME, runtime, broker path and bwrap/socat commands are already fixture-controlled. This is a sandbox socket prohibition, not a missing Pi installation. The same restriction affects six other Pi integration cases. Assertions were retained.

`WorkspaceTest.test_current_worktree_git_metadata_is_writable` also fails:
real ref cleanup cannot create `packed-refs.lock` in the read-only common Git
directory. Its original assertion remains intact. No permission widening,
network installation, skipped assertions, new dependencies or production-code
changes were introduced.

Python 3.9 and xdist are unavailable. Those CI validations remain outstanding;
no claim of two successful complete suites is made. Full local suite attempts
are reported in the dispatch result, including interruptions/time limits.

## Recorded checks and self-review

* Culprit + victims: `python3 -m pytest -q swrm/tests/test_agy.py swrm/tests/test_dispatch_pause.py swrm/tests/test_fleet_health_pause.py`: 33 passed.
* Guard + executor health after fixing a subprocess fixture-import issue: `python3 -m pytest -q swrm/tests/test_test_isolation.py tools/tests/test_resource_isolation.py tools/tests/test_executor_task_health.py`: 47 passed, 26 subtests.
* Guard + validation-mode cases: 38 passed, 21 subtests.
* Workspace/bench/guard/validation selection: 81 passed, 22 subtests, one existing real-Git-permission failure described above.
* Initial migrated-tools selection: 183 passed, 83 subtests, one failure introduced by importing the scratch helper when the test file is loaded as fake SSH support. Fixed by deferring that import to the fixture allocation method; the failing CLI case then passed, and all executor-health cases passed in the 47-test selection.
* `timeout 180 python3 -m pytest -q swrm/tests`: exit 124; did not finish. An earlier profiling run was interrupted after identifying the cleanup call chain.
* `timeout 180 python3 -m unittest discover -s tools/tests`: exit 124; did not finish. An earlier discover attempt was interrupted. These are not reported as passes.
* Changed/new Python sources: `ast.parse(..., feature_version=(3, 9))` plus `compile`: passed. This checks syntax, not a Python 3.9 runtime.
* `git diff --check`: passed.

Acceptance mapping: (1) culprit/call chain and deterministic two-process
reproduction above, including both symptoms; (2) resource inventory above and
private tools fixtures/environment restoration changes; (3) shared allocator,
Git ceiling, runtime cleanup guard, static guard and intentional-violation
self-tests; (4) no removed/weakened assertions and Python 3.9 grammar check;
(5) four individually run cases above, with full-suite completion outstanding.
The requested historical entire-checkout run, complete suite acceptance, and
Python/xdist CI matrix remain follow-ups; the report does not equate the
controlled old-victim reproduction with a complete historical checkout run.

SELF-REVIEW: 范围 — PASS
SELF-REVIEW: 门禁 — PASS
SELF-REVIEW: 安全 — PASS
SELF-REVIEW: 测试 — 发现并已修：fake SSH 加载测试模块时无法导入 helper，改为 fixture 内延迟导入并验证通过
SELF-REVIEW: 验收 — 未修：完整套件超时，socket bind 被沙箱拒绝，真实 Git packed-refs.lock 只读；Python 3.9/xdist 待 CI 验证

Final migrated-tools rerun (all changed scratch consumers other than executor
health, which passed separately): 147 passed, 73 subtests in 55.30 seconds.
