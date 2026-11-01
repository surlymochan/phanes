# Lesson: ~37-minute autodispatch silence (2026-11-01)

## Symptom
Heartbeat stuck 30–40 minutes while hosts were reachable with `ControlPath=none`.

## Root causes
1. GNU `timeout` did not always kill wedged `ssh` / `ProxyCommand` (`ssh -W gorelay-root`) trees; orphans held ControlMaster.
2. Serial SSH probes: one wedged host blocked the whole refresh/tick.
3. `ThreadPoolExecutor` used as context manager (`wait=True` on exit) froze the tick after a hung future.
4. Heartbeat written only at end of tick → long harvest looked like a dead supervisor.

## Structural fixes
- External wall-clock process-tree killer around ib-refresh and SSH helpers.
- Parallel host probes with per-host caps; isolate CM sockets (`cm-ad` vs refresh).
- Autodispatch: `shutdown(wait=False, cancel_futures=True)`; heartbeat at tick start and mid-harvest.
- ctl-loop: early wake from cert TTL margin; never sleep past expiry window.
- ib-refresh: reuse cert when TTL high; do not force renew every cycle.

## Prevention checklist
- After any SSH/harvest change: watch `autodispatch/heartbeat` for >3 minutes gap.
- Prefer wall-clock kill over trusting nested `timeout` alone.
- Never block a tick on one host; fail that host into backoff and continue.
