# Resource threshold policy

The optional top-level `resources` object controls resource pause and cleanup
thresholds. Missing values are filled from these defaults. Unknown keys are
preserved and reported as warnings.

| Path | Default | Meaning |
| --- | ---: | --- |
| `resources.inode.pause_pct` | 95 | Pause work when inode use reaches this percentage. |
| `resources.inode.resume_pct` | 92 | Resume paused work below this percentage. |
| `resources.inode.cleanup_pct` | 90 | Suggest cleanup for unpaused work at or above this percentage. |
| `resources.disk.pause_pct` | 95 | Pause work when disk use reaches this percentage. |
| `resources.disk.resume_pct` | 92 | Resume paused work below this percentage. |
| `resources.disk.cleanup_pct` | 90 | Suggest cleanup for unpaused work at or above this percentage. |
| `resources.cgroup.high_ratio` | 0.95 | Cgroup high-pressure ratio threshold. |
| `resources.cgroup.high_events_delta` | 1000 | Positive event-count delta threshold. |
| `resources.cgroup.shmem_ratio` | 0.5 | Cgroup shared-memory ratio threshold. |
| `resources.health.disk_pause_pct` | 95 | Pause a fleet host when its disk use reaches this percentage. |
| `resources.health.disk_resume_pct` | 92 | Resume a paused fleet host below this disk percentage. |
| `resources.health.inode_pause_pct` | 95 | Pause a fleet host when its inode use reaches this percentage. |
| `resources.health.inode_resume_pct` | 92 | Resume a paused fleet host below this inode percentage. |
| `resources.health.mem_pause_mb` | 512 | Pause a fleet host when its free memory falls to this many MB. |
| `resources.health.mem_resume_mb` | 640 | Resume a paused fleet host above this many MB of free memory. |
| `resources.health.ssh_fail_pause_streak` | 3 | Pause a fleet host after this many consecutive SSH failures. |

The `resources.health` group holds the watermarks of `openswarm fleet health`: the command pauses a host
over the pause watermark and releases it under the resume watermark (`--apply` writes the host-level pause).
A threshold the policy spells with a value that is not a number is ignored and the default stays in place.

For inode and disk, `cleanup_pct ≤ resume_pct < pause_pct`. Decisions use
hysteresis so a paused workload does not immediately resume at the pause point:

```text
96%: pause (threshold is 95%)
93%: stay paused (resume threshold is 92%)
91%: resume
```

Run `python3 openswarm/lib/resources/policy.py [policy.json] [--json]` to merge
and validate a policy. Invalid configuration exits with status 2.
