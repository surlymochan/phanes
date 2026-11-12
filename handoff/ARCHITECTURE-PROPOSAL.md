# Architecture proposal evolution log

## e1885 decision

This record is a proposal decision; ARCHITECTURE.md remains the architecture specification.
The experiment replayed 10 merged production L2 cards (p01 p02 p03 p05 p06 p07 p08 p09 p10 p11),
each with a frozen hidden acceptance suite kept on the control plane only. The control arm ran
card, author, reviewer; the treatment arm ran card, Spec (gpt-6.1-sol high), Spec check
(gpt-6.1-terra high), author writing failing tests from the scenarios first, reviewer checking
against the Spec. Both arms used the same base commit, the same author model (MiniMax-M3.1-Flash),
the same reviewer (gpt-6.1-terra high) and only the first round was scored.

It ran in three rounds, each with criteria frozen in issue 1885 before any run. Round 1 (e1885)
scored a rejected Spec as a failure with no PR. Round 2 (e1885b) let a rejected Spec be refined in
non-strict mode: the Spec role resolved ambiguity with its own assumptions, at most 2 rounds, and a
Spec still rejected was released as FORCED. Round 3 (e1885c, planner comment of 2026-11-12) tested
strict mode: a rejected Spec makes the Spec role ask at most 5 concrete clarification questions about
missing information (none when the rejection was the Spec adding requirements of its own), the
questions of all rejected cards go to Boss acting as the human in one batch, the Spec is refined once
with the answers and checked once more, and a Spec still rejected ends as NEEDS_HUMAN: no build, not
merged, not counted as a false approval. Boss answered only from the issue body at dispatch, issue
comments before dispatch, the epic description and code paths at the base commit. Cost is at most
5.0 times control and 10 USD per PR; wall time excludes the wait for answers. The fields below are
the round 3 result, which is the final decision; rounds 1 and 2 are summarised after them.

Frozen manifest digest: sha256:e584877fa14ff059ebf9b22d4bfe5b5ad0f5dd9c44aeb17c7440ea606909ada5
Report digest: sha256:74755e1a756f20e12ddbdbd12352e3a74a1b1f6ff957f4f78611787cd6562a4d
Paired sample count: 10
Discordant results: merge-valid (hidden suite pass and reviewer APPROVE) treatment-only 0 pairs, control-only 1 pair (p03, where the treatment change passed the hidden suite but the reviewer asked for changes), net -1; merge-valid totals control 3, treatment 2 (p05 p07); hidden suite pass totals control 4, treatment 5; false approvals control 2, treatment 1; NEEDS_HUMAN 2 cards (p02 p06), the frozen criteria require noting an excessively high return rate if more than 3 cards are NEEDS_HUMAN.
Extra runs: treatment used 63 model runs against 30 for control, 33 extra, counting the round 1 Spec and checks it reused; strict mode asked 10 questions in total (p02 5, p03 3, p06 2, and 0 for p05 p08 p10 whose rejection was the Spec overreaching), 4 of the 6 cards passed the check after one answered refinement and 2 ended as NEEDS_HUMAN.
Queue impact: in round 2, each refinement or check waited about 0.4 seconds in the queue, so the extra time was serial model time; median per-card wall time was 720 seconds for control and 1806 seconds for treatment, 2.51 times, above the frozen 2.0 times limit; the 50 seconds Boss took to answer the single batch is excluded and reported here only.
Quota impact: at frozen public list prices total cost was 1.87 USD for control and 4.66 USD for treatment; cost per merge-valid PR was 0.62 USD for control and 2.33 USD for treatment, 3.7 times, within the limit of 5.0 times and 10 USD; the most expensive refined card was p08 at 0.66 USD; a voided recheck (0.12 USD) is reported apart from the criteria.
INFRA counted as PASS: no
Decision: abandon
Rationale: Of the six frozen round 3 criteria two failed: net discordant merge-valid pairs was -1 against a required +2, and median wall time was 2.51 times control against a 2.0 times limit; hidden suite passes (5 vs 4), false approvals (1 vs 2), cost per PR (3.7 times) and valid pairs (10, none excluded) passed. Strict mode removed the round 2 failure mode: no answered card was wrongly approved, false approvals fell below control, and the reviewer rejected every refined change that failed the hidden suite. It did not add merge-valid PRs: answers resolved what the Spec role asked, but p08 and p10 were rejected for overreach, asked nothing, and still failed on interface details nobody clarified (field and function names), while p03 passed the hidden suite but was sent back by the reviewer. With 2 of 6 cards returned to a human and wall time 2.5 times control, the role pipeline stays implemented but disabled by default.

### Round 2 (e1885b, non-strict refinement)

Manifest v3.1 digest sha256:dabe5bb724bff90e03352e513e776dc7b0a82de5e201fd43cdada7a9edbd4e65, report digest sha256:325dc597df832e3e6efdc873e3a4492d99c80d27fd837d19e27cf653e32074f3. All 6 rejected cards used both refinement rounds; 2 were accepted after round 2 (p02 p10) and 4 released as FORCED (p03 p05 p06 p08). Merge-valid 3 vs 3 (p03 p05 p07), net 0; hidden suite passes control 4, treatment 5; false approvals control 2, treatment 3 (p02 added); cost per merge-valid PR 0.62 USD against 1.88 USD, 3.0 times; median wall time 720 seconds against 1913 seconds, 2.66 times. Of the 4 refined cards that failed the hidden suite, 3 failed on behaviour their own assumption list had decided, and 1 partly so; the reviewer checked against the same assumed Spec and could not catch it. Decision: abandon, which led to the strict mode test in round 3.

### Round 1 (e1885, rejected Spec scored as failure)

Manifest v2 digest sha256:f0a930fdab98f5ede384f9a4a7f14ba2a928e27f40d8b017ece61238de7f2423, report digest sha256:ed4d082f99e88fe399c8fbf914fd1ddb01acf887fa152614652c7099025fadc6. The Spec check rejected 6 of 10 underspecified cards (p02 p03 p05 p06 p08 p10), which the round 1 rule scored as no PR. Merge-valid control 3 of 10, treatment 1 of 10, net discordant -2; hidden suite passes control 4, treatment 3; false approvals control 2, treatment 1; cost per merge-valid PR 0.62 USD against 2.70 USD, 4.3 times, above the 2.0 times limit then in force; median wall time 720 seconds against 1198 seconds, 1.66 times. Decision: abandon, with the rejection rule identified as the main cause, which led to round 2.

| Measure | Control | Round 1 treatment | Round 2 treatment | Round 3 treatment |
|---|---|---|---|---|
| Merge-valid | 3 of 10 | 1 of 10 | 3 of 10 | 2 of 10 |
| Hidden suite passes | 4 | 3 | 5 | 5 |
| False approvals | 2 | 1 | 3 | 1 |
| Returned to a human | | | | 2 (p02 p06) |
| Cost per merge-valid PR | 0.62 USD | 2.70 USD | 1.88 USD | 2.33 USD |
| Median wall time | 720 s | 1198 s | 1913 s | 1806 s |
| Decision | | abandon | abandon | abandon |

### Process notes

- Round 3 reused all 10 control results and the treatment results of the 4 cards accepted in round 1 (p01 p07 p09 p11); configuration, card set, base and price list are unchanged and recorded in the frozen manifest. The 6 rejected cards reused their round 1 Spec and first rejection.
- Round 3 protocol fix: under manifest v4.0 the recheck saw only the card and the Spec, so p02 and p03 were rejected for details that came from the answers ("cannot be verified from the card alone"). The line was stopped, the recheck input was extended with the questions and answers as a clarification section of the card, the two rechecks were voided (0.12 USD, reported apart), the questions and answered Specs were kept since their inputs did not change, and the manifest was refrozen as v4.1. Under v4.1 p02 was still rejected, for an answer the Spec did not carry through, and p03 passed.
- Round 3 known bias: Boss knew the round 2 conclusion (self-made assumptions caused failures) but not any card's tests. The p02 epic description, read in its current form, also carried review notes written after dispatch; Boss stated it did not use them, though one field name in its answer matches them, so p02 carries this contamination risk. p02 ended as NEEDS_HUMAN, so it did not affect any merge-valid count.
- Round 2 reused all 10 control results and the treatment results of the 4 cards accepted in round 1 (p01 p07 p09 p11); same base, same configuration, and the control arm is unaffected by the rule change. The 6 rejected cards reused their round 1 Spec and first check, then ran refinement, check, author, scoring and review. Their cost adds the round 1 spend to the new runs; their wall time adds the round 1 segment to the new segment and excludes the gap between them.
- Round 2 pilot p03 was run twice. The first attempt was voided because the new driver read the review result as a mapping while the reviewer returns a string; after the fix the manifest was refrozen (v3.0 archived) and p03 rerun from scratch. One launch before that stopped on a missing environment setting before any run started.
- Round 1: pilot p03 was run twice (the Spec run needed a committed git snapshot); two mechanism faults were fixed before scoring (the Spec check could not see the public baseline, and queue time counted against the driver wait), with all 10 Spec checks redone under manifest v2; control runs interrupted while fixing the driver produced no scored output and were restarted; p06 control had one infrastructure retry, within the frozen rule.
- Round 2 kept ssh stderr in run artifacts and recorded openswarm queue seconds per run, closing the two round 1 driver follow-ups. No run was INFRA in round 2 and no cost alert fired.
- Prices are public list prices recorded in the frozen manifest and not verified against invoices.

### Follow-ups

- Strict mode was tested in round 3; no card with answered clarification questions was wrongly approved, but throughput did not improve. Cards rejected for Spec overreach should still be asked about interfaces the hidden behaviour depends on (names, fields, signatures); round 3 shows that is where p08 and p10 failed.
- Move the Spec check and clarification questions to supply time as a card-quality gate, so ambiguity is resolved once before a card enters L2 instead of on the critical path of every run, which is what drives the 2.5 times wall time.
