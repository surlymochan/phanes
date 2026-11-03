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

For inode and disk, `cleanup_pct ≤ resume_pct < pause_pct`. Decisions use
hysteresis so a paused workload does not immediately resume at the pause point:

```text
96%: pause (threshold is 95%)
93%: stay paused (resume threshold is 92%)
91%: resume
```

Run `python3 openswarm/lib/resources/policy.py [policy.json] [--json]` to merge
and validate a policy. Invalid configuration exits with status 2.

## Dispatcher guard

The dispatch engine keeps the same watermarks in its own dispatch config
(`resources.inode.pause_pct`, `resources.inode.resume_pct`,
`resources.inode.cleanup_pct`, with the defaults above). A host at or above
the pause watermark stops dispatching to its lanes without writing a marker
file, and it resumes at or below the resume watermark. The cleanup hook and the
dispatch ledger are described in [RESOURCES.md](RESOURCES.md).
