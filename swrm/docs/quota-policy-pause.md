# Per-backend quota policy: the pattern, the cooldown and the scoped pause

Refs #1368 (PR #1654, `upstream(openswarm): 每后端 policy quota_error_re + 冷却时间（Copilot 402 / agy 429 → 暂停）`).

## What the policy does

`policy["backends"][<backend>]` is the quota policy of one backend:

| field | meaning |
| --- | --- |
| `quota_error_re` | the pattern that marks a quota error of the backend (matched against the error text of the run that ended with the error) |
| `quota_cooldown_s` | how long the scope stays paused |
| `scope` | what is paused: `lane` pauses the lane, `backend` pauses the backend |

The policy is read from `policy.json` and `policy.local.json` (merged by
`load_policy()`), so a local policy changes the pattern, the cooldown or the
scope of one backend without touching the other backends.

A quota error pauses its scope through the scoped pause store
(`$OPENSWARM_HOME/pauses`): the records the autodispatch
(`tools/lane-autodispatch/autodispatch.py`) reads, so a paused lane or a paused
backend is not dispatched any more. The record carries the expiry, so the pause
ends by itself when the cooldown is over; the expired records are removed from
disk by `reap_quota_pauses()`. A manual pause of the same scope is never
replaced by an automatic one, and the quota path never removes one.

Defaults (`swrm/policy.json`): Copilot `402` pauses the lane for 3600 s, agy
`429`/`RESOURCE_EXHAUSTED` pauses the backend for 1800 s.

## Rebase note

The PR head changed `openswarm/lib/dispatch/engine.py` and
`openswarm/tests/test_dispatch_engine.py`. Both files are gone on main: the
vendored openswarm tree was replaced by `swrm` as the single source
(#1894, `Replace vendored openswarm with swrm as the single source`). The
conflict is therefore a modify/delete: main keeps the deletion, and the intent
of the PR (per-backend quota policy, cooldown, scoped pause) is ported to the
surviving implementation `swrm/bin/swrm`, which is the source of truth of the
upstream project now. `openswarm/tests/test_autodispatch.py` (the documented
acceptance of the PR) is restored: it covers the autodispatch worker, which
stayed in the phanes tree.

## BLOCKER mapping (PR #1654)

| BLOCKER | change (file:line) | test | result |
| --- | --- | --- | --- |
| B1 – the scoped quota pause of the lane and of the backend, the expiry clean up and the probe recovery must be in the diff | `swrm/bin/swrm:4094-4236` (`pause_store_dir`, `write_scope_pause`, `reap_quota_pauses`, `apply_backend_quota_policy`), `swrm/bin/swrm:8630` (the hook in `lane_finalize_run`), `swrm/bin/swrm:8921` (the expiry clean up of the lane supervisor), `swrm/bin/swrm:8995` (the probe recovery) | `swrm/tests/test_quota_policy.py::test_copilot_402_pauses_the_lane_scope`, `::test_agy_429_pauses_the_backend_scope`, `::test_plain_429_uses_the_policy_cooldown`, `::test_quota_pause_expires_and_is_reaped`, `::test_probe_recovery_resumes_the_automatic_pause`, `::test_quota_error_pauses_the_backend_scope` (end to end: a lane run that ends with a quota error pauses the scope) | pass |
| B2 – the automatic resume must not remove a manual pause of the same scope | `swrm/bin/swrm:4160` (`resume_quota_scope` removes automatic pauses only), `swrm/bin/swrm:4228` (an automatic pause never replaces a manual one) | `swrm/tests/test_quota_policy.py::test_quota_pause_does_not_replace_a_manual_pause`, `::test_probe_recovery_keeps_a_manual_pause` | pass |

The policy fields really control the backend they belong to (the earlier
round): `swrm/tests/test_quota_policy.py::test_local_policy_overrides_the_cooldown_of_one_backend_only`
and `::test_policy_quota_fields_are_read_per_backend`. The autodispatch honours
the scoped pause: `openswarm/tests/test_autodispatch.py::ScopedQuotaPauseTests`.

## Verification

```
cd swrm && python3 -m unittest discover -s tests -p 'test_quota_policy.py'   # 14 tests
cd openswarm && python3 -m unittest discover -s tests -p 'test_autodispatch.py'  # 17 tests
cd swrm && python3 -m unittest discover -s tests                              # 495 tests, 3 pre-existing failures
```

The three failures of the full swrm suite (`test_lane.LaneTests.test_cursor_systemd_unit_is_printed_not_installed`,
`test_pi_isolation.PiIsolationTests.test_host_secrets_and_network_are_not_visible`,
`test_pi_isolation.PiIsolationTests.test_linked_git_worktree_and_node_remain_available`)
are the ones the unmodified base of the branch already had.
