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

## Key loops: startup commands & heartbeats

The `start-control-plane` suite manages five resident loops (五件套: `ib-refresh-loop`, `lane-autodispatch`, `lane-merge`, `bus-worker-loop`, `health-loop`), with their startup commands and heartbeat/status files:

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
     is the dispatchable count after `_blocked` is removed. `_stale_stripped_n`
     (also exposed as `_stale_search_skips`) is the non-negative count of
     stale-search-index entries stripped by the current autodispatch process;
     `_stale_lane_failed_skips` counts the subset of those stale hits whose
     issue still carries both `lane-failed` and `lane-ready` (Refs #985).
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

3. **`lane-merge`** (merge Linux-green `fleet/*` PRs):
   - **Startup command**: `~/phanes-ctl/bin/lane-merge`  
     (or background one-shot: `setsid nohup ~/phanes-ctl/bin/lane-merge >/dev/null 2>&1 &`; script: `merge-loop.sh`)
   - **Heartbeat path**: `~/phanes-ctl/autodispatch/merge-heartbeat`
   - **Idle heartbeat contract (Refs #955)**: when idle (no PR pending or no open PR in queue), `merge-loop.sh` skips the merge step (no `gh pr merge` call) but still updates the `merge-heartbeat` timestamp on each tick to prevent false-positive stall detection. Check with `python3 tools/check-merge-idle-heartbeat.py`.

4. **`bus-worker-loop`** (GitHub issue bus executor loop):
   - **Startup command**: `~/phanes-ctl/bin/bus-worker-loop`  
     (or background one-shot: `setsid nohup ~/phanes-ctl/bin/bus-worker-loop >/dev/null 2>&1 &`; CLI: `openswarm bus-worker --bus <bus-repo> --name <worker-name> --backend <backend>`)
   - **Heartbeat path**: `~/phanes-ctl/bus-worker/heartbeat`
   - **Status / work directory**: `~/phanes-ctl/bus-worker/`

5. **`health-loop`** (disk/mem/ssh/quota probes; auto PAUSE-*):
   - **Startup command**: `~/phanes-ctl/bin/health-loop`  
     (or background one-shot: `setsid nohup ~/phanes-ctl/bin/health-loop >/dev/null 2>&1 &`; probe: `python3 ~/phanes-ctl/bin/health-check.py`)
   - **Heartbeat path**: `~/phanes-ctl/autodispatch/health.heartbeat`
   - **Status / alert snapshot**: `~/phanes-ctl/autodispatch/health.json`, `health.alert`

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
`skipped_cooldown`. When `need_ok` is configured, the gate stays healthy when
`hosts_ok >= need_ok`; cooldown hosts are omitted from an alternate fleet
config, so three reachable hosts can keep `ok=1` while two hosts cool down
(for example, `ok=1 hosts_ok=3 skipped_cooldown=2`).
Hosts absent from the configured topology are errors and are excluded from
`hosts_ok` (Refs #1164); exercise this invariant with
`python3 tools/check-fleet-unknown-host.py`.
For the ib1/ib2 cooldown case, exercise this accounting with the fake-input check
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
- `python3 tools/check-lane-failed-requeue.py` — offline #960 contract check. Before restoring `lane-ready` to a `lane-failed` issue, fake labels must have no `in-progress`/`lane-pr`, `state.json` must have no occupied lane entry, and the issue must still contain an inline acceptance command. It never touches the real issue or state.
- `python3 tools/check-ad-fetch-transient-class.py` — offline #986 contract check. It asserts `bad pack header` and `fetch-pack: unexpected disconnect` stderr samples remain transient and retryable within the harvest bound.
- `python3 tools/check-orphan-runner-window.py` — offline #961 contract check for an assignment whose remote `runner_alive` remains false. After the window expires while `run_state=running`, the assignment is recycled as `orphaned` and the task is relabelled `lane-ready`; the exact window boundary is not expired.
- `python3 tools/check-ad-assign-rollback.py` — offline #968 contract check for an `assign` returning non-zero while the remote lane remains `busy` or `queued` without a matching local `state.json` entry. It detects and clears only the fake remote reservation; it never connects to SSH or kills a real runner.
- `python3 tools/check-ad-mint-cooldown.py` — offline #1260 contract check for seed-mint burst cooling. A fake clock asserts that a second mint whose interval is less than `MINT_COOLDOWN` is skipped and does not refresh `_last_mint`.
- `python3 tools/check-ad-stale-label-skip.py` — offline #985 contract check. A stale search hit whose authoritative labels still contain both `lane-failed` and `lane-ready` is skipped once; the operator should remove the residual `lane-ready` label before retrying.
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

For low-memory `govpn2`-class hosts, the offline contract uses
`HEALTH_MEM_HARD_MB=256` and `HEALTH_MEM_SOFT_MB=512`. A `health.json` snapshot
below hard writes `auto:host_hard` to `PAUSE-HOST-govpn2` and `auto:mem_hard`
to each lane pause on that host. When a later snapshot recovers to soft or
above, the health loop self-heals by removing those auto pauses together, so
the same host's lanes become dispatchable again; manual pause bodies are
retained. The executable set/clear contract uses fake health snapshots and a
temporary pause directory: `python3 tools/check-health-pause.py`.

During dual-mode cooldown, the production pause set is restricted to
`PAUSE-HOST-ib1`, `PAUSE-HOST-ib2`, `PAUSE-M1`, and `PAUSE-M2`. The offline
check uses a fake pause directory and never changes the real state:

```bash
python3 tools/check-pause-set-cooldown.py
```

The SSH cooldown set/hold contract is checked offline with
`python3 tools/check-health-pause.py`; it uses fake probe output and never
starts the resident health loop or connects over SSH.

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

Lane hosts with ~1 GiB RAM (`govpn`, `govpn2`) should lower the memory ceiling of the
generated lane unit instead of editing an installed unit:

- per lane: `"memory_max": "700M"` in that host's `$OPENSWARM_HOME/lanes.json`
- per host: `{"lane": {"memory_max": "700M"}}` in that host's `$OPENSWARM_HOME/policy.local.json`

`openswarm lane systemd-unit <lane>` then prints `MemoryMax=700M`; without either setting the
default stays `900M`, so higher-memory hosts are unaffected. The recommended values and the
launchd equivalent are documented in [`docs/fleet-govpn-cx5.md`](fleet-govpn-cx5.md).

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

## Manual one-shots

```bash
setsid nohup ~/phanes-ctl/bin/lane-autodispatch >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/bus-worker-loop >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/lane-merge >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/ctl-loop >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/health-loop >/dev/null 2>&1 &
~/phanes-ctl/bin/ib-refresh                  # manual certificate refresh preflight
```

Do not invent systemd units here; a documented start script is enough.
