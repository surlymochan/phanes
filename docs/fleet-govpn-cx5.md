# CX5 on govpn: memory and smoke-task notes

## Baseline

- Host `govpn` has **2 GiB RAM**; the fleet status notes CX5 is memory-tight and
  should be watched for swap (`brain/dual-mode-status.md:41`).
- The generated OpenSwarm lane systemd unit sets `MemoryMax=900M` by default
  (`openswarm/bin/openswarm:8830`, default constant `LANE_DEFAULT_MEMORY_MAX`
  at `openswarm/bin/openswarm:6831`). That default is the **high profile** and
  must stay untouched for normal hosts. On a low-memory host (~1 GiB class,
  such as `govpn`/`govpn2`) lower it instead of editing the generated unit.
- No verified swap size or current swap-use measurement is recorded in this
  worktree. Before and during a smoke task, capture `free -h` and
  `swapon --show`; do not infer swap headroom from the RAM total.

## Per-lane memory ceiling (low-memory hosts)

`openswarm lane systemd-unit` writes the ceiling of the lane it is asked about,
so a low-memory host lowers it in its own configuration instead of patching the
unit by hand. Precedence: lane entry → host policy → built-in default `900M`
(`openswarm/bin/openswarm:6847`).

Per lane, in `$OPENSWARM_HOME/lanes.json`:

```json
{"lanes":[
  {"name":"CX5","backend":"codex","model":"gpt-6-luna","effort":"high","repo":"/abs/repo","memory_max":"700M"}
]}
```

Per host, in `$OPENSWARM_HOME/policy.local.json` (keeps every lane of that host
low, and the repo policy keeps the high default for everyone else):

```json
{"lane":{"memory_max":"700M"}}
```

The value is a systemd size: an integer with an optional `K`, `M`, `G` or `T`
suffix (`900M`, `2G`, `1048576`). Anything else is rejected when the lane
config is read (`openswarm/bin/openswarm:6957`), so a typo fails loudly instead
of silently producing an unlimited unit.

### Recommended values

| Host class | RAM | Recommended `MemoryMax` | Note |
|---|---|---|---|
| default / high profile | ≥4 GiB | `900M` (built-in default) | unchanged behaviour |
| low-memory (`govpn`, `govpn2`) | ~1–2 GiB | `700M` | leaves room for the OS, cache and one task |
| very tight (~1 GiB, one lane only) | ~1 GiB | `512M` | only with `--verify` omitted |

`700M` is the recommended ceiling for the ~1 GiB `govpn`/`govpn2` lanes: it
keeps the lane well below the host total so the OS, other services and
filesystem cache keep their share. The cap is a ceiling, not a measured
steady-state footprint — measure before and after a smoke task.

### launchd equivalent

`openswarm lane launchd-plist` has no `MemoryMax=`; when a ceiling is
configured it writes the closest launchd equivalent, `SoftResourceLimits` and
`HardResourceLimits` with `Memory` in bytes (`openswarm/bin/openswarm:8808`).
`700M` becomes `734003200`. Without a configured ceiling the plist is
unchanged and stays unlimited, because the launchd limit caps *address space*
(`RLIMIT_AS`), not resident memory, and would kill a healthy lane. Review the
plist before installing it.

## Smoke-task operating limit

Run one small task on CX5 and observe its completion, peak memory, and swap
before starting another task there. Do not run a second task in parallel:
CX5's memory ceiling already consumes a substantial share of a 2 GiB host,
and concurrent processes add demand outside that lane's cap (including system
services and cache). Under memory pressure, that can push the host into swap
and make both tasks unstable. Keep CX5 at one active task until measurements
show reliable headroom.

This note records the configuration and qualitative baseline available in the
repository; it does not claim a live memory or swap measurement.

## Unprivileged observation and acceptance

The following commands can be run without root before and during the smoke
task. They select the first generated OpenSwarm lane unit when checking its
cap:

```sh
UNIT="$(systemctl list-units --type=service --all 'openswarm*' --no-legend | awk 'NR==1 {print $1}')"
test -n "$UNIT" && systemctl show "$UNIT" -p MemoryMax --value
free -h
swapon --show
```

The ceiling that `openswarm lane systemd-unit` would print can be checked
without touching the remote host at all:

```sh
openswarm lane systemd-unit CX5 | grep -E '^MemoryMax='
```

Documentation acceptance also requires no root access:

```sh
test -f docs/fleet-govpn-cx5.md && rg -n 'MemoryMax|swap|CX5' docs/fleet-govpn-cx5.md
```
