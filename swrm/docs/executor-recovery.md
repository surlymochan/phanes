# Executor operation

The default models are selected for the accounts checked during this recovery. Model
catalogues and billing can change. Codex probes the signed-in account's model list;
Copilot `auto` reports the selected model in one-shot attempt metadata. Cursor's
one-shot default requests Composer 2.5 standard speed. Cursor ACP currently exposes
Composer 2.5 fast as its default, so its cost can differ from one-shot runs.

Pi uses a Linux bubblewrap launcher and a single-model MiniMax broker. The broker runs
on the host that already has the Pi credential; the worker never receives that
credential. The worker needs bubblewrap, socat, and a Pi 0.73-compatible installation.
The launcher disables extension, skill and context discovery, starts with an empty
environment, and lets the sandbox reach only the broker socket. The broker accepts
only the configured model and the MiniMax messages endpoint.
Each broker process accepts at most 64 inference requests; restart the bridge when
that limit is reached.

From a machine with the existing SSH identities, start the bounded bridge:

```sh
bin/pi-fleet-bridge --broker-host broker --worker worker --minutes 120
```

The command prints the worker socket path. On the worker, set
`OPENSWARM_PI_BROKER_SOCKET` to that path and `OPENSWARM_PI_RUNTIME` to the isolated
Pi installation prefix, then run the candidate's `bin/swrm-fleet`. This uses a
separate swrm state directory, preserving the existing fleet configuration. Keep the bridge
alive for the run. Interrupting it or reaching its time limit closes its SSH sessions
and removes its private sockets. It is a transport process, not an executor.

Pi read-only review and consult are deliberately rejected until their own sandbox
contract is verified. One-shot isolated Pi cannot resume a rate-limited session;
resident RPC Pi keeps its session inside the sandbox. A missing bridge, unsupported
host, unapproved model or unisolated binary fails closed. Do not turn off
`pi.require_isolation` on a host containing credentials or other valuable data.

The broker is the single point of failure of a Pi host: every Pi run of that host
goes through its socket. The executor therefore probes the socket before every Pi
dispatch, restarts a dead broker with a capped exponential backoff and verifies the
fresh broker with a minimal session before it releases the channel again. The minimal
session creates no run and spends no run budget; a broker that answers a connection
but fails that session keeps the host interrupted. The recovery state of every host
lives in `<OPENSWARM_HOME>/broker-recovery.json`, the reconnect events (host, attempt,
result) are appended to the brain log (`<OPENSWARM_HOME>/brain.log` by default), and
`swrm fleet status` reports a host whose Pi channel is interrupted as an alert — the
other backends of that host keep working. A Pi run that cannot use the broker ends
with the error class `broker_unavailable`, not with a vague `exec_error`.

Configure the recovery in `policy.local.json` under `pi.broker`:

| key | meaning |
| --- | --- |
| `attempts` | how often a dead socket is restarted before the host is declared interrupted |
| `backoff_base_s`, `backoff_cap_s` | the reconnect backoff: doubling, capped |
| `restart_cmd` | the local command that starts the broker again; the default `bin/pi-broker-restart` starts the broker of this deployment on the probed socket, `none` disables the restart |
| `restart_grace_s` | how long the restart command may stay in the foreground before it counts as the broker itself |
| `socket_wait_s` | how long a restarted broker may need before its socket answers |
| `session_timeout_s` | how long the budget-free minimal session may wait for the broker |
| `alert_failures` | consecutive failed reconnects before the fleet status warns about the host (the number of attempts by default) |
| `brain_log` | the file the reconnect events are written to (default `<OPENSWARM_HOME>/brain.log`) |

The default restart command is `bin/pi-broker-restart`: it starts
`bin/pi-minimax-broker.mjs` with the credential store and the model of the host
(`$OPENSWARM_PI_NODE`, `$OPENSWARM_PI_AUTH_MODULE` and `$OPENSWARM_PI_ISOLATED_MODEL`
override the defaults), removes a stale socket and stays in the foreground — it is the
broker. The executor starts it detached, hands it the socket it probes
(`$OPENSWARM_PI_BROKER_SOCKET`) and reads the reason of a failed restart from
`<OPENSWARM_HOME>/broker-restart.log`. A host whose broker is managed by a service
manager sets `restart_cmd` to `none`: the failure is then reported instead of a
second broker being started.

Only a Pi run that really uses the socket is gated by this: `exec_pi` probes when the
binary is the isolated launcher, and a resident Pi lane probes when it names a
`broker_socket` (or exports `PI_BROKER_SOCKET`), selects the isolated binary, or
uses the default launcher with `pi.require_isolation` on. Explicit plain binaries
do not trigger broker recovery. A plain (unisolated) Pi run
never touches the broker.

### Delivery mapping of the lane review of PR #1778 (round 2)

Every blocker of the review is mapped to the files that close it, to the tests that
prove it and to the result of the local run. The line numbers are the ones of the
delivered files.

| blocker | changed file and line | test | result |
| --- | --- | --- | --- |
| B1 — a dead broker has to be restarted automatically, with the default configuration | `swrm/policy.json:250` (`pi.broker.restart_cmd` = `bin/pi-broker-restart`), `swrm/bin/pi-broker-restart:124` (the script starts the broker of this deployment on the probed socket), `swrm/bin/swrm:3743` (`pi_broker_settings`), `swrm/bin/swrm:3779` (`pi_broker_restart_argv`), `swrm/bin/swrm:3819` (`pi_broker_restart`) | `DefaultBrokerRestartTests.test_the_default_restart_command_is_the_one_of_the_deployment`, `DefaultBrokerRestartTests.test_the_default_restart_command_brings_the_broker_back`, `DefaultBrokerRestartTests.test_the_restart_command_reports_why_it_failed`, `BrokerRestartScriptTests.test_the_script_execs_the_broker_on_the_given_socket`, `BrokerRestartScriptTests.test_the_script_removes_a_stale_socket`, `BrokerRestartScriptTests.test_the_script_refuses_a_path_that_is_not_a_socket`, `BrokerRestartScriptTests.test_the_script_keeps_a_socket_of_another_user`, `BrokerRestartScriptTests.test_the_script_keeps_a_socket_whose_state_is_unclear`, `ExecutorBrokerDispatchTests.test_a_dead_socket_is_restarted_and_the_dispatch_continues` | real-socket evidence requires an AF_UNIX-capable host; see local results below |
| B1 — the recovery runs before the dispatch, the alert reaches the health output | `swrm/bin/swrm:3876` (`pi_broker_ensure_ready`), `swrm/bin/swrm:5610` (`exec_pi`), `swrm/bin/swrm:9179` (`lane_run_turn`), `swrm/bin/swrm:3921` (`pi_broker_channel_status`), `swrm/bin/swrm:11162` (`fleet_pi_channel_alert`), `swrm/bin/swrm:11731` (`cmd_fleet`), `swrm/bin/broker_recovery.py:210` (`BrokerRecovery`) | `ExecutorBrokerDispatchTests.test_the_pi_dispatch_reports_the_broker_instead_of_a_vague_error`, `ExecutorBrokerDispatchTests.test_the_lane_dispatch_reports_the_broker_instead_of_a_vague_error`, `ExecutorBrokerDispatchTests.test_a_broker_that_stays_dead_alerts_the_host`, `ExecutorBrokerDispatchTests.test_the_health_output_reports_an_interrupted_pi_channel`, `FleetPiChannelTests.test_a_broken_pi_channel_is_an_alert_not_a_pause`, `BrokerRecoveryTests.test_a_broker_that_stays_dead_keeps_the_host_interrupted_and_alerts` | real-socket evidence requires an AF_UNIX-capable host; see local results below |
| B2 — dispatch level coverage: the lane really runs after the recovery, a failed validation keeps the dispatch blocked | `swrm/tests/test_broker_recovery.py:748` (`BrokerDispatchIntegrationTests`: `swrm lane up` + `swrm lane assign`) | `BrokerDispatchIntegrationTests.test_the_dispatch_continues_after_the_broker_was_restarted`, `BrokerDispatchIntegrationTests.test_a_failed_validation_keeps_the_dispatch_blocked`, `BrokerDispatchIntegrationTests.test_a_dead_broker_without_a_restart_keeps_the_dispatch_blocked`, `BrokerRecoveryTests.test_a_failed_validation_keeps_the_interrupted_marker`, `BrokerRecoveryTests.test_an_interrupted_channel_is_only_released_by_a_validated_session`, `SandboxRecoveryTests.test_lane_child_recovers_before_launch_and_passes_socket`, `SandboxRecoveryTests.test_lane_child_does_not_launch_when_recovery_fails`, `SandboxRecoveryTests.test_concurrent_dispatches_wait_for_validation`, `SandboxRecoveryTests.test_proxy_session_failure_is_unhealthy`, `SandboxRecoveryTests.test_proxy_restart_never_starts_a_local_broker`, `SandboxRecoveryTests.test_concurrent_dispatches_are_released_after_one_validation`, `SandboxRecoveryTests.test_marker_exists_before_restart_and_other_socket_failure_survives` | 7 injected SandboxRecoveryTests pass; real-socket integration requires an AF_UNIX-capable host |
| follow-up — the minimal session must not block the dispatch forever | `swrm/bin/swrm:3862` (`pi_broker_minimal_session`), `swrm/policy.json:253` (`session_timeout_s`) | `BrokerRecoveryTests.test_the_minimal_session_answers_on_a_live_broker`, `ExecutorBrokerDispatchTests.test_the_health_output_reports_an_interrupted_pi_channel` | real-socket evidence requires an AF_UNIX-capable host; see local results below |

The two blocked-start integration cases assert a nonzero `lane up` exit, the
`pi broker unavailable` diagnostic and no executor prompt. They do not assign a
run after startup has already failed. The existing executor dispatch tests retain
the exact `broker_unavailable` error-class assertions.

Sandbox evidence: `python3 -m unittest test_broker_recovery.SandboxRecoveryTests -v`
(from `swrm/tests`) passes all 7 tests. These exercise the real LaneChild startup,
proxy restart routing, file-lock recovery serialization and per-socket markers
with injected probes/process launching, without binding a real socket. Real SSH
proxy and MiniMax credential checks remain FOLLOW-UP on a configured host.

Local acceptance in the restricted sandbox (no tests removed or newly skipped):

- `cd swrm/tests && python3 -m unittest test_broker_recovery`: 47 tests,
  8 failures and 11 errors from unavailable AF_UNIX binding (including control
  socket startup failures); the other 28 tests pass when run separately.
- `python3 -m unittest discover -s swrm/tests -p 'test_*broker*.py'`:
  75 tests, 34 failures and 16 errors; real-socket tests require a capable host.
- `python3 -m unittest discover -s swrm/tests -p 'test_lane*.py'`:
  158 tests, 70 failures, 1 error, 2 existing skips in this sandbox.
- `python3 -m unittest discover -s swrm/tests`: 1029 tests in 510.146s,
  118 failures, 32 errors, 13 existing skips; this is not a passing full-suite
  acceptance run (the permitted fleet-auto baseline alone does not explain it).
- `git diff --check`: pass.


### CI classification regression verification

The dispatch predicate now recognizes explicit plain RPC binaries before falling
back to isolation policy; explicit socket configuration still enables recovery.
Mock RPC recovery fixtures explicitly use a non-isolated policy, and the stream
classification fixture names its plain binary. Existing assertions are unchanged.

Restricted Linux sandbox (Python 3.14), compared with the supplied HEAD:

- Focused lane recovery, stream contracts and SandboxRecoveryTests: 54 tests,
  20.988s, OK (including the new socket-selection boundary test).
- Requested four modules: 108 tests, 74.253s, 14 failures and 15 errors.
  Baseline: 107 tests, 212.373s, 22 failures and 15 errors. No new failing test
  records; eight baseline failure records disappear. AF_UNIX binding is denied
  in both runs, so this is not broker integration acceptance.
- Full discovery: 1031 tests, 455.692s, 110 failures, 35 errors, 13 skips.
  This run began before the additional boundary test was added; that test passed
  in the subsequent four-module run. Archived HEAD baseline: 989 tests, 533.502s,
  125 failures, 88 errors, 13 skips. The archive lacks repository metadata and
  collected fewer tests, so these totals are not an equivalent full-suite baseline
  and cannot establish that every remaining failure is environmental.
- All 15 file:line references above were checked against their target lines;
  `git diff --check` passes.

FOLLOW-UP: run the requested commands on gocc with AF_UNIX support and the CI
Python versions; confirm all original 49 broker tests and the new boundary test
pass. Recheck the candidate-only full-suite failure
`test_bus.BusTests.test_reference_worker_claims_runs_pushes_and_reports` against
an equivalent git-backed baseline. No remote host access was available in this execution environment.


Copilot on macOS uses the normal GitHub device login. If the system keychain is
unavailable, its CLI may store the credential in plaintext under `COPILOT_HOME`;
restrict the directory and files to the owning account and treat that location as a
credential store. The Copilot sandbox restricts shell commands; built-in file edits
use the CLI's best-effort policy rather than an OS sandbox.

## Resident lane restart recovery

`lane serve` persists `runs/<run_id>/assignment.json` before executing, including
its task, base, workspace and persistent Codex thread ID or Pi session file.
On restart it resumes the same run; it never substitutes a fresh session when
that identifier is missing or the backend rejects restoration.

Issue assignments (AD's `#<number> ...` title) require both AD's exact lane/run
ownership and an open GitHub issue carrying `in-progress`. Missing authority,
terminal labels and a second lane owning the issue all abandon recovery with a
reason. Manual assignments without an issue number have no AD ownership check.

Configure each lane's optional `recovery_ad` object in `lanes.json`:

```json
{"recovery_ad": {"host": "control-host", "state_path": "/absolute/path/to/autodispatch/state.json", "repo": "owner/repo", "brain_log": "/local/path/to/brain/log.md"}}
```

`host` uses read-only SSH calls (`cat` and `gh api`) on the AD host. Omit it for
local AD. Environment fallbacks are `OPENSWARM_AD_HOST`, `OPENSWARM_AD_STATE`,
`OPENSWARM_AD_REPO` and `OPENSWARM_BRAIN_LOG`; the local defaults are
`~/phanes-ctl/autodispatch/state.json`, `surlymochan/phanes` and
`<lane repo>/brain/log.md`. The brain log is written on the executor host; use a
local shared log path when collecting logs centrally. Unavailable authority
never falls back to local run status.

Recovery fetches the exact `origin/swrm/<run_id>` branch. A tip strictly beyond
the saved base is a commit recovery point. A missing workspace can be rebuilt
from that pushed tip; an existing workspace advances with `merge --ff-only`,
which refuses divergent or conflicting edits without resetting or cleaning.
Without verified pushed progress, the existing workspace is preserved and
marked `workspace` (恢复自未提交状态). Missing workspaces without a pushed
point are abandoned. Events and brain log lines contain the run ID, level and
commit/path recovery point; recovery failure leaves the lane available for new
assignments.

### Review finding map

| Finding | Change in `swrm/bin/swrm` | Test evidence |
|---|---|---|
| B1: a pushed commit is the recovery point | `lane_recover_assignment` fetches the exact `origin/swrm/<run_id>`; `lane_prepare_workspace` rebuilds a missing workspace from that tip or advances an existing one with `merge --ff-only` (never reset or clean) | `test_lane_recovery`: `test_pushed_commit_recovers_at_remote_tip`, `test_remote_branch_at_base_is_not_commit_recovery`, `test_stale_tracking_ref_without_remote_confirmation_is_workspace_only`; `test_lane`: `test_restart_restores_remote_tip_ahead_of_local_head`, `test_restart_restores_missing_workspace_from_pushed_tip` |
| B2: authoritative AD verification | `lane_ad_recovery_view` reads AD's exact lane/run ownership and the open issue's `in-progress` label (read-only, local or over SSH); unavailable authority never falls back to local run status | `test_lane_recovery`: `test_ad_requires_exact_issue_owner_and_explicit_live_status`, `test_remote_ad_reads_run_ownership_and_issue_status`, `test_unavailable_ad_then_new_run_keeps_abandoned_files`, `test_mismatched_run_status_is_abandoned`; `test_lane`: `test_restart_abandons_unavailable_ad_authority` |

Focused acceptance (the lane module uses fake executors and an isolated capacity
configuration, including a disconnected Unix-socket broker):

```sh
python3 -m unittest discover -s swrm/tests -p 'test_lane_recovery.py' -v
python3 -m unittest discover -s swrm/tests -p 'test_lane.py' -v
git diff --check
```

## Broker socket recovery: threat model

`pi-broker-restart` and `pi-worker-proxy` remove a stale broker socket only
after a connect probe shows nobody is listening. The socket directory is
opened with `O_DIRECTORY|O_NOFOLLOW`, must be owned by the current user and
mode 0700, and the entry is renamed to a unique private name, re-validated
(inode/device, still a socket) and then unlinked relative to the directory fd.

Accepted residual risk (Boss decision for PR #2134, no further code change):

1. **Out of scope: processes of the same UID.** They can already delete,
   replace or connect to these sockets directly, so no check here can stop them.
   The directory is private (0700, current user), so other users cannot reach it.
2. **Where the window is.** Two short races remain, both inside that private
   directory: between validating the isolated name and the `unlink`, and in the
   recovery branch between "name does not exist" and the `rename`. A same-UID
   actor that wins one of them can only make recovery remove or skip its own
   user's entry; the next recovery attempt re-probes and converges.
3. **Why it cannot be closed completely.** POSIX has no unlink-by-inode
   primitive; every removal is path (name) based relative to a directory fd, so
   a check-then-act gap between validation and removal is inherent. The
   rename-to-unique-name step shrinks it to the isolated name; the dirfd hardening
   is kept as is and tracked in no separate issue.

## Host verification

The sandbox of the executors cannot bind AF_UNIX sockets, so the socket-level tests must run on a host that can:

```
cd swrm/tests && python3 -m unittest test_broker_recovery
```

The default restart script only removes a socket that belongs to the current user, is a socket, and refuses connections; any other connect failure leaves the path in place and the restart fails.

### Capacity lease audit

Run `swrm/bin/fleet-capacity-audit --health` periodically on the coordinator.
The independent command opens `capacity.sqlite` with SQLite `mode=ro` and never
renews, releases, or recovers reservations. `--db` and `--runs` select the
coordinator database and run directory; `--grace-seconds` defaults to 60.
`--state` selects the audit's deduplication ledger, which defaults alongside
the database. Protect this file and retain it between rounds: current findings
are marked `new` once and `ongoing` on consecutive rounds. The JSON report
includes current counts and counts first seen within 24 hours, including
resolved findings. Missing run records are counted; expiry and duplicate run
checks still work without them. The coordinator has no acquire queue timing,
so `lease_age_seconds` is null with an explanation rather than an estimate.
Run records are merged by field: an explicitly terminal `result.json` wins;
otherwise `status.json` takes priority, followed by `result.json` and `run.json`.
The first available status (`status`, then `state`) and card (`issue`, then
`title`, excluding blank values) in that order are used. Thus a result without
a title can borrow the status record's title. `partial` is terminal and any
reservation it still holds is reported as a ghost.

The lifecycle writers in `swrm/bin/swrm` use these records:

| Record | Stage and card fields | Status fields | Time fields |
| --- | --- | --- | --- |
| `status.json` | Queue, routing, execution, finalization; `title` is set on admission/detach and lane assignment | `state` during execution; final `status` plus `state=finished` | `started`, `queued_at`, final `finished`; `set_status` always updates `updated` |
| `result.json` | Finalization or early failure; normally `title`, but some failure paths omit it | Final `status`, including `partial` | Durations `seconds` / sometimes `queued_seconds`; no consistent absolute state timestamp |
| `run.json` | Compatibility input; no writer in the current `swrm` binary; optional `issue` / `title` | Optional `status` / `state` | No current writer contract |
| Other run artifacts | `task.md`, event logs, `review.json`, reservation/release JSON, lock files and executor outputs | Not authoritative run lifecycle records; never used to infer card/state | Not used by the audit |

`missing_run_records` counts reservations with incomplete run evidence once per
round. `missing_run_record_reasons` reports per-reason reservation counts:
`directory_missing`, `files_missing` (none of the three lifecycle files exists),
`invalid_json`, `invalid_record` (JSON is not an object), `unreadable_file`,
`missing_card`, `missing_status`, or `invalid_run_id`. Multiple reasons can apply
to one reservation. Missing optional files do not count when another lifecycle
file exists. Corrupt files count even if a readable fallback supplies the
fields. Missing fields are checked after merging readable objects. These
counters are included in `--health` and ordinary JSON output; they describe
coverage of this round, not the 24-hour alert window. Missing card information
skips card grouping; missing status skips terminal detection. Independently
available card/status evidence, duplicate run checks and expiry checks remain
usable. Only the audit ledger is written; all run artifacts remain untouched.
