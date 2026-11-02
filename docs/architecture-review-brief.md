# Architecture review data brief

Task: [#1777](https://github.com/surlymochan/phanes/issues/1777). This is an evidence packet and a set of questions, not a disposition plan.

Data window: **2026-10-26T19:06:11Z ≤ event < 2026-11-02T19:06:11Z**, UTC. The reported live capture finished at **2026-11-02T19:20:04.194266Z**. Repository: `surlymochan/phanes`. All figures below come from the [machine report](evidence/architecture-review/report-c.json.gz), with per-event/per-PR/per-label evidence URLs. The compressed evidence preserves the complete JSON payload and keeps generated data out of the source diff. The [raw input](evidence/architecture-review/snapshot.json.gz) supports offline replay. The script uses only the Python standard library and read-only `gh` queries.

## Observed results

| Measure | Result | Machine-report field |
| --- | ---: | --- |
| Published verdict records before repost deduplication | 415 | `reviews.raw_total` |
| Logical published decisions | 409 | `reviews.total` |
| APPROVE | 169 | `reviews.verdicts.APPROVE` |
| CHANGES | 240 | `reviews.verdicts.CHANGES` |
| Reposted decisions removed | 6 | `reviews.duplicate_events` |
| PR denominator for round histogram | 865 | `review_rounds.pull_requests` |
| Merges in the window | 707 | `merges.count` |
| Known merge waits / unknown waits | 704 / 3 | `merges.known_wait_count`, `unknown_wait_count` |
| Known wait min / median / p90 / max, seconds | 33.0 / 382.5 / 7345.0 / 71337.0 | `merges.wait_seconds` |

Review-round histogram (recognized published decisions, not internal attempts):

| Rounds per PR | PRs |
| --- | ---: |
| 0 | 645 |
| 1 | 108 |
| 2 | 58 |
| 3 | 36 |
| 4 | 13 |
| 5 | 5 |

A zero means no recognized published decision in this window; it does not establish that no review took place elsewhere. Counts and PR URLs are in `review_rounds.rows`.

| Merge wait bucket | PRs |
| --- | ---: |
| 1d-3d | 0 |
| 1h-6h | 77 |
| 6h-24h | 33 |
| <1h | 594 |
| >=3d | 0 |
| unknown | 3 |

Open-issue label stock at capture time, for issues created before the endpoint (overlapping labels are not summed into a queue total):

| Label | Open issues |
| --- | ---: |
| follow-up | 14 |
| lane-failed | 8 |
| lane-pr | 57 |
| lane-ready | 17 |
| needs-human | 0 |

This stock excludes PRs; `needs-human=0` is not a count of PRs requiring human action. Each membership is inspectable in `issue_labels.issues_by_label`. Failure-label distribution on open `lane-failed` issues is **raw label evidence**, not a diagnosis: `{"lane-failed": 8, "p1": 2}`. Individual cards and their complete captured labels are in `issue_labels.failed_issues`. A priority label is not converted into a runtime cause.

| Explicit review BLOCKERS count | Logical decisions |
| --- | ---: |
| 0 | 1 |
| 1 | 9 |
| 2 | 10 |
| 3+ | 22 |
| unknown | 367 |

The unknown bucket includes older reports without an explicit numeric field; approval is not treated as an inferred zero. Every event retains its verdict, bucket and evidence URL in `reviews.events`.

## Measurement definitions

- Scope: public GitHub records in `surlymochan/phanes`; every PR with a relevant published review, creation or merge in the fixed window. This is a repository report, not a claim that every PR was produced by a fleet lane.
- Window: half-open UTC interval; review submission/publication and merge timestamps determine inclusion. PRs created earlier can contribute a review or merge in the window.
- Verdicts: formal GitHub `APPROVED` / `CHANGES_REQUESTED` decisions plus explicitly marked lane-review comments with an anchored `VERDICT: APPROVE` or `VERDICT: CHANGES`. Ordinary discussion, review requests and follow-up comments are not verdicts.
- Rounds: published logical decision events per PR. Reposts with the same PR/head/run/verdict count once; independent runs or heads count separately. Raw publication and duplicate evidence remain visible. These are not unpublished dispatcher attempts, which require unavailable control-plane state.
- Merge wait: elapsed time from PR creation to merge, in seconds. It is not queue time after green CI or approval. The report includes per-PR timestamps/URLs, fixed duration buckets and nearest-rank p90. Merges with reversed source timestamps remain counted, but their waits are null and excluded from duration statistics; their raw timestamps and URLs remain visible.
- Open issue labels: current capture-time stock, not reconstructed historical label state at the window end. Labels can overlap. Old open issues remain included; issues created after the end are outside this capture's fixed PR/issue cohort. Capture timestamps and the two-run comparison identify the actual observation times.
- Failure labels: observed labels on captured open `lane-failed` issues, not inferred runtime causes. `BLOCKERS` buckets are `0`, `1`, `2`, `3+`, `unknown` for logical review events. Absent, conflicting or malformed values stay unknown; narrative findings are not converted into invented counts.
- GitHub records and labels can later be edited or deleted. The archived snapshot supports the repeatable calculation; independent live captures test stability at the two stated observation times, not permanent historical immutability.
- Pagination and collection errors must fail visibly. Unavailable runtime data is marked 不可得, not reported as zero.

## Control-plane evidence boundary

These are repository-defined paths and contracts, not live lane measurements. Source links are pinned to the implementation base, `9b48c227b01ac9913230919f5e4fff62ff3faf91`.

| Item | Repository evidence | What is observable here |
| --- | --- | --- |
| Core runtime root | [openswarm:82–94](https://github.com/surlymochan/phanes/blob/9b48c227b01ac9913230919f5e4fff62ff3faf91/openswarm/bin/openswarm#L82) selects `OPENSWARM_HOME`, legacy fallback, or `~/.claude/openswarm`. | Path selection is inspectable; host runtime contents are 不可得 through GitHub. |
| Host registry | [fleet_config](https://github.com/surlymochan/phanes/blob/9b48c227b01ac9913230919f5e4fff62ff3faf91/openswarm/bin/openswarm#L10321) reads `DATA/fleet.json`. | Actual registered hosts and reachability are 不可得; a local-host default is not a measured host count. |
| Lane registry | [lane_config](https://github.com/surlymochan/phanes/blob/9b48c227b01ac9913230919f5e4fff62ff3faf91/openswarm/bin/openswarm#L7709) reads `OPENSWARM_LANES_FILE` or `DATA/lanes.json`. | Actual lane registry is 不可得. The tracked [fleet-inventory fixture](../openswarm/tests/fixtures/fleet-inventory.json) is test data, not current registration. |
| Candidate entry point | [openswarm-fleet](https://github.com/surlymochan/phanes/blob/9b48c227b01ac9913230919f5e4fff62ff3faf91/openswarm/bin/openswarm-fleet#L5) selects the candidate runtime home and guard config. | Entry-point behavior is inspectable; configured guard values and active pauses are 不可得. |
| Dispatcher state | [engine defaults](https://github.com/surlymochan/phanes/blob/9b48c227b01ac9913230919f5e4fff62ff3faf91/openswarm/lib/dispatch/engine.py#L123) and [lane-path resolution](https://github.com/surlymochan/phanes/blob/9b48c227b01ac9913230919f5e4fff62ff3faf91/openswarm/lib/dispatch/engine.py#L267) select a separate state directory and lane registry. | Repository defaults do not prove which deployment config is running. |
| Raw review state | [load_reviews](https://github.com/surlymochan/phanes/blob/9b48c227b01ac9913230919f5e4fff62ff3faf91/openswarm/lib/dispatch/engine.py#L1451) reads `state_dir/reviews.json`. | Raw review attempts, unpublished results and retry state are 不可得. Published GitHub reports are the measured proxy. |
| Legacy Phanes dispatcher | [autodispatch configuration](https://github.com/surlymochan/phanes/blob/9b48c227b01ac9913230919f5e4fff62ff3faf91/tools/lane-autodispatch/autodispatch.py#L10) reads `~/phanes-ctl/autodispatch/lanes.json` and state. | Deployed entry point and live state are 不可得; repository source alone does not establish which dispatcher runs. |
| Effective policy and admission | [local policy override](https://github.com/surlymochan/phanes/blob/9b48c227b01ac9913230919f5e4fff62ff3faf91/openswarm/bin/openswarm#L131) and [fleet-capacity inputs](https://github.com/surlymochan/phanes/blob/9b48c227b01ac9913230919f5e4fff62ff3faf91/openswarm/bin/fleet-capacity#L13). | Runtime policy overrides, reservations and occupancy are 不可得. Checked-in defaults are not measured effective policy. |
| Expected topology | [fleet-topology-expected](fleet-topology-expected.md) and [handoff note](../brain/autopilot.md). | Historical expectations and handoff notes are available; they are not live capacity observations. No current capacity total is inferred from them. |

No SSH, host filesystem, credentials, private runtime state, deployment or settings access was attempted. No disposition decision follows from this brief; questions below are inputs for the user's review.

## Evidence-backed structural questions

1. **Which timestamp is authoritative for merge-wait measurement?** The API returns creation after merge for [PR #1081](https://github.com/surlymochan/phanes/pull/1081), [#1082](https://github.com/surlymochan/phanes/pull/1082) and [#1084](https://github.com/surlymochan/phanes/pull/1084). The report's `merges.unknown_wait_rows` retains all raw values, and a [separate REST verification](evidence/architecture-review/invalid-time-verification.json) confirms the discrepancy. These records stay in merge counts, but their wait durations are unknown. No clock or process cause is inferred.
2. **How should duplicate review publications be distinguished from new rounds?** `reviews.raw_total=415` and `reviews.total=409` differ by `reviews.duplicate_events=6`. For [PR #1751](https://github.com/surlymochan/phanes/pull/1751), [one publication](https://github.com/surlymochan/phanes/pull/1751#issuecomment-6583864706) and [its repost](https://github.com/surlymochan/phanes/pull/1751#issuecomment-6583873005) contain the same head, run and verdict. The metric deduplicates that logical event, while later independent runs remain distinct. This is publication evidence, not proof of duplicate execution.
3. **What failure-severity evidence can be compared across review formats?** `reviews.blockers.unknown=367` records no usable explicit numeric `BLOCKERS` value. For example, [PR #766's CHANGES report](https://github.com/surlymochan/phanes/pull/766#issuecomment-6559715604) has narrative findings but lacks that field. The script does not infer a blocker count from prose or verdict. The raw review state needed to resolve unpublished attempts is 不可得.

## Reproduce and validate

```sh
python3 tools/architecture_review_metrics.py --repo surlymochan/phanes --end 2026-11-02T19:06:11Z --snapshot-out /tmp/architecture-snapshot.json > /tmp/architecture-report.json
python3 tools/architecture_review_metrics.py --snapshot-in docs/evidence/architecture-review/snapshot.json.gz
python3 -m unittest discover -s tools/tests -p 'test_architecture_review_metrics.py' -v
```

The first command makes new read-only GitHub queries; current issue labels may legitimately change after these observations. The second replays the archived window without network access. The third tests time boundaries, formal and lane verdicts, reposts, malformed/incomplete input, evidence links, blocker buckets and source timestamp anomalies. Collection failures remain nonzero errors; unavailable source values are explicit unknowns.

## Acceptance evidence

The live commands use the same fixed endpoint and exit 0. Runs B and C finished at `2026-11-02T19:17:09.212207Z` and `2026-11-02T19:20:04.194266Z`. Their complete metric sections (`reviews`, `review_rounds`, `merges`, `issue_labels`) match exactly, including per-record evidence lists. [Comparison and snapshot digest](evidence/architecture-review/repeatability.json); [run B](evidence/architecture-review/report-b.json.gz); [run C](evidence/architecture-review/report-c.json.gz). The capture timestamp itself is expected to differ.

The earlier [run A](evidence/architecture-review/report-a.json.gz) had the same historical review and merge metrics, but its live stock changed before B: [#1716](https://github.com/surlymochan/phanes/issues/1716) moved from the captured ready set to the PR set. Ready stock was 18 then 17, and PR stock was 56 then 57. This observation is retained rather than presented as a deterministic historical label stock. Two consecutive later captures agreed. Future live queue changes can change capture-time stocks; replaying the archived snapshot gives the exact recorded counts.

All 14 focused CLI tests passed, including compressed replay and truncated-input rejection. An offline replay of the archived source matches report C byte-for-byte. The brief contains three evidence-backed questions and no disposition decision. Runtime registries and raw review state remain explicitly unavailable; the three invalid waits are source-quality observations, not a failed collection or invented zero.
