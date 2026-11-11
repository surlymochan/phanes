# Architecture proposal evolution log

## e1885 decision

This record is a proposal decision; ARCHITECTURE.md remains the architecture specification.
The experiment replayed 10 merged production L2 cards (p01 p02 p03 p05 p06 p07 p08 p09 p10 p11),
each with a frozen hidden acceptance suite kept on the control plane only. The control arm ran
card, author, reviewer; the treatment arm ran card, Spec (gpt-6.1-sol high), Spec check
(gpt-6.1-terra high), author writing failing tests from the scenarios first, reviewer checking
against the Spec. Both arms used the same base commit, the same author model (MiniMax-M3.1-Flash),
the same reviewer (gpt-6.1-terra high) and only the first round was scored. The adoption criteria
were frozen in the issue before any run (issue 1885, planner comment of 2026-11-11).

Frozen manifest digest: sha256:f0a930fdab98f5ede384f9a4a7f14ba2a928e27f40d8b017ece61238de7f2423
Report digest: sha256:ed4d082f99e88fe399c8fbf914fd1ddb01acf887fa152614652c7099025fadc6
Paired sample count: 10
Discordant results: merge-valid (hidden suite pass and reviewer APPROVE) treatment-only 0 pairs, control-only 2 pairs (p03 p05), net -2; hidden suite pass treatment-only 1 pair (p01), control-only 2 pairs (p03 p05); merge-valid totals control 3 of 10, treatment 1 of 10; hidden suite pass totals control 4, treatment 3.
Extra runs: treatment used 39 model runs against 30 for control, 9 extra; 6 of 10 treatment cards stopped at SPEC_REJECTED after 2 extra runs each, and the superseded first-pass Spec checks cost 0.48 USD, included in treatment cost.
Queue impact: Spec and Spec check calls wait in the shared codex concurrency queue; on 2026-11-11 three of them waited more than 900 seconds and were first misread as infrastructure failures until the driver wait was made queue aware; median per-card wall time excluding repair rounds was 720 seconds for control and 1198 seconds for treatment, 1.66 times.
Quota impact: at frozen public list prices total cost was 1.87 USD for control and 2.70 USD for treatment, 1.44 times per card; cost per merge-valid PR was 0.62 USD for control and 2.70 USD for treatment, 4.3 times, above the frozen 2.0 times limit; no single PR exceeded 10 USD.
INFRA counted as PASS: no
Decision: abandon
Rationale: Of the six frozen criteria three failed: net discordant merge-valid pairs was -2 against a required +2, treatment passed the hidden suite on 3 cards against 4 for control, and cost per merge-valid PR was 4.3 times control against a 2.0 times limit; the false-approve, wall-time and valid-pair criteria passed (1 vs 2 false approvals, 1.66 times wall time, 10 valid pairs with no pair excluded). The dominant effect is that an independent Spec check rejects underspecified production cards (6 of 10), which the frozen protocol scores as no PR, while the current flow still ships a passing change on some of them (p03 p05). The role pipeline stays implemented but disabled by default; a future trial should first fix card quality at supply time or route rejected Specs back to the card author, and must be re-frozen as a new experiment.

### Process notes

- Pilot p03 was run twice. The first attempt was voided because the Spec run needs a committed git snapshot; the snapshot is now created outside the experiment repository.
- Two mechanism faults surfaced during the main batch and were fixed before scoring: the Spec check could not see the public baseline (p09 was rejected for a baseline claim it could not verify), and queue time counted against the driver wait. All 10 treatment Spec checks were redone under the corrected flow (manifest v2 above; v1 archived), reusing the already produced Specs; p01 p09 p11 changed from reject to accept and were then run to completion; p07 treatment was rerun from scratch.
- Control-arm author runs interrupted by the planner while stopping the driver for these fixes produced no scored output and were restarted; p06 control then had exactly one infrastructure retry (SSH transfer exit 255), within the frozen rule.
- Prices are public list prices recorded in the frozen manifest and not verified against invoices; queue seconds were not observable per run, so wall time above is end to end per card.

### Follow-ups

- The experiment driver discards ssh stderr, so the SSH exit 255 on p06 cannot be diagnosed; keep ssh stderr in run artifacts.
- Record openswarm queued seconds per run in the driver so queue time can be reported directly instead of inferred.
- If the role pipeline is retried, decide first how a rejected Spec is handled (return to the card author or to Boss) and freeze that as part of the next experiment.
