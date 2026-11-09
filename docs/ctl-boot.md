# Box control-plane boot (no systemd)

This box has no systemd user units and no cron for phanes ctl. After reboot, start the loops with:

```bash
~/phanes-ctl/bin/start-control-plane
```

That script starts (if not already running):

| Loop | Script | Role | Heartbeat / status |
|------|--------|------|--------------------|
| ib-refresh-loop (`ib-refresh`) | `ctl-loop` → `ib-refresh` | renew instabox SSH certs before expiry | `~/phanes-ctl/ctl-loop.heartbeat`, `ib-refresh.status` |
| lane-autodispatch | `lane-autodispatch` | SSH fleet dispatch + backlog refill | `~/phanes-ctl/autodispatch/heartbeat` |
| lane-merge | `lane-merge` → `merge-loop.sh` | merge Linux-green `fleet/*` PRs | `~/phanes-ctl/autodispatch/merge-heartbeat` |
| bus-worker-loop (`bus-worker`) | `bus-worker-loop` | GitHub issue bus mode 2 | `~/phanes-ctl/bus-worker/heartbeat`, `bus-worker/` |
| health-loop | `health-loop` → `health-check.py` | disk/mem/ssh/quota probes; auto PAUSE-* | `~/phanes-ctl/autodispatch/health.heartbeat`, `health.json`, `health.alert` |
| disk-watermark | `disk-watermark-guard.py --loop` | remote lane host disk watermark guard (Refs #1666) | `~/phanes-ctl/autodispatch/disk-watermark-<host>.json`, `/workspace/phanes/brain/log.md` |

## Key loops: startup commands & heartbeats

The `start-control-plane` suite manages the five core resident loops (五件套: `ib-refresh-loop`, `lane-autodispatch`, `lane-merge`, `bus-worker-loop`, `health-loop`) plus the `disk-watermark` guard loop (Refs #1666), with their startup commands and heartbeat/status files:

1. **`ib-refresh-loop`** / **`ib-refresh`** (instabox SSH certificate preflight & renewal loop):
   - **Startup command**: `~/phanes-ctl/bin/ctl-loop` (supervises `ib-refresh`) or manual preflight `~/phanes-ctl/bin/ib-refresh`  
     (or background one-shot: `setsid nohup ~/phanes-ctl/bin/ctl-loop >/dev/null 2>&1 &`; repo CLI: `python3 tools/ib-refresh.py --fleet-config ~/phanes-ctl/fleet.json --renew-command ~/phanes-ctl/bin/renew-cert --pause-dir ~/phanes-ctl/autodispatch --status-file ~/phanes-ctl/ib-refresh.status`)
   - **Heartbeat path**: `~/phanes-ctl/ctl-loop.heartbeat`
   - **Status path**: `~/phanes-ctl/ib-refresh.status` (stable row: `ok=... hosts_ok=... skipped_cooldown=... min_ttl=... min_exp=...`; field order is fixed)

2. **`lane-autodispatch`** (fleet SSH dispatch + backlog refill):
   - **Startup command**: `~/phanes-ctl/bin/lane-autodispatch`  
     (or background one-shot: `setsid nohup ~/phanes-ctl/bin/lane-autodispatch >/dev/null 2>&1 &`)
   - **Heartbeat path**: `~/phanes-ctl/autodispatch/heartbeat`
   - **Status snapshot**: `~/phanes-ctl/autodispatch/lanes-status.json` (inspect with `~/phanes-ctl/bin/lane-autodispatch-status`)
     It contains `_busy` (lanes currently assigned work), `_ready` (open GH
     `lane-ready` issues not marked `in-progress`, `lane-pr`, or
     `lane-failed`), and `_blocked` (ready issues waiting on `依赖`); `_ready`
     is the dispatchable count after `_blocked` is removed. `_ready` follows the
     authoritative issue labels (the bool `fresh_ready` check), not the lagging search index:
     a stale search row (e.g. an issue that really carries `lane-failed`) is
     skipped and logged as `skip #<n>: stale search index (already claimed/done)`,
     never counted as dispatchable. A failed authoritative read is NOT stale:
     the row is kept and logged as `skip #<n>: label read failed (...)`, so a
     transient `gh` error can neither drive `_ready` to a false 0 nor trigger a
     needless refill. Each issue is read authoritatively at most once per tick
     (`AD_FRESH_BUDGET`, default 60 reads/tick, keeps the GraphQL quota safe).
     `_stale_stripped_n` (also exposed as `_stale_search_skips`) is the
     non-negative count of stale-search-index entries stripped by the current
     autodispatch process; `_stale_lane_failed_skips` counts the subset of those
     stale hits whose issue still carries both `lane-failed` and `lane-ready`
     (Refs #985).
   - Failed work is not retried by leaving a stale `lane-ready` label: after a
     failure, an operator must manually re-add `lane-ready` and remove
     `lane-failed` (and `lane-pr`, if present) before it can be dispatched again.
   - **Task upload contract (Refs #920)**: dispatch uploads the generated task
     over the existing SSH connection with stdin (`ssh ... 'cat > /tmp/task-*.md'`).
     A standalone `scp` upload is forbidden because it is unreliable through
     the instabox ProxyJump path. Check with `python3 tools/check-ad-task-upload.py`.
   - **SSH option quoting contract (Refs #972)**: status probes and harvest both
     build their SSH command from the same `git_ssh_command`/`shlex` path, so a
     `ProxyCommand` or `ProxyJump` value containing spaces remains one argv item.
     Check offline with `python3 tools/check-ssh-opts-quote.py`.
   - **Jump host ProxyCommand contract (Refs #907)**: a host listed in the AD jump
     table gets its `ProxyCommand` injected by `ssh_opts(host)`; the value carries
     spaces and is `shlex.quote`d before it reaches `core.sshCommand`, so `ssh`
     never sees a split option. The jump table is runtime configuration
     (`$AD_JUMP_HOSTS` or `~/phanes-ctl/autodispatch/jump-hosts.json`), not code —
     the box ssh config owns the topology (see
     [`docs/dual-mode-ssh.md`](dual-mode-ssh.md)).
     Check offline with `python3 tools/check-ad-ssh-command.py`.
   - **Chunked sleep heartbeat contract (Refs #906)**: between ticks, autodispatch sleeps
     in chunks of ≤ 5s (`sleep(5)`) and refreshes `autodispatch/heartbeat` at each step.
     This prevents host suspend / nanosleep freeze from creating ~40-minute silent heartbeat
     gaps and false-positive supervisor stalls.
     Check offline with `python3 tools/check-ad-heartbeat-chunk.py`.
   - **Switch source audit (Refs #1827)**: every tick compares the `PAUSE*` / `EXCLUDE*`
     files of `~/phanes-ctl/autodispatch` (name, mtime, content) with the previous tick and
     appends one line per change to `~/phanes-ctl/autodispatch/switch-audit.jsonl` (time,
     file name, action `created`/`deleted`/`changed`, before/after content summary, owner,
     file mtime). A change without an authorization entry (no `用户.*令` / `user order` /
     `auto:` in the last 30 lines of `brain/log.md`, and no `auto:` body in the switch
     itself) is marked `unattributed` and raises a `level=hard source=switch-audit` line in
     `~/phanes-ctl/autodispatch/health.alert`. The audit only records: it never removes nor
     restores a switch, and a broken audit file or an unreadable switch directory never
     stops the dispatch loop. An unreadable directory or switch makes the snapshot
     incomplete: the tick is skipped (no comparison, the baseline is kept), so a read error
     never fakes a `deleted` or `created` row (Refs #1827). Every switch is stat'ed once
     per tick and the file type comes from that stat: a second stat would swallow its own
     error and drop a living switch from the set (Refs #1827).
     Check offline with `python3 tools/check-ad-switch-audit.py`.

3. **`lane-merge`** (merge Linux-green `fleet/*` PRs):
   - **Startup command**: `~/phanes-ctl/bin/lane-merge`  
     (or background one-shot: `setsid nohup ~/phanes-ctl/bin/lane-merge >/dev/null 2>&1 &`; script: `merge-loop.sh`)
   - **Heartbeat path**: `~/phanes-ctl/autodispatch/merge-heartbeat`
   - **Idle heartbeat contract (Refs #955)**: when idle (no PR pending or no open PR in queue), `merge-loop.sh` skips the merge step (no `gh pr merge` call) but still updates the `merge-heartbeat` timestamp on each tick to prevent false-positive stall detection. Check with `python3 tools/check-merge-idle-heartbeat.py`.

4. **`bus-worker-loop`** (GitHub issue bus executor loop):
   - **Startup command**: `~/phanes-ctl/bin/bus-worker-loop`  
     (or background one-shot: `setsid nohup ~/phanes-ctl/bin/bus-worker-loop >/dev/null 2>&1 &`; CLI: `swrm bus-worker --bus <bus-repo> --name <worker-name> --backend <backend>`)
   - **Heartbeat path**: `~/phanes-ctl/bus-worker/heartbeat`
   - **Status / work directory**: `~/phanes-ctl/bus-worker/`

5. **`health-loop`** (disk/mem/ssh/quota probes; auto PAUSE-*):
   - **Startup command**: `~/phanes-ctl/bin/health-loop`  
     (or background one-shot: `setsid nohup ~/phanes-ctl/bin/health-loop >/dev/null 2>&1 &`; probe: `python3 ~/phanes-ctl/bin/health-check.py`)
   - **Heartbeat path**: `~/phanes-ctl/autodispatch/health.heartbeat`
   - **Status / alert snapshot**: `~/phanes-ctl/autodispatch/health.json`, `health.alert`

6. **`disk-watermark`** (remote lane host disk watermark guard, Refs #1666):
   - **Startup command**: `~/phanes-ctl/bin/disk-watermark-guard.py --loop --pause-dir ~/phanes-ctl/autodispatch --brain-log /workspace/phanes/brain/log.md --host <host>`
     (installed by `bash tools/install-disk-watermark-guard.sh`)
   - **Heartbeat path**: none; the guard writes its state into `~/phanes-ctl/autodispatch/disk-watermark-<host>.json`
   - **Status / alert snapshot**: `~/phanes-ctl/autodispatch/disk-watermark-<host>.json`, `/workspace/phanes/brain/log.md`

### Reboot pid-state helper scope (Refs #887)

The functions `start_bus_worker` in `swrm/bin/hub-swrm-bridge` and
`start_autodispatch` in `swrm/bin/swrm-auto` and
`tools/lane-autodispatch/autodispatch.py` persist JSON `{pid, started_at}` state
under `OPENSWARM_CTL_STATE_DIR` (default `~/phanes-ctl/state`). They clear
malformed, non-positive, exited, or PID-reused records; a matching live process
is reused. Process identity is checked against `/proc` start time.

These are library helpers with no caller in `start-control-plane` today. The
example `tools/lane-autodispatch/start-control-plane.example` does not read these
variables, so setting them while invoking `~/phanes-ctl/bin/start-control-plane`
does not enable this behavior. Unit tests invoke the helpers directly with fake
worker commands.

For production supervision, the authoritative contract remains `ctl-loop`'s
plain-text positive PID files at `~/phanes-ctl/autodispatch/autodispatch.pid`,
`autodispatch/health.pid`, and `ib-refresh.pid` (see below and
`tools/check-ctl-loop-reap.py`). It terminates and reaps a live old child before
restarting it. The JSON helper state is isolated fixture/library state; do not
run it as a second supervisor for a ctl-loop child or treat it as authoritative
production state. The offline ctl-loop contract checker is unchanged.

`ib-refresh` must run its renewal preflight before starting `lane-autodispatch`.
The repository implementation is `tools/ib-refresh.py`; it reads a temporary
fleet JSON (`--fleet-config`), classifies each certificate as `valid`,
`expiring`, `expired`, or `missing`, and prints one JSON result.  Only a result
with `start_lane_autodispatch: true` may start the dispatch loop.  Renewals are
staged in a temporary directory and atomically installed only after every
renewal returns a valid X.509 certificate; a partial failure leaves the old
certificates and fleet config untouched. The status file contains a stable,
machine-readable row with those five fields; `hosts_ok` counts only valid
certificates in the written fleet config, and cooldown skips contribute to
`skipped_cooldown`. When the SSH BatchMode probe is enabled (`ssh_probe` in the fleet
config, or `--ssh-probe`), a host whose probe exits non-zero is marked
`probe_status: failed` and stays outside `hosts_ok` (Refs #954).
When `need_ok` is configured, the gate stays healthy when
`hosts_ok >= need_ok`; cooldown hosts are omitted from an alternate fleet
config, so three reachable hosts can keep `ok=1` while two hosts cool down
(for example, `ok=1 hosts_ok=3 skipped_cooldown=2`).
Hosts absent from the configured topology are errors and are excluded from
`hosts_ok` (Refs #1164); exercise this invariant with
`python3 tools/check-fleet-unknown-host.py`.
For the node-1.example.invalid/node-2.example.invalid cooldown case, exercise this accounting with the fake-input check
`python3 tools/check-ib-cooldown-status.py` (Refs #1262).
An active `PAUSE-HOST-<host>` body with `auto:cooldown:until=<ISO-Z>` is read
without modification; `ib-refresh` skips resume/wake/renew for that host and
counts it in `skipped_cooldown`. Exercise this boundary with
`python3 tools/check-ib-cooldown-skip.py` (Refs #904).
Check the parser contract offline with
`python3 tools/check-ib-status-line.py`.

### SSH ControlMaster stale-socket sweep (Refs #921)

At the beginning of every `lane-autodispatch` tick, sweep
`~/.ssh/cm-ad` and `unlink` Unix-socket entries whose mtime is more than `600`
seconds old.  A socket exactly `600` seconds old remains for that tick, and
regular files are never removed.  This is the cleanup boundary for the
`ControlMaster` mux used by fleet SSH; it is intentionally before dispatch so
the next connection can recreate a usable mux.

The sweep matters after `ib-refresh` renews a fleet SSH certificate: an old
mux can stay wedged with the pre-renewal session and make fresh certificate
authentication appear broken.  Removing only stale sockets lets the next SSH
operation establish a new control connection without touching certificates or
keys.  The offline contract check creates fake sockets with old and fresh mtimes:

```bash
python3 tools/check-cm-ad-sweep.py
```

### SSH fleet BatchMode probe & hosts_ok rollup (Refs #954)

`ib-refresh` can probe every fleet host with `ssh -o BatchMode=yes` before it rolls the
fleet up into the control-plane status row. Any non-zero probe exit code (including
`Permission denied`, `Connection closed`, or timeout) marks the host as failed: the row
carries `probe_status: failed` and the host is excluded from `hosts_ok`, so a broken host
never adds capacity. Only a zero exit code may add one `hosts_ok`.

Enable the probe in the fleet config (`~/phanes-ctl/fleet.json`) or on the command line:

```json
{
  "need_ok": 3,
  "ssh_probe": {"enabled": true, "ssh_config": "~/.ssh/swrm-hub/config"},
  "hosts": [{"name": "node-1.example.invalid", "cert": "certs/node-1.example.invalid.crt"}]
}
```

```bash
python3 tools/ib-refresh.py --fleet-config ~/phanes-ctl/fleet.json \
  --renew-command ~/phanes-ctl/bin/renew-cert --ssh-probe \
  --ssh-config ~/.ssh/swrm-hub/config
```

The offline contract check drives the same production refresh path with a fake probe
runner, so it never opens an SSH connection and never touches live instabox nodes:

```bash
python3 tools/check-fleet-batchmode-probe.py
```

Certificate reuse is controlled by `IB_CERT_REUSE_TTL` (default `1500` seconds):
`need_renew` is true only below that remaining TTL.  Set a higher value to force
earlier renewal; `--threshold-seconds` is the one-run override.  The offline
contract check `python3 tools/check-ib-cert-reuse.py` uses fake TTLs and the
environment variable to cover both branches, and never invokes a real renewal
command or touches a machine certificate.

### Merge loop idle heartbeat (Refs #955)

When no open PR is waiting to merge (`idle`), the `lane-merge` loop (`merge-loop.sh`)
must skip the merge step but still touch/refresh
`~/phanes-ctl/autodispatch/merge-heartbeat`. Updating the heartbeat while idle prevents
watchdog monitors from misinterpreting an idle period (no PR activity) as a wedged
or stalled merge loop.

`bin/merge-loop.sh` writes the heartbeat before the PR query and again on an idle
tick (empty `gh pr list` result), so an empty queue never looks like a dead loop.
The offline contract check replays that real script with a fake `gh` stub inside a
fake directory: while the queue is empty no `gh pr merge` call may happen (the
merge step is skipped), and `merge-heartbeat` must still be refreshed. The check
does not model merge policy: `merge-loop.sh` delegates the merge decision to the
host's `gh` configuration and branch protections.

The contract is asserted offline with fake directory fixtures:

```bash
python3 tools/check-merge-idle-heartbeat.py
```

## Smoke (safe, no restart) (Refs #777)

```bash
~/phanes-ctl/bin/start-control-plane          # idempotent; prints already/start
~/phanes-ctl/bin/lane-autodispatch-status     # busy lanes + queue
test -f ~/phanes-ctl/bin/ctl-loop \
  -a -f ~/phanes-ctl/bin/lane-autodispatch \
  -a -f ~/phanes-ctl/bin/lane-merge \
  -a -f ~/phanes-ctl/bin/bus-worker-loop \
  -a -f ~/phanes-ctl/bin/health-loop \
  -a -f ~/phanes-ctl/bin/ib-refresh \
  -a -f ~/phanes-ctl/bin/start-control-plane
python3 ~/phanes-ctl/bin/health-check.py >/dev/null
```

### Path existence check (no SSH)

Single executable check verifying control-plane binaries and heartbeat/status paths without connecting to SSH:

```bash
test -f ~/phanes-ctl/bin/lane-autodispatch \
  -a -f ~/phanes-ctl/bin/bus-worker-loop \
  -a -f ~/phanes-ctl/bin/lane-merge \
  -a -f ~/phanes-ctl/bin/health-loop \
  -a \( -f ~/phanes-ctl/bin/ib-refresh -o -f ~/phanes-ctl/bin/ctl-loop \) \
  -a -f ~/phanes-ctl/bin/start-control-plane \
  -a -f ~/phanes-ctl/autodispatch/heartbeat \
  -a -f ~/phanes-ctl/autodispatch/merge-heartbeat \
  -a -f ~/phanes-ctl/autodispatch/health.heartbeat \
  -a \( -f ~/phanes-ctl/bus-worker/heartbeat -o -d ~/phanes-ctl/bus-worker \) \
  -a \( -f ~/phanes-ctl/ctl-loop.heartbeat -o -f ~/phanes-ctl/ib-refresh.status \)
```

Check scripts (no SSH):
- `python3 tools/check-lanes-status-summary.py` — offline #980 contract check
  using a fake `lanes-status.json` summary and fake GH ready list. For a soak
  read, compare the live snapshot with the filtered GH list (dependency
  blockers are shown separately as `_blocked`):
  `watch -n 30 "jq -c '{busy:._busy,ready:._ready,blocked:._blocked,at:._at}' ~/phanes-ctl/autodispatch/lanes-status.json; gh issue list --state open --label lane-ready --limit 100 --json number,labels --jq '[.[] | select(([.labels[].name] | index(\"in-progress\") | not) and ([.labels[].name] | index(\"lane-pr\") | not) and ([.labels[].name] | index(\"lane-failed\") | not))] | length'"`.
  That `gh issue list` is a search-index view while `_ready` follows the
  authoritative labels: in a stale scenario (an issue that really carries
  `lane-failed`) the two numbers differ on purpose.
- `python3 tools/check-ad-ready-count.py` — offline #950 contract check. It
  drives the shipped dispatcher with fake search results plus authoritative
  issue labels: a stale `lane-failed` search row is skipped and `_ready`
  matches the fresh count, a failed authoritative read keeps the row instead of
  shrinking `_ready`, and every issue is read at most once per tick;
  it never calls `gh`.
- `python3 tools/check-soak-summary.py` — offline dual-mode soak summary
  contract. It reads fake `lanes-status` and `ib-refresh.status` rows and
  prints the `busy`, `ready`, `hosts_ok`, and `min_exp` values for polling.
- `bash docs/check-ctl-boot.sh` — offline path and heartbeat existence verification under `~/phanes-ctl` (or `bash docs/check-ctl-boot.sh --self-test` to test in an offline fixture)
- `bash tools/check-ctl-boot.sh` — offline control-plane smoke check asserting start-control-plane 五件套 (`ib-refresh-loop`, `lane-autodispatch`, `lane-merge`, `bus-worker-loop`, `health-loop`), four heartbeat paths (`autodispatch/heartbeat`, `merge-heartbeat`, `ctl-loop.heartbeat`, `health.heartbeat`), `PAUSE-BACKEND-*` naming conventions, and replaying backend gate offline (`bash tools/check-ctl-boot.sh --gate-only`).
- `python3 tools/check-ad-heartbeat-stall.py` — read-only autodispatch heartbeat stall check. The default threshold is **20 minutes**; a missing, unreadable, or older heartbeat emits a clear `WARNING` and exits non-zero. It never kills or restarts the real AD.
- `python3 tools/check-ad-heartbeat-chunk.py` — offline #906 contract check asserting that lane-autodispatch's main loop sleeps in chunks of ≤ 5s (`sleep(5)`) between ticks and updates `autodispatch/heartbeat` on each step, preventing host suspend / nanosleep freeze false stalls.
- `python3 tools/check-ad-ssh-command.py` — offline #907 contract check. The fleet host list is parsed from `docs/fleet-topology-expected.md` and the jump-host table from the AD runtime configuration (`$AD_JUMP_HOSTS` / `~/phanes-ctl/autodispatch/jump-hosts.json`), so the script keeps no second copy of the topology; it asserts that a `ProxyCommand` containing spaces stays one `shlex.quote`d argv item and that the `core.sshCommand` of the harvest fetch has no unquoted space breakage. It also fails if the AD source hardcodes a fleet host name.
- `python3 tools/check-ad-queue-filter.py` — offline #903 contract check using fake issue JSON; issues carrying `lane-ready` together with `lane-failed` or `lane-pr` are not dispatchable.
- `python3 tools/check-codex-queue-heartbeat.py` — offline #988 contract check for a Codex status that stays `queued` with a provider rate-limit `queue_reason`. If both `queued_at` and `heartbeat_at` are older than **30 minutes**, the item must be reclaimed; a fresh heartbeat or another queue reason is preserved.
- `python3 tools/check-ctl-loop-reap.py` — offline #951 contract check for ctl-loop heartbeat expiry, pid disappearance, child restart, and duplicate-instance prevention.
- `python3 tools/check-ctl-hb-refresh.py` — offline #1263 contract check: static scan of ctl-loop ensuring the ib-refresh wall-clock wait loop writes ctl-loop.heartbeat using date and enforces REFRESH_CAP.
- `python3 tools/check-ready-refill-contract.py` — offline dual-mode refill contract (Refs #869). It creates a fake backlog and status snapshot with `_ready < 10`, then asserts the next tick's `filed N issue(s) from backlog` log. The check imports the shared `is_thin_card` predicate: a thin card is skipped and a thick card is accepted. It uses no live `~/phanes-ctl`, GitHub, git, SSH, or `gh issue create`; it also asserts `WARNING queue low` when local and repo backlog sources are exhausted.
- `python3 tools/check-ad-ready-target.py` — offline #1242 AD ready-queue contract. With fake `ready`, `LOW`, and `TARGET` values, when `ready < LOW` the refill `need` is `max(TARGET - ready, ...)` (and reaches `TARGET`); `is_thin_card` rejects thin cards. It uses only a local fixture and never calls `gh`.
- `python3 tools/check-ad-task-upload.py` — offline #920 contract check. It captures fake task content, asserts SSH stdin upload via `cat > /tmp/task-*.md`, and rejects a bare `scp` upload.
- `python3 tools/check-fleet-batchmode-probe.py` — offline #954 contract check. It drives the production `ib-refresh` refresh path with a fake BatchMode probe runner: fake probe outputs with non-zero exit codes (such as `Permission denied` and `Connection closed`) never increment `hosts_ok`, while a zero exit code still does. It opens no SSH connection and touches no live instabox node.
- `python3 tools/check-lane-failed-requeue.py` — offline #960 contract check. Before restoring `lane-ready` to a `lane-failed` issue, fake labels must have no `in-progress`/`lane-pr`, `state.json` must have no occupied lane entry, and the issue must still contain an inline acceptance command. It never touches the real issue or state.
- `python3 tools/check-ad-fetch-transient-class.py` — offline #986 contract check. It asserts `bad pack header` and `fetch-pack: unexpected disconnect` stderr samples remain transient and retryable within the harvest bound.
- `python3 tools/check-orphan-runner-window.py` — offline #961 contract check for an assignment whose remote `runner_alive` remains false. After the window expires while `run_state=running`, the assignment is recycled as `orphaned` and the task is relabelled `lane-ready`; the exact window boundary is not expired.
- `python3 tools/check-ad-assign-rollback.py` — offline #968 contract check for an `assign` returning non-zero while the remote lane remains `busy` or `queued` without a matching local `state.json` entry. It detects and clears only the fake remote reservation; it never connects to SSH or kills a real runner.
- `python3 tools/check-ad-mint-cooldown.py` — offline #1260 contract check for seed-mint burst cooling. A fake clock asserts that a second mint whose interval is less than `MINT_COOLDOWN` is skipped and does not refresh `_last_mint`.
- `python3 tools/check-ghost-in-progress.py` — offline ghost in-progress check (Refs #922). Fake fixtures only: an open issue carrying `in-progress` with no lane entry in `state.json` counts as a ghost only when the label is older than **10 minutes** (a dispatch in flight still has no lane record), `lane-pr` issues are protected, and a `lane-failed` ghost is only re-labelled `lane-ready` when `state.json` has no lane occupancy and the issue body keeps its acceptance command (#960). It prints the recommended `gh issue edit` commands and never touches GitHub.
- `python3 tools/check-ad-stale-label-skip.py` — offline #985 contract check. A stale search hit whose authoritative labels still contain both `lane-failed` and `lane-ready` is skipped once; the operator should remove the residual `lane-ready` label before retrying.
- `python3 tools/check-health-hosts-ok-pause.py` — offline #948 contract check. It asserts that a host held by a `PAUSE-HOST-<host>` file is never counted in the `hosts_ok` summary, that it is reported in `skipped_cooldown`, that an expired automatic pause is released, and that a pause that has not expired yet — or a manual one — is never cleared.
- `python3 tools/check-dual-hosts-ok.py` — offline #1637 dual-mode contract check. It drives the production `ib-refresh` refresh path with a fake BatchMode probe runner and a fake `lanes.json`: the reachable lanes are exactly the lanes whose host is in the `ib-refresh.status` `hosts_ok` set and which are not drained, while lanes on probe-failed, cooldown, or topology-unknown hosts (and lanes with no host) are never reachable; with `hosts_ok=0` no lane is reachable and the boot gate is closed. It opens no SSH connection and touches no live `~/phanes-ctl` state.
- `python3 tools/check-ad-review-pause-uncounted.py` — offline #1607 contract check. It drives the hotfixed AD's `box_pause_during(cfg, st, path)` and its `finish_review` no-verdict branch: a review failure that happened while the box VM was paused is not counted, a failure outside the pause window is counted. It reads and writes only a temporary tree, never the live `~/phanes-ctl/autodispatch` state.
- `python3 tools/check-review-evidence-pack.py <pack.md>` — offline #1788 evidence-pack reader. It parses the raw evidence pack of a lane review (`swrm/lib/review/evidence.py`) into structured fields (CI conclusions, test logs, diff statistics, acceptance checks, preliminary opinion) and exits 1 when a required section is missing or the format is broken — the same verdict the dispatcher records in `reviews.json` (`evidence.complete`). It reads one file and never touches GitHub.
- `python3 tools/check-ad-harvest-backoff.py` — offline #947 contract check. It replays a fake harvest fetch failure sequence and asserts the transient/fatal classification, the exponential backoff ladder, and that the first N transient failures only log and back off — the (N+1)-th failure is the one labelled `lane-failed`. It never touches the live lane state.
- `python3 tools/check-merge-idle-heartbeat.py` — offline #955 contract check. Replays the real `merge-loop.sh` in a fake directory fixture with a fake `gh` stub: when idle with no open PR the merge step is skipped (no `gh pr merge` call) while `merge-heartbeat` is still refreshed, so an idle period never looks like a false-positive stall.

### Roll back a failed assign (Refs #968)

An `assign` returning a non-zero code is not proof that the remote reservation
was rejected. The control plane must compare the remote lane with its local
`state.json` after a failed assign:

- `assign_rc != 0`, remote state `busy` or `queued`, and no matching local
  assignment is a ghost reservation;
- a `queued` ghost can be released, and a `busy` ghost can be released only
  when the remote check says `runner_alive=false`;
- a `busy` lane with a live runner is left unchanged. Rollback must not kill a
  real runner or invent a local state entry;
- the check is deliberately offline and uses fake state only:
  `python3 tools/check-ad-assign-rollback.py`.

### Stale search results and residual labels (Refs #985)

`gh issue list --label lane-ready` uses a search index that can briefly retain a
failed issue after it was changed to `lane-failed`. `fresh_ready` re-reads the
issue and rejects the stale hit, so it is not dispatched in that tick. When the
re-read labels still contain both `lane-failed` and `lane-ready`, the log says
`stale search hit lane-failed + lane-ready` and asks the operator to remove the
residual `lane-ready` label; the status snapshot exposes `_stale_search_skips`
and `_stale_lane_failed_skips` for monitoring. Operators can clear the residual
with:

```bash
gh issue edit <N> --remove-label lane-ready
```

Re-add `lane-ready` only after the failure has been reviewed and the issue is
ready for another attempt.

### Orphan runner window after assign (Refs #961)

An assign is not proof that the remote runner started. The control plane must
re-read the remote lane state after the post-assign window:

- `runner_alive=false`, `run_state=running`, and elapsed time **greater than**
  the window means the assignment is an orphan;
- reclaim the orphan assignment, clear its lane ownership, and re-apply the
  `lane-ready` label so the task can be dispatched again;
- `runner_alive=true`, a terminal `run_state`, or an elapsed time at or below
  the window is not an orphan and must remain unchanged.

The offline check uses a fake remote state and makes no SSH or live-state
changes:

```bash
python3 tools/check-orphan-runner-window.py
```

### Host SSH transient backoff (Refs #967)

`lane-autodispatch` keeps transient SSH failures at host scope.  Consecutive
failures use exponential backoff of `30s → 60s → 120s → 240s → 480s`, capped
at `600s`; any successful host probe calls `host_good` and clears that host's
failure count and cooldown.  The offline contract uses a synthetic
failure/success sequence and does not touch the real backoff state:

```bash
python3 tools/check-ad-host-backoff.py
```

### AD harvest fetch backoff and `lane-failed` threshold (Refs #947)

The lane harvest pulls the finished branch from the fleet host with `git fetch`
(`tools/lane-autodispatch/autodispatch.py`).  Every failure is classified
first (`classify_harvest_fetch_error`):

- **transient** — a broken transfer or a flaky ssh mux, as seen in the field
  (M4#904): `fetch-pack: unexpected disconnect`, `bad pack header`, `early eof`,
  `index-pack failed`, `RPC failed`, `Connection reset`, `Connection closed`,
  `broken pipe`, banner/`timed out` and `Permission denied`.
- **fatal** — anything else (e.g. `could not read from remote repository`); a
  retry cannot help.

Rules:

- One harvest cycle runs at most **3** fetch attempts
  (`HARVEST_FETCH_MAX_ATTEMPTS`), 20 s apart (`HARVEST_FETCH_RETRY_DELAY`).
- A transient failure increments the lane's consecutive-failure counter
  (`fetch_failures` in the lane state) and only **logs and backs off**:
  `min(600, 30 * 2 ** (failures - 1))` seconds (30s → 60s → 120s → 240s →
  480s → 600s cap).  The issue stays `in-progress` and the next tick skips the
  lane until `fetch_backoff_until` has passed.
- The first **N** consecutive transient failures (N =
  `HARVEST_FETCH_MAX_TRANSIENT_FAILURES`, default **3**, override with
  `AD_HARVEST_MAX_FAILURES`) therefore never label the issue `lane-failed`:
  only the failure **past** the threshold (N+1) — or a fatal error right away —
  labels it `lane-failed` with an explanatory comment.  A successful fetch
  resets the counter.

The offline contract check replays a fake failure sequence (no SSH, no live
lane state):

```bash
python3 tools/check-ad-harvest-backoff.py
```

### AD queue share for verify cards (Refs #1794)

The dispatch queue is split into two classes: **verify cards** (`verify-intent` label +
`## Verify Intent` section, see #1811) and **work cards**. When the share policy is on, verify
cards get a reserved share of lanes and the work class is capped at `total - reserve`, so a
verify flood cannot starve the work lanes and a work flood cannot starve the verification
cards.

The share is computed from the idle lanes and the runs in flight (lanes that are paused, in
host backoff or in error are not counted); it is never a global static number, and it never
locks all dispatching when there are too few lanes. The work class cap is a real limit: when
`work_slots == 0` (the work class already holds `total - reserve` lanes) no work card is
dispatched on any lane, the work cards wait for a free lane; the reserved share of the verify
class is not affected. Configuration (all read from the environment at AD start-up):

| Variable | Default | Meaning |
| --- | --- | --- |
| `AD_VERIFY_SHARE_ENABLED` | `0` (off) | master switch; with `0` the dispatch order is exactly the one before this policy |
| `AD_VERIFY_SHARE` | `0.2` | reserved share ratio: `reserve = max(AD_VERIFY_MIN, round(total * ratio))` |
| `AD_VERIFY_MIN` | `1` | lower bound of the reserved lanes |

A malformed numeric value falls back to the default (`env_number()`), so a typo in the
configuration cannot kill the AD loop at start-up.

**Rollout**: the policy is off by default, so the box keeps the old behaviour until an
operator enables it explicitly:

```bash
cp /workspace/phanes-repo/tools/lane-autodispatch/autodispatch.py ~/phanes-ctl/bin/autodispatch.py
export AD_VERIFY_SHARE_ENABLED=1     # optional: AD_VERIFY_SHARE=0.2 AD_VERIFY_MIN=1
~/phanes-ctl/bin/start-control-plane # restart the AD loop with the new environment
```

The AD log (`~/phanes-ctl/autodispatch/autodispatch.log`) records every share decision
(`queue share: <lane> prefers verify (...); other class yields`), every work class cap
(`queue share: <lane> work class at its cap (work slots=0, ...); N work card(s) wait`) and the
class of every dispatched card (`queue share: dispatched <class> #<n> on <lane>`). The priority
composition order (class admission → pin class → priority/number) and the offline proof are
documented in [`tools/lane-autodispatch/README.md`](../tools/lane-autodispatch/README.md).

```bash
python3 tools/lane-autodispatch/test_can_take.py
```

### AD ready queue target refill (Refs #1242)

When `lane-autodispatch` ready queue drops below `LOW` (`AD_LOW`, default 10), the refill
loop must request enough candidate cards to refill up to `TARGET` (`TARGET = 20`) rather than
topping up one card at a time. For fake `ready`, `LOW`, and `TARGET` values, the refill count
calculates `need = max(TARGET - ready, starved, 0)` whenever `ready < LOW`.

Candidate issue cards are rejected if they are thin:
- `is_thin_card` discards any card lacking a `# ` title, a `labels:` declaration, a substantive body (at least two lines), or an `验收：` acceptance line.
- The contract is entirely local, validated against local markdown fixtures without calling `gh`, git, or SSH:

```bash
python3 tools/check-ad-ready-target.py
```

Recommended heartbeat monitoring (no SSH and no restart): run this from the
repository checkout every five minutes, for example with cron:

```cron
*/5 * * * * cd /path/to/phanes-ctl && python3 tools/check-ad-heartbeat-stall.py >>/tmp/ad-heartbeat-check.log 2>&1
```

For a manual check, run `python3 tools/check-ad-heartbeat-stall.py` (or pass
`--heartbeat-file PATH --threshold-minutes 20`). Offline smoke fixtures are
`python3 tools/check-ad-heartbeat-stall.py --fixture-old` (expected non-zero)
and `python3 tools/check-ad-heartbeat-stall.py --fixture-fresh` (expected 0).

### Autodispatch chunked sleep and heartbeat refresh (Refs #906)

Drawing from the ~40-minute silence lesson (`brain/lessons-37min-silence.md`) caused by host suspend
freezing nanosleep, `lane-autodispatch` must never perform a single monolithic `sleep(AD_TICK)`.
Instead, the main loop steps between ticks in increments of ≤ 5s (`sleep(5)`), refreshing
`autodispatch/heartbeat` at every step. Even if the host suspends or sleeps, the heartbeat
is continuously refreshed while awake and promptly updated upon resume.

Verify offline (without connecting to SSH or touching running processes):

```bash
python3 tools/check-ad-heartbeat-chunk.py
```

### Codex provider throttling queue recovery (Refs #988)

The lane must continue emitting a heartbeat while its status is `queued`. A
`queue_reason` containing a provider rate-limit signal (`provider`, `429`,
`rate limit`, or `throttled`) is an expected temporary wait, not permission to
leave the lane silent. Recycle the queued item when both `queued_at` and
`heartbeat_at` are more than **30 minutes** old; this is the timeout recovery
threshold, and the next dispatch may retry it. A fresh heartbeat keeps the item
queued. The offline contract is:

```bash
python3 tools/check-codex-queue-heartbeat.py
```

## Health pauses (stability)

`health-loop` writes only files whose body starts with `auto:`:

- `PAUSE-HOST-<host>` / `PAUSE-<LANE>` on hard disk/mem/ssh-streak
- `PAUSE-BACKEND-<pi|agy|codex>` on quota/429 signals (also set by autodispatch on harvest)
- An SSH probe containing `cooling down until` or `machine_cooldown` writes
  `PAUSE-HOST-<host>` with `auto:cooldown:until=<deadline>`; `clear_auto` must
  retain that pause until the deadline has passed.

Manual `touch PAUSE-*` without `auto:` is never cleared by health-loop. Soft alerts
(`disk_soft`/`mem_soft`) stay in `health.alert` with `level=soft` and never create a
`PAUSE-HOST-*` file. The disk boundary contract is checked offline with
`python3 tools/check-health-disk-soft.py`.

For low-memory `worker-b.example.invalid`-class hosts, the offline contract uses
`HEALTH_MEM_HARD_MB=256` and `HEALTH_MEM_SOFT_MB=512`. A `health.json` snapshot
below hard writes `auto:host_hard` to `PAUSE-HOST-worker-b.example.invalid` and `auto:mem_hard`
to each lane pause on that host. When a later snapshot recovers to soft or
above, the health loop self-heals by removing those auto pauses together, so
the same host's lanes become dispatchable again; manual pause bodies are
retained. The executable set/clear contract uses fake health snapshots and a
temporary pause directory: `python3 tools/check-health-pause.py`.

During dual-mode cooldown, the production pause set is restricted to
`PAUSE-HOST-node-1.example.invalid`, `PAUSE-HOST-node-2.example.invalid`, `PAUSE-M1`, and `PAUSE-M2`. The offline
check uses a fake pause directory and never changes the real state:

```bash
python3 tools/check-pause-set-cooldown.py
```

The #1642 soak check verifies that the node-1.example.invalid/node-2.example.invalid host pauses match paused M1/M2
lanes in a fake `lanes-status` snapshot, and that the other lanes can be busy
up to the cooldown limit of nine:

```bash
python3 tools/check-soak-nine.py
```

The SSH cooldown set/hold contract is checked offline with
`python3 tools/check-health-pause.py`; it uses fake probe output and never
starts the resident health loop or connects over SSH.

### Host cooldown vs `hosts_ok` (Refs #948)

`health-loop` writes the probe summary to `~/phanes-ctl/autodispatch/health.json`.
A held host has a `PAUSE-HOST-<host>` file in that same directory. health-loop
writes those bodies with an `auto:` prefix (automatic cooldown); a manual
`touch PAUSE-HOST-<host>` carries no `auto:` prefix but holds the host just the
same.

| `health.json` field | meaning |
|---|---|
| `hosts_ok` | healthy probed hosts **minus** every host that has a `PAUSE-HOST-<host>` file |
| `skipped_cooldown` | healthy hosts skipped from `hosts_ok` because a `PAUSE-HOST-<host>` file exists (automatic cooldown **or** manual hold) |
| `hosts_degraded` | probed hosts whose probe failed (disk/mem/ssh) |

These are host lists; the `ib-refresh.status` row of the same names carries
counts instead.

`hosts_ok` and `skipped_cooldown` partition the healthy probes: a held host is
never reported in `hosts_ok`, and a host without a `PAUSE-HOST-<host>` file is
never reported in `skipped_cooldown`.  `PAUSE-<LANE>` and `PAUSE-BACKEND-*` files
are not host pauses and never change `hosts_ok`.

Release rules, mirroring `clear_auto` in `tools/check-health-pause.py`:

- an `auto:`-prefixed `PAUSE-HOST-<host>` is released once it expired
  (`PAUSE_TTL` = 3600 s in the mirrored spec); an `auto:cooldown:until=<deadline>`
  body must be past its deadline as well;
- a manual pause (body not starting with `auto:`) is never cleared at all;
- the mem hard pauses (`auto:host_hard`, `auto:mem_hard`) are additionally
  removed by the health self-heal path as soon as the host recovers (see the
  low-memory `worker-b.example.invalid` paragraph above);
- a pause that has not expired yet is never cleared.

`clear_auto` has no probe condition: an expired automatic pause is released even
while the host still probes degraded, and that host is then reported in
`hosts_degraded` instead of `skipped_cooldown`.

Offline proof (fake `health.json` + fake `PAUSE-HOST-*` files inside a
temporary directory; the real `~/phanes-ctl/autodispatch/PAUSE-*` files are
never read, written or removed):

```bash
python3 tools/check-health-hosts-ok-pause.py
```

## Lane host disk watermark guard (Refs #1666)

`<host>` runs the CX2/CX4 review lanes; a full disk there stalls the reviews.  The
guard is a resident loop of the box control plane that measures the **remote**
filesystem of the lane host over ssh (the box and `<host>` have very different
disks, so the local disk says nothing about the lane host) and holds the lanes of
that host while the disk is too full.

| Artefact | Role |
| --- | --- |
| `tools/disk-usage-probe.py` | the usage probe: `used_pct` / `used_bytes` / `free_bytes` and the top directories, ranked by the **allocated** blocks (`st_blocks`), not by the apparent size (a sparse file looks huge but occupies nothing) |
| `tools/disk-watermark-guard.py` | the guard: measures the host over ssh, applies the watermark policy, writes the alert row into the brain log and owns the pause marker |
| `tools/install-disk-watermark-guard.sh` | the installer: deploys the guard **and** its probe dependency, wires the resident loop into the live `~/phanes-ctl/bin/start-control-plane`, replaces the guard that is still running and verifies the new version |
| `tools/check-disk-watermark.py` | the offline contract check (no ssh, no live state) |

Watermark policy (the thresholds are the ones of the lane host):

| Measured usage | State | Effect |
| --- | --- | --- |
| `>= 95%` | `block` | the independent disk gate pauses the host and the brain log gets an alert row |
| `> 87%` and `< 95%`, previously blocked | `hold` | the pause **stays** (hysteresis): the lane is not released before the recovery threshold |
| `> 87%` and `< 95%`, previously unblocked | `warn` | the brain log gets a warning; dispatch stays open and no pause is created |
| `<= 87%` | `recovered` | the guard releases **its own** marker only |

Ownership rules: the guard writes its diagnostic marker only in
`~/phanes-ctl/autodispatch/.disk-watermark/PAUSE-HOST-<host>`. The shared
`~/phanes-ctl/autodispatch/PAUSE-HOST-<host>` belongs to health checks and
operators; the guard never writes or deletes it. Checking its body before a
replace/unlink cannot protect a concurrent independent writer. A removed
private marker is restored on the next tick; the independent state file below
holds the gate throughout that interval. A **failed** probe is never a healthy
host: the guard fails closed (the host stays paused) and says so in the brain log.

The dispatch gate is independent of the pause marker: the dispatcher reads
`~/phanes-ctl/autodispatch/disk-watermark-<host>.json` (written by the guard) and
holds every lane of that host (`host:<host>` scope) even in the window where the
health sweep has just cleared the marker. Blocked records never expire: if the
guard stops, the host remains blocked until a successful probe records recovery.
When upgrading from guard version 1, the shared `auto:disk-watermark host=...`
marker is left untouched. Once a version 2 state exists, the updated dispatcher
uses that state in place of the legacy disk marker, so recovery can open the
gate without an unsafe deletion. Manual and other guards' shared markers still
hold the host, including when they replace the legacy marker during recovery.

Deploy (on the box, from the repository mirror):

```bash
bash tools/install-disk-watermark-guard.sh            # default: $HOME/phanes-ctl
```

The installer stops the resident guard that is still running (an old process would
keep the old code alive), deploys `disk-watermark-guard.py` and
`disk-usage-probe.py` next to each other into `~/phanes-ctl/bin`, wires the
resident loop into the live `~/phanes-ctl/bin/start-control-plane`, updates the
dispatcher that carries the gate and verifies that the resident guard of the new
version really runs. If a dispatcher is running, the installer checks that its
restart entry identifies a dispatcher it can update before stopping any process;
an unknown entry fails installation and preserves the running processes. A
dispatcher that was stopped must be running again for installation to succeed.
Alerts go to `/workspace/phanes/brain/log.md`, the same
brain log used by the dispatcher. Set `DISK_WATERMARK_BRAIN_LOG` during installation
to override this path for an isolated deployment. The probe counts allocated
blocks throughout each directory tree by default; `--max-depth` explicitly
requests a partial scan.

Review fixes and evidence:

- Marker ownership race: private markers separate disk writes/releases from
  shared host pauses. `check-disk-watermark.py` injects manual and other-guard
  writes (in-place and atomic replacement) at creation, refresh and unlink,
  verifies the shared bytes survive, and checks v1 upgrade and manual pauses
  in the actual dispatcher gate. The regression suite keeps its block/hold/
  recovery, marker removal and deployment checks against the private marker;
  foreign ownership checks still exercise the shared host marker.
- Recovery boundary: the guard and offline check exercise 95% → 91% → 87%;
  at 87% the own marker is removed, the independent gate opens and recovery is logged.
- Dispatcher restart: installer tests cover the updated resident dispatcher's
  heartbeat, an unknown restart entry preserving the old process, and a start
  script that fails to restart the dispatcher returning an installation error.
- B1: blocked state has no TTL; the real dispatch tick test removes the marker
  and ages the state past 30 minutes, then verifies that <host> gets no run.
- B2: the probe scans all depths by default; a deep 1 MiB payload must be counted
  and rank above a shallow 64 KiB directory.
- B3: the installer, startup example and guard default use the dispatcher brain
  log path; the installation test verifies a block alert at the configured path.

Manual one-shots:

```bash
python3 ~/phanes-ctl/bin/disk-watermark-guard.py --pause-dir ~/phanes-ctl/autodispatch \
    --brain-log /workspace/phanes/brain/log.md --host <host>      # measure once and apply the policy
python3 ~/phanes-ctl/bin/disk-usage-probe.py --path / --top 10   # the usage probe of a host
```

Offline acceptance (run from the repository root; no remote SSH or live control
plane state):

```sh
git diff --check && python3 tools/check-disk-watermark.py && python3 -m unittest tools.tests.test_disk_watermark_guard
```

Use that complete command in the host verifier, rather than the prose placeholder
`run the acceptance command(s) named in the issue`. The placeholder is not shell
syntax and fails before any test runs. The `&&` chain preserves each failing
check's exit status and stops immediately; a shell parse error is not evidence
that the disk guard itself failed.

The contract check covers probe allocation, hysteresis, concurrent foreign-marker
writes, failed probes, the dispatch gate and the paired probe dependency. The
unittest suite also exercises installation and replacement of the resident guard
inside an isolated fake control-plane tree. Passing these offline checks does not
claim a production deployment or a successful SSH probe of the live lane host;
those remain separate operator verification steps above.
### Executor task / zombie watermark probe (Refs #1826)

The executor services of a lane leak: their cgroup fills up with zombie children until
`TasksMax` is hit and `fork` fails (`exec_error` storm, 13 h unnoticed).  Neither the
disk/mem/ssh probes of `health-loop` nor the lane reaper carries a task or zombie
counter.

The repo mirror of that probe is `tools/executor-task-health.py`.  It samples one fleet
host over ssh, enumerates every `openswarm-executor-*.service` of the host, reads
`TasksCurrent` / `TasksMax` of the unit and the pids of its cgroup, counts the `Z` state
processes of that cgroup and its descendants (and their parent pids), writes the result
into `health.json` and raises the watermark alert. **It only alerts**: it never restarts,
stops or kills a service (the restart of a leaking lane is a decision of the coordinator).

```bash
python3 tools/executor-task-health.py <host> \
  --health-json ~/phanes-ctl/autodispatch/health.json \
  --alert-file ~/phanes-ctl/autodispatch/health.alert \
  --snapshot-dir ~/phanes-ctl/autodispatch/executor-task-snapshots
```

The box runtime (`~/phanes-ctl/bin/health-check.py`) lives outside this repository; wiring
the probe into that loop is a deployment step, not part of this card.

`health.json` updates use an exclusive `flock` on the stable sidecar
`health.json.lock`, held from **before reading** the current summary until the merged
JSON has been atomically replaced. All writers, including the external health-check
loop, must use that same sidecar and reread the summary under the lock; advisory
locking cannot protect against a writer that ignores it. Do not lock the JSON inode
itself or delete the sidecar, since replacement would let writers lock different
inodes. A malformed or unreadable existing summary fails the probe write instead of
being overwritten with an empty summary. Temporary JSON files are created beside the
target, flushed and replaced atomically, and removed if writing fails.
Replacement preserves the existing JSON file's permission bits; a newly created
summary keeps the temporary file's default `0600` permissions.

| watermark | level | condition |
|---|---|---|
| zombie count | `soft` | `Z > 50` |
| zombie count | `hard` | `Z > 200` |
| task ratio | `soft` | `TasksCurrent / TasksMax > 0.6` |
| task ratio | `hard` | `TasksCurrent / TasksMax > 0.85` |

Rules of the probe:

- a host without systemd (the macOS box `<host>`) is **skipped** - not degraded, and it is
  not sampled at all;
- a failed sample (ssh error, missing `systemctl show`) is recorded as **not collected**
  (`"level": "uncollected"`) and never alerts;
- a failed bulk `/proc` cgroup mapping marks the host and every enumerated service
  **not collected**, without alerts, because descendant cgroup zombies cannot be
  counted reliably without that mapping. A failed or empty `/proc/<pid>/cgroup` read
  is skipped only if `/proc/<pid>` has disappeared (the process exited during the
  sample); otherwise it aborts the remote command. PID-only or malformed mapping
  records also fail sampling;
- when an alert fires, the full `ps -eo pid,ppid,stat,etimes,args` table is written to the
  snapshot directory **before** the alert is appended to `health.alert`; if the snapshot
  cannot be saved, no alert is written (no evidence, no alert);
- the duplicate `fleet-capacity --sweep-loop` processes are grouped by their full command
  line: more than two instances of the same command alert with that command, its parent
  processes and the run id of the parent. `sweep_loop.count` summarizes all sweep
  processes; thresholds apply separately to each entry in `sweep_loop.groups`. They are
  never killed.

The probe adds one field to `health.json`; the summary of the control plane (`at`,
`hosts`, `hosts_ok`, `hosts_degraded`) stays untouched - the host status string of the
health loop is not rewritten by the probe:

```json
{
  "at": "2026-11-03 19:15:00",
  "hosts": {"<host>.example.invalid": "degraded"},
  "hosts_ok": ["m9.example.invalid"],
  "hosts_degraded": ["<host>.example.invalid"],
  "executor_tasks": {
    "<host>.example.invalid": {
      "status": "collected",
      "host": "<host>.example.invalid",
      "at": "2026-11-03 19:15:00",
      "services": [
        {
          "service": "openswarm-executor-M19.service",
          "collected": true,
          "level": "hard",
          "tasks_current": 869,
          "tasks_max": 1552,
          "tasks_ratio": 0.56,
          "zombie_count": 844,
          "zombie_parent_pids": [1210977],
          "control_group": "/system.slice/openswarm-executor-M19.service"
        }
      ],
      "sweep_loop": {
        "count": 3,
        "limit": 2,
        "pids": [627900, 627901, 627902],
        "parent_pids": [627802],
        "run_ids": ["20261103-214705-b66c3f"],
        "groups": [
          {
            "command": "/usr/bin/fleet-capacity --sweep-loop <host>",
            "count": 3,
            "limit": 2,
            "pids": [627900, 627901, 627902],
            "parent_pids": [627802],
            "run_ids": ["20261103-214705-b66c3f"]
          }
        ]
      }
    }
  }
}
```

The matching alert line (appended to `health.alert`, one line per alerting service):

```
2026-11-03 19:15:00 level=hard source=executor-tasks host=<host>.example.invalid service=openswarm-executor-M19.service zombies=844 tasks=869/1552 ratio=0.56 parents=1210977 snapshot=/home/phanes-ctl/autodispatch/executor-task-snapshots/executor-tasks-gores1.example.invalid-20261103-191500-000000.ps
```

Offline proof (a fake fleet host answers the ssh commands of the probe; no network, no
ssh, no systemd, no root):

```bash
python3 -m pytest -q tools/tests/test_executor_task_health.py
```

Review mapping (the open findings of the pull request of the issue):

| finding | change | test |
|---|---|---|
| B1 - no service enumeration / collection / `health.json` write / no-systemd skip | `collect()` enumerates the units over ssh, samples `systemctl show` + `cgroup.procs` + `ps`, `write_health_payload()` adds `executor_tasks` to `health.json` without touching the summary, a host without systemd returns `skipped` | `ExecutorServiceCollectionTests` (4 tests), `ExecutorProbePresenceTests` |
| B2 - no snapshot/alert chain, the tests only covered the parser | `update()` saves the `ps` snapshot before it appends the alert, `build_alerts()` names the service, the level and the zombie parent pids; a failed sample never alerts | `ExecutorAlertChainTests` (6 tests), `ExecutorWatermarkThresholdTests` (7 tests) |
| B3 - the zombie count of `cgroup.procs` misses the not yet reaped zombies | the zombie count comes from the `ps` table; a zombie belongs to the unit cgroup when the cgroup lists it, when its parent lives in that cgroup, or when the bulk `/proc` cgroup dump says so | `ExecutorWatermarkThresholdTests.test_the_zombie_parent_pids_are_reported`, `...test_more_than_fifty_zombies_is_soft` |
| B4 - the chain tests were not in the CI discovery path | the tests live in `tools/tests/` (`unittest discover -s tools/tests` picks them up) and every test class sits above `unittest.main()`, so a direct run finds them too | `python3 -m unittest discover -s tools/tests -p 'test_*.py'` |
| card - the duplicate sweep loops of #1826 | `detect_sweep()` groups the `fleet-capacity --sweep-loop` processes by full command line and reports their parents and the run id of the parent | `ExecutorSweepLoopTests` (2 tests) |

Implementation review follow-up (baseline `e05a8f2ee`):

| blocker | fix | regression in `ExecutorReviewRegressionTests` |
|---|---|---|
| 1 - SSH strips remote shell quotes, losing bulk cgroup attribution | pass the complete remote command as one SSH argument, preserving shell quotes | `test_ssh_preserves_shell_quotes_for_the_bulk_cgroup_map` executes the command through a real shell with fixture `/proc` files, then verifies 51 zombies outside `cgroup.procs` trigger `soft` with the parent PID |
| 2 - failed `cgroup.procs` read looks like a healthy empty cgroup | return `uncollected` on read failure or missing `ControlGroup`, without a service alert | `test_failed_cgroup_read_is_uncollected_and_never_alerts`, `test_missing_control_group_is_uncollected` |
| 3 - concurrent health writers lose summary updates | lock before reading, merge the latest summary, atomically replace it under the same lock | `test_health_update_waits_for_writer_and_reads_its_latest_summary` uses a separate probe process; `test_failed_atomic_replace_preserves_the_existing_summary` and `test_invalid_health_json_is_preserved_instead_of_overwritten` verify failure safety |

Second implementation review (baseline `42425ead7`), regressions in `ExecutorReviewRegressionTests`:

| blocker | fix | regression |
|---|---|---|
| 1 - zombies in descendant cgroups are missed when the root PID list is empty | match the service cgroup itself or descendants separated by `/`, excluding similarly prefixed sibling paths | `test_nested_cgroup_zombies_alert_without_root_cgroup_pids` verifies 51 zombies, parent PID, snapshot and soft alert; `test_similarly_prefixed_cgroup_is_not_part_of_the_service` excludes unrelated paths |
| 2 - different sweep commands combine into a false alert | group by the full command line and apply the threshold to each group, with only its own parent PIDs and run IDs in its alert | `test_different_sweep_commands_do_not_combine_into_an_alert`, `test_only_duplicate_sweep_commands_alert_with_their_own_parents` |

Third implementation review (baseline `a22027eaa`), regressions in `ExecutorReviewRegressionTests`:

| finding | fix | regression |
|---|---|---|
| B1 - failed bulk cgroup mapping silently becomes an empty mapping | record the host and every enumerated service as `uncollected`, persist the error in `health.json`, and suppress alerts for the failed sample | `test_failed_cgroup_map_marks_each_service_uncollected_without_alerting` covers an empty root cgroup, 51 zombies, a second service above the hard task threshold, and duplicate sweep loops |
| B2 - systemd's failed-unit marker hides executor services | accept the optional leading `●` in `list-units` output and sample the unit normally | `test_failed_unit_marker_is_parsed_and_the_service_is_sampled` covers marked and unmarked executor services and verifies the marked service's zombie alert |
| follow-up - atomic replacement changes existing summary permissions to `0600` | copy the existing permission bits to the temporary file before replacement | `test_health_atomic_replace_preserves_existing_permissions` covers `0644`, `0640`, and `0600` |

Fourth implementation review (baseline `1c890c4e4`), regressions in `ExecutorReviewRegressionTests`:

| blocker | fix | regression |
|---|---|---|
| B1 - `printf` masks failed bulk cgroup reads and parsing ignores incomplete records | exit nonzero immediately on a failed read and reject PID-only or malformed records, using the existing host/service `uncollected` path | `test_bulk_cgroup_command_propagates_a_read_failure` executes the remote command in a real shell with a missing middle file; `test_incomplete_cgroup_map_is_uncollected_instead_of_zero_zombies` verifies 51 zombies with an empty root PID list, persisted failure for every service, and no alerts or snapshots |

Fifth implementation review (baseline `32cf3ae6d`), regression in `ExecutorReviewRegressionTests`:

| blocker | fix | regression |
|---|---|---|
| B1 - normal process exits abort the bulk cgroup sample and suppress all host alerts | skip failed or empty reads only after confirming that the process directory disappeared; continue to fail sampling for unreadable surviving processes or malformed records | `test_bulk_cgroup_command_skips_exited_processes_and_keeps_alerts` forces an exit during the actual shell command, covering both failed and empty reads, then verifies 51 descendant zombies, their parent PID, soft and hard service alerts, persisted health payload and the full ps snapshot; existing read-failure and incomplete-record regressions remain in place |


## ctl-loop child reaping and restart (Refs #951)

`~/phanes-ctl/bin/ctl-loop` is the supervisor for the resident `ad` (`lane-autodispatch`),
`health` (`health-loop`), and `ib-refresh` children. Each child has one pid file and one
heartbeat file. The canonical heartbeat paths are `autodispatch/heartbeat`,
`autodispatch/health.heartbeat`, and `ctl-loop.heartbeat`; the corresponding pid files are
`autodispatch/autodispatch.pid`, `autodispatch/health.pid`, and `ib-refresh.pid` under
`~/phanes-ctl`.

On every supervisor tick, ctl-loop must:

1. read and validate each pid, and treat a missing, malformed, or exited pid as unhealthy;
2. treat a missing heartbeat or one older than 120 seconds as unhealthy;
3. restart each unhealthy child exactly once; if its old pid is still alive, terminate and
   reap that process before spawning its replacement;
4. write the replacement pid and wait for a fresh heartbeat before considering the child
   healthy again.

The pid check is the duplicate-instance guard: a stale heartbeat never authorizes a second
copy to run alongside a live old pid. The offline fixture contract covers fresh/stale
heartbeats, missing/exited pids, and the one-decision-per-child rule:

```bash
python3 tools/check-ctl-loop-reap.py
```

### ctl-loop ib-refresh heartbeat maintenance (Refs #1263)

During `ib-refresh` (instabox certificate renewal), `ctl-loop` enters a wall-clock wait
loop bounded by `REFRESH_CAP` (default 420s). In accordance with the 37-minute silence
lesson (`brain/lessons-37min-silence.md`), long child waits must never pause supervisor
heartbeats: `ctl-loop` must refresh `ctl-loop.heartbeat` with the current `date` timestamp
inside every iteration of the wait loop.

The offline static scanner verifies that `bin/ctl-loop` enforces `REFRESH_CAP`, references
`ctl-loop.heartbeat`, and updates it via `date` within the wait loop:

```bash
python3 tools/check-ctl-hb-refresh.py
```

Autodispatch skips paused host/backend/lane and keeps other backends busy.

## Lane memory ceiling on low-memory hosts (Refs #867)

Lane hosts with ~1 GiB RAM (`worker-a.example.invalid`, `worker-b.example.invalid`) should lower the memory ceiling of the
generated lane unit instead of editing an installed unit:

- per lane: `"memory_max": "700M"` in that host's `$OPENSWARM_HOME/lanes.json`
- per host: `{"lane": {"memory_max": "700M"}}` in that host's `$OPENSWARM_HOME/policy.local.json`

`swrm lane systemd-unit <lane>` then prints `MemoryMax=700M`; without either setting the
default stays `900M`, so higher-memory hosts are unaffected. The recommended values and the
launchd equivalent are documented in [`docs/fleet-worker-cx5.md`](fleet-worker-cx5.md).

## Per-lane proxy and token environment (Refs #1347)

Give one lane its own proxy or token in `lanes.json` instead of a unit drop-in, a wrapper
script or a global `/etc/environment` proxy:

- `"env_file": "/path/to/local-secrets/<lane>.env"` (absolute path or list, `KEY=value` lines, mode `600`)
- `"env": {"NO_PROXY": "localhost,127.0.0.1"}` (string map; overrides `env_file`)

The lane supervisor applies both on systemd and launchd hosts alike; the generated unit or plist
only carries `env`. A too-open file is rejected at config load, a missing one stops the lane.
`OPENSWARM_HOME`/`OPENSWARM_LANES_FILE` cannot be set this way, and `pi` lanes reject both fields.

## Backend pause gate and recovery (Refs #768)

`PAUSE-BACKEND-*` files live in the autodispatch state dir `~/phanes-ctl/autodispatch/`
(`D` in `tools/lane-autodispatch/autodispatch.py:11`). The **presence** of the file is
the gate; the body only decides whether `health-loop` is allowed to clear it.

| `~/phanes-ctl/autodispatch/PAUSE-BACKEND-<backend>` | autodispatch | health-loop |
|---|---|---|
| absent | that backend is dispatchable again (next tick) | — |
| present, body starts with `auto:` | that backend is skipped, other backends keep running | cleared after the hold window when no fresh quota/429 signal arrives |
| present, body empty / not `auto:` (manual `touch`) | that backend is skipped | never cleared; the operator removes it |

Recovery re-dispatches the **same** backend:

- `auto:` pause — `health-loop` deletes the file once the hold window passes with no
  fresh quota/429 signal. On the next `autodispatch` tick that backend's lanes take work
  again (own `backend:<same>` pin first, then unpinned, then cross-pin — see
  `tools/autodispatch-backend-fallback.md`). Nothing has to be re-labelled: an issue that
  was skipped because its backend was paused is still `lane-ready` in the queue.
- manual pause — `rm -f ~/phanes-ctl/autodispatch/PAUSE-BACKEND-<backend>`; the next tick
  dispatches the same backend again.

Offline proof (no SSH; nothing in the running `phanes-ctl` is read or written):

```bash
bash tools/check-ctl-boot.sh --gate-only   # replay the gate in a scratch dir
```

The script asserts the naming convention `PAUSE-BACKEND-<pi|agy|codex>`, that only
`auto:` bodies are auto-clearable (manual bodies are not), and that deleting the pause
file makes the same backend dispatchable again.

Mirror drift to fix separately: the repo mirror `tools/lane-autodispatch/autodispatch.py`
only gates on `PAUSE` / `PAUSE-<LANE>` (lines 8 and 229); the `PAUSE-BACKEND-*` gate runs
in the box runtime (`~/phanes-ctl/bin/autodispatch.py`) and is not mirrored yet.

## 幽灵 in-progress 判定与重贴处置 (Refs #922)

现场双模式运行中，常见 issue 带有 `in-progress` 标签但 `state.json` 无对应 lane 记录（如 runner 异常退出、lane state 被清理或调度中断）。`lane-autodispatch` 的 `DISPATCH_EXCLUDING_LABELS`（`tools/lane-autodispatch/autodispatch.py:22`）包含 `in-progress`，`dispatchable_issue`（`tools/lane-autodispatch/autodispatch.py:73-76`）因此跳过这类 issue：它既不会被再次调度，也没有 worker 在处理，沦为“幽灵 in-progress”（ghost in-progress）。

### 判定原理 (offline detection)

1. **提取活跃 issue**：解析 `~/phanes-ctl/autodispatch/state.json` 中各活跃 lane 的 issue 字段（lane 名为顶层键，`_done`/`_imported` 等下划线键只是簿记，不算 lane 占用）：
   ```python
   active_issues = {v["issue"] for k, v in state.items() if not k.startswith("_") and isinstance(v, dict) and "issue" in v}
   ```
2. **排除在途派发（必须先做）**：`tick()` 先在 `tools/lane-autodispatch/autodispatch.py:290` 打 `in-progress` 标签，直到 `:294` 才把 lane 写进 `state.json`；中间的 `dispatch()`（`:188`）含一次 SSH 任务上传（timeout 60s）与一次 `lane assign`（timeout 180s），最坏约 240s。该窗口内 issue 同样“有 `in-progress`、无 lane 记录”，外观与幽灵完全一致，**不能**按幽灵处理。
   - 工具侧：只有 `in-progress` 标签陈旧超过 **600s（10 分钟）** 才判为幽灵；读不到 `updatedAt` 时一律按“在途/不确定”跳过，不给重贴建议。
   - 运维侧：运行前确认没有在途派发（见下方“运行前的前置条件”）。
3. **排除 lane-pr**：main 的孤儿回收显式保护带 `lane-pr` 的 issue（`tools/lane-autodispatch/autodispatch.py:145`），本判定同样跳过它们，不做重贴。
4. **离线判定幽灵**：open 状态、标签含 `in-progress`、不在 `active_issues` 中、无 `lane-pr`、且 `updatedAt` 已超过阈值 → 判定为无 lane state 的幽灵 issue。

### 运行前的前置条件

先确认没有在途派发，避免把活跃派发误判为幽灵、并被另一条空闲 lane 重复接单：

```bash
ls ~/phanes-ctl/autodispatch/PAUSE ~/phanes-ctl/autodispatch/PAUSE-* 2>/dev/null  # 暂停整个 AD 或单条 lane
jq -c '{busy:._busy,ready:._ready,at:._at}' ~/phanes-ctl/autodispatch/lanes-status.json  # 与远端 lane 状态比对 _busy
```

### 推荐处置 (remediation)

对幽灵 issue 执行**去 IP 重贴 lane-ready**，把遗留的执行中标签收割并恢复为待调度任务：

```bash
# 普通幽灵（无 lane-failed）
gh issue edit <issue-number> --remove-label in-progress --add-label lane-ready

# 带 lane-failed 的幽灵：必须同时去掉 lane-failed，否则会造出 lane-failed + lane-ready 组合（#903 不可派）
gh issue edit <issue-number> --remove-label in-progress --remove-label lane-failed --add-label lane-ready
```

`lane-failed` 的重贴沿用 #960 契约（`tools/check-lane-failed-requeue.py:36-43`）：`state.json` 里不得还有 lane 占用，issue 体内必须保留验收命令。任一条不满足时，工具只输出“去掉标签”的命令并提示先补齐条件，**不会**直接 `--add-label lane-ready`；带 `lane-pr` 的 issue 一律不判为幽灵、不重贴。

### 离线验证

```bash
python3 tools/check-ghost-in-progress.py
```

该脚本内置假 `state.json` 与假 label 列表进行自测与契约验证，输出检测到的幽灵 issue 及推荐重贴命令，绝不修改真实 GitHub。

用真实只读数据自检（先在 box 上取快照，再离线判定）：

```bash
gh issue list --state open --label in-progress --json number,title,labels,state,updatedAt,body > ~/ghost-check-issues.json
cp ~/phanes-ctl/autodispatch/state.json ~/ghost-check-state.json
python3 tools/check-ghost-in-progress.py --state-file ~/ghost-check-state.json --issues-file ~/ghost-check-issues.json
```

`--min-age-seconds` 可调陈旧度门槛（默认 600s），`--now` 可固定“现在”以复现判定结果。
## Autodispatch git_ssh_command 与 ProxyCommand 引用保护 (Refs #907)

Autodispatch 在 harvest 阶段通过 `core.sshCommand` 拉取各 host 分支。若某 host 需要经跳板访问，AD 会给它注入一条含空格的 `ProxyCommand`（如 `ssh -W %h:%p <跳板别名>`）。

写入 `core.sshCommand` 必须经 `shlex.quote`（`shlex.join` 对每个参数逐一引用）。如果未引用，git 在调用 shell 解析命令串时会将 `ProxyCommand` 拆碎（如拆出独立的 `-o ProxyCommand=ssh` 与孤立参数），导致 `ssh` 打印 usage 并使 harvest 失败。

- 跳板拓扑不在代码里写死：AD 从运行配置读跳板表 `$AD_JUMP_HOSTS`（JSON）或
  `~/phanes-ctl/autodispatch/jump-hosts.json`，默认空表 → 完全依赖 box 端 ssh config 的
  `ProxyJump`。事实来源与核对依据见 [`docs/dual-mode-ssh.md`](dual-mode-ssh.md)（含
  “命令行 `-o ProxyCommand=` 会先于 config 里的 `ProxyJump` 生效”这条优先级）。
- 引用路径：`ssh_opts(host)` 生成选项向量 → `git_ssh_command(opts)` 用 `shlex.join` 引用；
  `git_fetch_command()` 复用同一路径写 `core.sshCommand`；`ssh_argv()` 反解回 argv，
  状态探针（`remote()`）与 dispatch（`dispatch()`）都走这一条 argv 构造路径。
- 校验工具：`python3 tools/check-ad-ssh-command.py` 对样例 host 生成命令串并断言不含未引用空格破损；跳板主机集合取自 AD 运行配置，脚本里不另抄一份。

### 部署说明（仓库镜像 ≠ 运行副本）

本改动落在仓库镜像 `tools/lane-autodispatch/autodispatch.py`；box 上真正运行的是
`~/phanes-ctl/bin/autodispatch.py`。不同步到运行副本，线上 harvest 行为不变：

```bash
cp tools/lane-autodispatch/autodispatch.py ~/phanes-ctl/bin/autodispatch.py
```

若 box 需要 AD 自己注入跳板（而不是用 ssh config 的 `ProxyJump`），另外在运行副本的
状态目录放 `~/phanes-ctl/autodispatch/jump-hosts.json`（JSON 对象
`{"<host>": "<ProxyCommand>"}`），格式与取值见
[`docs/dual-mode-ssh.md`](dual-mode-ssh.md)。

## 审查失败在 box VM 暂停期间不计 fails（Refs #1607）

11-02 08:3x 控制面 AD（`~/phanes-ctl/bin/autodispatch.py`）热修：本机车道的审查 run 以
`exec_error` 结束、且 `box-pauses.jsonl` 里有一条 `gap_s ≥ 120`、`from` 落在
`[派发时间−5s, 现在]` 的暂停时，记 `box VM paused …s (…→…) during run → not counted`，
**不**增加该 PR 的 fails。起因：07:31→07:49 的暂停打断了 PR #1563/#1572/#1577 三个审查，
#1572 被记了 2 次失败。

AD 本身不加新功能，只用离线契约检查锁住这个修复：

```bash
python3 tools/check-ad-review-pause-uncounted.py
```

脚本用 importlib 加载 `--ad`（默认 `~/phanes-ctl/bin/autodispatch.py`，运行副本见
[`tools/lane-autodispatch/README.md`](../tools/lane-autodispatch/README.md)），表驱动断言
`box_pause_during(cfg, st, path)`：

- 暂停落在窗口内（`from = dispatch−3` / `dispatch−4`）→ 返回该条暂停记录；
- `from = dispatch−5`（窗口端点）与 `gap_s = 120`（阈值）→ 仍命中；
- `from = dispatch−6`（窗口外）、`gap_s = 119`（低于阈值）、远端车道（host 不在 AD 的本机
  列表）→ 返回 None；
- 坏行跳过，其后的合法行仍然命中。

再用临时目录 + 假 `gh`/`ssh` 打桩调用 `finish_review` 的无 verdict 分支：暂停命中时
`reviews.json` 里该 PR 的 fails 不变、日志含暂停专有标记与 `not counted`；未命中时 fails+1。
夹具用 AD 真实的 `at` 派发时间（`%F %T` 墙钟串，`tools/lane-autodispatch/autodispatch.py:837`）
与生产格式的暂停记录（`{"from": "%Y-%m-%d %H:%M:%S", "to": …, "gap_s": N}`，样本见
`swrm/tests/test_dispatch_pause_window.py`）。

隔离：脚本在 import 之前把 `HOME` 与 `OPENSWARM_CTL_STATE_DIR` 指向临时目录（AD 在 import
期就绑定 `D = f"{HOME}/phanes-ctl/autodispatch"`），再断言 AD 的 reviews.json /
box-pauses.jsonl / 日志路径都落在该目录内，因此不会读写真实控制面状态，也不会往
`brain/log.md` 写假审查行。热修只存在于 box 运行副本，所以本机没有该副本时脚本输出
`SKIP`（不假装契约成立），用 `--ad PATH` 可指定别的 AD 文件。

## Manual one-shots

```bash
setsid nohup ~/phanes-ctl/bin/lane-autodispatch >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/bus-worker-loop >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/lane-merge >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/ctl-loop >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/health-loop >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/disk-watermark-guard.py --loop --pause-dir ~/phanes-ctl/autodispatch --brain-log /workspace/phanes/brain/log.md --host <host> >/dev/null 2>&1 &
~/phanes-ctl/bin/ib-refresh                  # manual certificate refresh preflight
```

Do not invent systemd units here; a documented start script is enough.
