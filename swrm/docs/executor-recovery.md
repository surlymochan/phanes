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
