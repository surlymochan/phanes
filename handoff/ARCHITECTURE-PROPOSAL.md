# Architecture proposal evolution log

## e1885 decision

This record is a proposal decision; ARCHITECTURE.md remains the architecture specification.
The experiment replayed 10 merged production L2 cards (p01 p02 p03 p05 p06 p07 p08 p09 p10 p11),
each with a frozen hidden acceptance suite kept on the control plane only. The control arm ran
card, author, reviewer; the treatment arm ran card, Spec (gpt-6.1-sol high), Spec check
(gpt-6.1-terra high), author writing failing tests from the scenarios first, reviewer checking
against the Spec. Both arms used the same base commit, the same author model (MiniMax-M3.1-Flash),
the same reviewer (gpt-6.1-terra high) and only the first round was scored.

It ran in two rounds, each with criteria frozen in issue 1885 before any run. Round 1 (e1885,
planner comment of 2026-11-11) scored a rejected Spec as a failure with no PR. Round 2 (e1885b,
planner comment of 2026-11-12) changed only that rule: a rejected Spec goes back to the Spec role
with the check comments, is refined and checked again, at most 2 refinement rounds. Refinement ran
in non-strict mode: the Spec role resolves ambiguity itself with the most reasonable assumption and
lists every assumption in the Spec; a Spec still rejected after 2 rounds is released with its
assumptions and marked FORCED. Round 2 also relaxed the cost criterion from 2.0 to 5.0 times
control (still at most 10 USD per PR). The fields below are the round 2 result, which is the final
decision; round 1 is summarised after them.

Frozen manifest digest: sha256:dabe5bb724bff90e03352e513e776dc7b0a82de5e201fd43cdada7a9edbd4e65
Report digest: sha256:325dc597df832e3e6efdc873e3a4492d99c80d27fd837d19e27cf653e32074f3
Paired sample count: 10
Discordant results: merge-valid (hidden suite pass and reviewer APPROVE) treatment-only 0 pairs, control-only 0 pairs, net 0; both arms are merge-valid on the same 3 cards (p03 p05 p07); hidden suite pass totals control 4, treatment 5; false approvals control 2, treatment 3 (p02 added).
Extra runs: treatment used 72 model runs against 30 for control, 42 extra, counting the round 1 Spec and checks it reused; each of the 6 refined cards (p02 p03 p05 p06 p08 p10) used both refinement rounds, 2 were accepted after round 2 (p02 p10) and 4 were released as FORCED (p03 p05 p06 p08).
Queue impact: queue seconds were recorded per run in round 2 and each refinement or check waited about 0.4 seconds, so the extra time is model time from two serial refine and check rounds; median per-card wall time was 720 seconds for control and 1913 seconds for treatment, 2.66 times, and summing active stage time instead gives 2.60 times, above the frozen 2.0 times limit either way.
Quota impact: at frozen public list prices total cost was 1.87 USD for control and 5.63 USD for treatment; cost per merge-valid PR was 0.62 USD for control and 1.88 USD for treatment, 3.0 times, within the round 2 limit of 5.0 times and 10 USD; the most expensive card cost 0.81 USD.
INFRA counted as PASS: no
Decision: abandon
Rationale: Of the six frozen round 2 criteria three failed: net discordant merge-valid pairs was 0 against a required +2, false approvals rose from 2 to 3, and median wall time was 2.66 times control against a 2.0 times limit; hidden suite passes (5 vs 4), cost per PR (3.0 times) and valid pairs (10, none excluded) passed. Refinement fixed the round 1 failure mode, since p03 and p05 now ship passing approved changes, but it only drew level with the current flow. Self-chosen assumptions often differ from the real intent: of the 4 refined cards that failed the hidden suite, 3 failed on behaviour their assumption list had decided differently or left to the implementer, and 1 partly so; the reviewer checks against the same assumed Spec and cannot catch this, which produced the new false approval on p02. The role pipeline stays implemented but disabled by default.

### Round 1 (e1885, rejected Spec scored as failure)

Manifest v2 digest sha256:f0a930fdab98f5ede384f9a4a7f14ba2a928e27f40d8b017ece61238de7f2423, report digest sha256:ed4d082f99e88fe399c8fbf914fd1ddb01acf887fa152614652c7099025fadc6. The Spec check rejected 6 of 10 underspecified cards (p02 p03 p05 p06 p08 p10), which the round 1 rule scored as no PR. Merge-valid control 3 of 10, treatment 1 of 10, net discordant -2; hidden suite passes control 4, treatment 3; false approvals control 2, treatment 1; cost per merge-valid PR 0.62 USD against 2.70 USD, 4.3 times, above the 2.0 times limit then in force; median wall time 720 seconds against 1198 seconds, 1.66 times. Decision: abandon, with the rejection rule identified as the main cause, which led to round 2.

| Measure | Control | Round 1 treatment | Round 2 treatment |
|---|---|---|---|
| Merge-valid | 3 of 10 | 1 of 10 | 3 of 10 |
| Hidden suite passes | 4 | 3 | 5 |
| False approvals | 2 | 1 | 3 |
| Cost per merge-valid PR | 0.62 USD | 2.70 USD | 1.88 USD |
| Median wall time | 720 s | 1198 s | 1913 s |
| Decision | | abandon | abandon |

### Process notes

- Round 2 reused all 10 control results and the treatment results of the 4 cards accepted in round 1 (p01 p07 p09 p11); same base, same configuration, and the control arm is unaffected by the rule change. The 6 rejected cards reused their round 1 Spec and first check, then ran refinement, check, author, scoring and review. Their cost adds the round 1 spend to the new runs; their wall time adds the round 1 segment to the new segment and excludes the gap between them.
- Round 2 pilot p03 was run twice. The first attempt was voided because the new driver read the review result as a mapping while the reviewer returns a string; after the fix the manifest was refrozen (v3.0 archived) and p03 rerun from scratch. One launch before that stopped on a missing environment setting before any run started.
- Round 1: pilot p03 was run twice (the Spec run needed a committed git snapshot); two mechanism faults were fixed before scoring (the Spec check could not see the public baseline, and queue time counted against the driver wait), with all 10 Spec checks redone under manifest v2; control runs interrupted while fixing the driver produced no scored output and were restarted; p06 control had one infrastructure retry, within the frozen rule.
- Round 2 kept ssh stderr in run artifacts and recorded openswarm queue seconds per run, closing the two round 1 driver follow-ups. No run was INFRA in round 2 and no cost alert fired.
- Prices are public list prices recorded in the frozen manifest and not verified against invoices.

### Follow-ups

- Strict mode was designed but not tested, because a replay has no human to answer: a rejected Spec produces clarification questions, Boss or the card author answers them, and the Spec is refined with the answers and checked again. The round 2 assumption analysis suggests this is what underspecified cards need; test it as a new experiment with its own frozen criteria.
- Alternatively move the Spec check to supply time as a card-quality gate, so underspecified cards are clarified before they enter L2.
