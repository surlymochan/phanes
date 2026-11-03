# #1703 host clock skew in the health probe

On 2026-11-02 the fleet logs of the control plane and the logs of the hosts
disagreed about the order of the same events: the brain log said 20:09, the run
log of the host said 20:15, and nobody could say which came first. A host whose
clock is off also makes every age that is read from its own stamps wrong by the
skew -- the age of the oldest capacity reservation, the duration of a run, the
freshness of a quota signal, the staleness of an `in-progress` label.

The health probe now measures the clock of every host against the clock of the
control plane, and the measured skew is published for everything that has to
decide something by time.

## The measurement

`openswarm fleet health` reads the clock of the host with a lightweight remote
command (`date +%s.%N`) and compares it with the clock of the control plane:

- the control plane is sampled **right before and right after** the remote
  reading, and the middle of the two readings is the control-plane time of the
  moment the host answered (`clock_probe_s` keeps the duration of the probe).
  A slow host or a slow link therefore moves the reading, not the verdict: the
  hosts are probed one after the other, and each host is compared with the
  control-plane time of its own probe.
- a skew beyond **60 seconds** (`CLOCK_SKEW_THRESHOLD_S`) makes the clock of
  the host untrustworthy: the host is reported as `soft`, the report is not
  `ok`, and the warning names the direction, the seconds and the repair:

  ```
  worker-a  soft       worker-a: healthy
    metrics: disk_pct=70.0 inode_pct=10.0 mem_avail_mb=5000 ssh_fail_streak=0 clock_offset_s=300.0
    clock: clock fast by 300.0s; synchronize with NTP (timedatectl set-ntp true)
  ```

## What is remembered and what is written

The measured clock of every host is remembered in `fleet-health.json`, next to
its SSH failure streak: `clock_offset_s` and `clock_unreliable`.  A host that
could not be reached has no clock reading, so nothing is remembered about it:
the last known state stays untouched instead of being replaced by a guess.

A skew is written into the brain log (the `brain_log` of the dispatch
configuration) when it appears **for the first time** or when it **grew**.  A
steady skew is not written again, so the log does not fill up with the same
line; a host whose clock came back into range reports nothing, and its next
skew counts as a first one again.

```
- 21:07 UTC+8 fleet-health: worker-a clock fast by 300.0s (> 60s); synchronize with NTP (timedatectl set-ntp true)
```

## What trusts a clock

A time-sensitive computation that reads a stamp of a host may only be decided
when the clock of that host is trusted:

| computation | what happens with a skewed clock |
| --- | --- |
| the age of the oldest capacity reservation (`fleet_status.capacity_data`) | the age is reported as not reliable (`oldest_age_reliable`), the page names the hosts whose clock is off |
| the quota signal of a run (`engine.quota_signal_fresh`) | the signal is not judged by the stamp of the host, so nothing is paused on it |
| the timestamps of a run (`engine.run_clock_state`) | the run record is marked `clock_unreliable`: the start, the end and the duration of the run cannot be compared with the log of the control plane |
| the staleness of an `in-progress` label (`tools/check-ghost-in-progress.py`) | no label is judged stale: the tool says that the staleness cannot be proven and treats the label as in-flight |

The repair is on the host itself: `timedatectl set-ntp true` (or any NTP
client) brings the clock back, and the next probe reports the host as `ok`
again.
