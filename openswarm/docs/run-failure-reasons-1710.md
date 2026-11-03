# #1710 run failure reason backfill

On 2026-11-02, four CX9 failed runs were reported with an empty (`None`) failure
reason. The original run records do not contain enough evidence to distinguish
session spawn, fetch, timeout, OOM, execution, budget, or rate-limit failures.
Those historical records therefore remain classified as **原因缺失**; no cause
has been inferred or written back. New failed run records store `failure_reason`,
and unknown classifications retain the available raw error tail.

## Which endings carry a reason

Every harvest ending of a run that did **not** end cleanly (`failed`, `partial`,
`stopped`, `budget_exceeded`) writes one reason of the enumeration plus the raw
error into the brain log ending line (`[failure_reason=...; error=...]`) and into
the run record (`failure_reason` / `failure_error` in `result.json`):

| ending | reason source |
| --- | --- |
| ended without a commit | the run (`error_class` + raw error) |
| ended with a commit (PR opened, rebase, duplicate, push failed) | the run (`error_class` + raw error) |
| harvest gave up on the fetch | `fetch-failed` + the git error |
| rebase produced nothing to push | the run (`error_class` + raw error) and the rebase cause |
| watchdog abort | `timeout` + the watchdog reason |
| VM pause / model unavailable | the run (`unknown` + the raw error) |
| PR could not be created | `exec-error` + the `gh` error (`harvest_failure_reason`) |

An ending line and the run record always carry **both** the raw error of the run
and the cause of the ending (for example `child exited with code 1 ; rebase
produced nothing to push: not based on main abc12345`): the cause of the
harvest never replaces the error that explains the run.

A run that ended cleanly (`done`) has no failure reason, so nothing is written
for it and it is not counted as a failure.

## Health histogram

`openswarm fleet-health` reports `recent_failure_reasons`: a small table with the
recent failure causes. A run contributes one count per **distinct** cause: its
own reason when the run failed, plus the harvest reason when the harvest broke
the ending — a run that failed and whose harvest then failed contributes both,
so neither stays invisible in the distribution. A cause outside the enumeration
is counted as `unknown`, never dropped and never `None`.

The run record is replaced atomically (write to `result.json.tmp`, then
`os.replace`), so an interrupted harvest write cannot truncate a record.
