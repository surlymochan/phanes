# CX5 on govpn: memory and smoke-task notes

## Baseline

- Host `govpn` has **2 GiB RAM**; the fleet status notes CX5 is memory-tight and
  should be watched for swap (`brain/dual-mode-status.md:41`).
- The generated OpenSwarm lane systemd unit sets `MemoryMax=900M`
  (`openswarm/bin/openswarm:8653`). Treat **900M as the recommended per-lane
  ceiling** on this host; it leaves roughly 1.1 GiB of nominal RAM for the OS,
  other services, and filesystem cache. The cap is a ceiling, not a measured
  steady-state footprint.
- No verified swap size or current swap-use measurement is recorded in this
  worktree. Before and during a smoke task, capture `free -h` and
  `swapon --show`; do not infer swap headroom from the RAM total.

## Smoke-task operating limit

Run one small task on CX5 and observe its completion, peak memory, and swap
before starting another task there. Do not run a second task in parallel:
CX5's 900M memory ceiling already consumes a substantial share of a 2 GiB host,
and concurrent processes add demand outside that lane's cap (including system
services and cache). Under memory pressure, that can push the host into swap
and make both tasks unstable. Keep CX5 at one active task until measurements
show reliable headroom.

This note records the configuration and qualitative baseline available in the
repository; it does not claim a live memory or swap measurement.
