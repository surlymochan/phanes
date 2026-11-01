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
     (or background one-shot: `setsid nohup ~/phanes-ctl/bin/ctl-loop >/dev/null 2>&1 &`; repo CLI: `python3 tools/ib-refresh.py --fleet-config ~/phanes-ctl/fleet.json --renew-command ~/phanes-ctl/bin/renew-cert --status-file ~/phanes-ctl/ib-refresh.status`)
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
     is the non-negative count of stale-search-index entries stripped by the
     current autodispatch process.
   - **Task upload contract (Refs #920)**: dispatch uploads the generated task
     over the existing SSH connection with stdin (`ssh ... 'cat > /tmp/task-*.md'`).
     A standalone `scp` upload is forbidden because it is unreliable through
     the instabox ProxyJump path. Check with `python3 tools/check-ad-task-upload.py`.
   - **SSH option quoting contract (Refs #972)**: status probes and harvest both
     build their SSH command from the same `git_ssh_command`/`shlex` path, so a
     `ProxyCommand` or `ProxyJump` value containing spaces remains one argv item.
     Check offline with `python3 tools/check-ssh-opts-quote.py`.

3. **`lane-merge`** (merge Linux-green `fleet/*` PRs):
   - **Startup command**: `~/phanes-ctl/bin/lane-merge`  
     (or background one-shot: `setsid nohup ~/phanes-ctl/bin/lane-merge >/dev/null 2>&1 &`; script: `merge-loop.sh`)
   - **Heartbeat path**: `~/phanes-ctl/autodispatch/merge-heartbeat`

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
config, so three reachable hosts can keep `ok=1` while two hosts cool down.
Hosts absent from the configured topology are errors and are excluded from
`hosts_ok`; exercise this invariant with
`python3 tools/check-fleet-unknown-host.py`.
Exercise this accounting with the fake-input check
`python3 tools/check-ib-cooldown-status.py`.
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
- `python3 tools/check-codex-queue-heartbeat.py` — offline #988 contract check for a Codex status that stays `queued` with a provider rate-limit `queue_reason`. If both `queued_at` and `heartbeat_at` are older than **30 minutes**, the item must be reclaimed; a fresh heartbeat or another queue reason is preserved.
- `python3 tools/check-ctl-loop-reap.py` — offline #951 contract check for ctl-loop heartbeat expiry, pid disappearance, child restart, and duplicate-instance prevention.
- `python3 tools/check-ctl-hb-refresh.py` — offline #1007 contract check: static scan of ctl-loop ensuring the ib-refresh wall-clock wait loop writes ctl-loop.heartbeat using date and enforces REFRESH_CAP.
- `python3 tools/check-ready-refill-contract.py` — offline ready/refill contract check. It uses a fake directory to assert `filed N issue(s)` for a local card and `WARNING queue low` when local and repo backlog sources are exhausted.
- `python3 tools/check-ad-ready-target.py` — offline #1061 AD ready-queue contract. When `ready < LOW`, the refill `need` is `max(TARGET - ready, ...)` (and reaches `TARGET`); `is_thin_card` rejects cards without a substantive body and `验收：` line. It uses only a local fixture and never calls `gh`.
- `python3 tools/check-ad-task-upload.py` — offline #920 contract check. It captures fake task content, asserts SSH stdin upload via `cat > /tmp/task-*.md`, and rejects a bare `scp` upload.
- `python3 tools/check-ad-fetch-transient-class.py` — offline #986 contract check. It asserts `bad pack header` and `fetch-pack: unexpected disconnect` stderr samples remain transient and retryable within the harvest bound.
- `python3 tools/check-orphan-runner-window.py` — offline #961 contract check for an assignment whose remote `runner_alive` remains false. After the window expires while `run_state=running`, the assignment is recycled as `orphaned` and the task is relabelled `lane-ready`; the exact window boundary is not expired.
- `python3 tools/check-ad-assign-rollback.py` — offline #968 contract check for an `assign` returning non-zero while the remote lane remains `busy` or `queued` without a matching local `state.json` entry. It detects and clears only the fake remote reservation; it never connects to SSH or kills a real runner.
- `python3 tools/check-ad-mint-cooldown.py` — offline #1041 contract check for seed-mint burst cooling. A fake clock asserts that a second mint whose interval is less than `MINT_COOLDOWN` is skipped and does not refresh `_last_mint`.

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

### AD ready queue target refill (Refs #1061)

When `lane-autodispatch` ready queue drops below `LOW` (`AD_LOW`, default 10), the refill
loop must request enough candidate cards to refill up to `TARGET` (`TARGET = 20`) rather than
topping up one card at a time. The refill count calculates `need = max(TARGET - ready, starved, 0)`.

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

Manual `touch PAUSE-*` without `auto:` is never cleared by health-loop. Soft alerts
(`disk_soft`/`mem_soft`) stay in `health.alert` with `level=soft` and never create a
`PAUSE-HOST-*` file. The disk boundary contract is checked offline with
`python3 tools/check-health-disk-soft.py`.

During dual-mode cooldown, the production pause set is restricted to
`PAUSE-HOST-ib1`, `PAUSE-HOST-ib2`, `PAUSE-M1`, and `PAUSE-M2`. The offline
check uses a fake pause directory and never changes the real state:

```bash
python3 tools/check-pause-set-cooldown.py
```

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

### ctl-loop ib-refresh heartbeat maintenance (Refs #1007)

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
